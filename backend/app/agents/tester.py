"""Tester Agent: real lint + LLM-generated pytest suite for the generated backend.

This agent does genuine QA on the code the codegen agents produced, mirroring the
build-gate + self-heal pattern used by the Frontend/Backend agents:

  1. Lint  — flake8 (a *bug* pass selecting E9/F-codes for syntax/undefined names,
     plus an informational style pass) and Black (`--check`, with optional
     in-place auto-format) over the generated `backend/main.py`.
  2. Unit/API tests — the codegen model writes a `test_main.py` (FastAPI
     TestClient) derived from the *actual* backend source; it's run with pytest.
     On failure the pytest output is fed back to the model to repair the suite,
     up to `tester_test_retries` times.
  3. Suggestions — the reasoning model returns extra QA/test-coverage advice
     (always available, incl. demo mode), so the agent is useful even when the
     external tools (pytest/black/flake8) aren't installed in the environment.

Everything degrades gracefully: missing tools are reported as "skipped", and the
agent never hard-fails the pipeline — it reports a `passed` flag instead.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Dict

from ..config import settings
from ..local_deploy import (
    APPS_ROOT,
    _slug,
    black_format,
    
    run_black,
    run_flake8,
    run_pytest,
    tool_available,
)
from ..models import Stage
from ..nvidia_client import nvidia
from .base import BaseAgent
from .developer import _strip_fences

# flake8 codes that flag *real* defects (syntax errors, undefined names,
# duplicate function args, f-string/format bugs) rather than cosmetic style.
FLAKE8_BUG_CODES = "E9,F63,F7,F82,F811,F821,F823"

# Named exports the generated React app commonly uses. The codegen model
# sometimes references one (e.g. <NavLink>) WITHOUT importing it — which the
# Vite build does NOT catch (an undefined identifier is a *runtime*
# ReferenceError, not a compile error), so the app builds fine and then crashes
# live in the browser ("NavLink is not defined"). The Tester repairs this.
_REACT_HOOKS = (
    "useState", "useEffect", "useRef", "useMemo", "useReducer", "useCallback",
    "useContext", "useLayoutEffect", "useImperativeHandle", "useId", "useTransition",
)
_ROUTER_NAMES = (
    "HashRouter", "BrowserRouter", "MemoryRouter", "Routes", "Route", "Link",
    "NavLink", "Navigate", "Outlet", "useNavigate", "useParams", "useLocation",
    "useSearchParams", "useMatch", "useResolvedPath", "useOutletContext",
)

TESTER_SYSTEM = (
    "You are a senior QA / test engineer. Output ONLY the complete contents of a "
    "Python file `test_main.py` — no markdown fences, no prose, no explanation.\n"
    "The file tests a FastAPI application defined in `main.py` (SAME directory) "
    "using FastAPI's TestClient.\n"
    "RULES:\n"
    "- Start with: `from fastapi.testclient import TestClient` and `from main import app`, "
    "then `client = TestClient(app)`.\n"
    "- Use ONLY pytest, the Python standard library, and fastapi (all installed). "
    "Do NOT import the real database, network, or any third-party package.\n"
    "- Read the provided source and test the ACTUAL routes that exist. Always "
    "include a test that `GET /health` returns 200.\n"
    "- For POST/PUT endpoints, send valid JSON bodies that match the Pydantic models "
    "in the source. Assert on status codes and the SHAPE/types of the JSON response, "
    "NOT on exact generated values (ids, random codes, timestamps).\n"
    "- Every test function name must start with `test_`. The file MUST be complete, "
    "importable and free of TODOs or undefined names."
)

TESTER_FIX_SYSTEM = (
    "You are a senior QA engineer fixing a failing pytest file `test_main.py` that "
    "tests a FastAPI app in `main.py`. Output ONLY the corrected, complete contents "
    "of `test_main.py` — no markdown fences, no prose. Keep using "
    "`from main import app` + `TestClient(app)`. Fix the failures shown: align the "
    "requests/assertions with how the app ACTUALLY behaves (correct paths, payloads, "
    "status codes, response shape). Do not test values the app generates randomly."
)

SUGGEST_SYSTEM = (
    "You are a senior QA lead reviewing a generated FastAPI backend. In 4-6 short "
    "bullet points, suggest the most valuable additional tests or edge cases to "
    "cover (auth, validation/422s, not-found/404s, boundary inputs, idempotency). "
    "Be concrete and specific to the code. Plain text bullets only."
)


class TesterAgent(BaseAgent):
    stage = Stage.tester
    title = "Tester Agent"

    async def execute(self) -> Dict[str, Any]:
        app_dir: Path = APPS_ROOT / _slug(self.project.name)
        backend_dir = app_dir / "backend"
        main_py = backend_dir / "main.py"

        checks: Dict[str, Any] = {}
        passed = True
        suggestions: list[str] = []

        # Frontend QA first: guarantee the generated React app imports every
        # component/hook it references (prevents runtime "X is not defined").
        frontend_ok = await self._frontend(app_dir, checks)
        passed = passed and frontend_ok

        await self.step("Locating generated backend for QA...")
        if not main_py.exists():
            await self.step("No backend/main.py found — skipping backend QA")
            checks["backend"] = "not found (skipped)"
            await self.step(f"QA complete — overall: {'PASSED' if passed else 'ISSUES FOUND'}")
            return {"checks": checks, "passed": passed, "suggestions": suggestions}

        source = main_py.read_text(encoding="utf-8", errors="ignore")
        await self.step(f"Found backend/main.py ({len(source):,} chars)")

        if settings.tester_run_lint:
            lint_ok = await self._lint(main_py, checks)
            passed = passed and lint_ok

        if settings.tester_run_pytest:
            tests_ok = await self._pytest(backend_dir, source, checks)
            passed = passed and tests_ok

        suggestions = await self._suggest(source)
        if suggestions:
            checks["suggestions"] = f"{len(suggestions)} QA recommendation(s)"

        await self.step(f"QA complete — overall: {'PASSED' if passed else 'ISSUES FOUND'}")
        return {
            "checks": checks,
            "passed": passed,
            "suggestions": suggestions,
        }

    # -------------------------------------------------------------- frontend
    async def _frontend(self, app_dir: Path, checks: Dict[str, Any]) -> bool:
        """Repair imports and run a `no-undef` static check on the generated JSX.

        1) Auto-fix missing React / react-router-dom imports (the common case).
        2) Re-scan the effective code for ANY JSX component that is still
           referenced but neither imported nor defined — the runtime-crash class
           (`X is not defined`) that the Vite build silently lets through.

        Rewrites src/App.jsx in place so the app the Deployment agent serves next
        actually runs. Returns False only when unfixable undefined components
        remain (something we can't auto-import because the source is unknown).
        """
        app_jsx = app_dir / "src" / "App.jsx"
        if not app_jsx.exists():
            checks["frontend imports"] = "skipped (no src/App.jsx)"
            checks["frontend no-undef"] = "skipped (no src/App.jsx)"
            return True

        await self.step("Checking generated React imports (src/App.jsx)...")
        code = app_jsx.read_text(encoding="utf-8", errors="ignore")
        fixed, added = _fix_imports(code)
        if added:
            app_jsx.write_text(fixed, encoding="utf-8")
            checks["frontend imports"] = f"fixed {len(added)} missing ({', '.join(added)})"
            await self.step(f"Fixed missing import(s) in src/App.jsx: {', '.join(added)}")
        else:
            checks["frontend imports"] = "passed (all used symbols imported)"
            await self.step("React imports: all referenced hooks/router symbols are imported")

        # no-undef static analysis on the (possibly repaired) code.
        await self.step("Running no-undef static analysis on src/App.jsx...")
        undefined = _undefined_components(fixed)
        if not undefined:
            checks["frontend no-undef"] = "passed (no undefined components)"
            await self.step("no-undef: all JSX components are imported or defined")
            return True
        checks["frontend no-undef"] = f"{len(undefined)} undefined ({', '.join(undefined)})"
        await self.step(
            "no-undef: undefined JSX component(s) — referenced but not imported or defined:"
        )
        for name in undefined:
            await self.step(f"  ! <{name} /> is not defined (would crash at runtime)")
        return False

    # ------------------------------------------------------------------ lint
    async def _lint(self, main_py: Path, checks: Dict[str, Any]) -> bool:
        """Run flake8 (bug pass + style pass) and Black over the backend."""
        ok = True

        # 1) flake8 — real-bug pass (syntax errors / undefined names).
        if tool_available("flake8"):
            bug_ok, bug_out = await asyncio.to_thread(run_flake8, main_py, FLAKE8_BUG_CODES)
            if bug_ok:
                checks["lint (flake8 bugs)"] = "passed (0 errors)"
                await self.step("flake8 [E9,F]: passed — no syntax errors or undefined names")
            else:
                n = len([ln for ln in bug_out.splitlines() if ":" in ln])
                checks["lint (flake8 bugs)"] = f"{n} error(s)"
                await self.step(f"flake8 [E9,F]: {n} real issue(s) found")
                for ln in bug_out.splitlines()[:6]:
                    await self.step(f"  {ln}")
                ok = False

            # 2) flake8 — informational style pass (never fails the build).
            style_ok, style_out = await asyncio.to_thread(run_flake8, main_py, None)
            n_style = 0 if style_ok else len([ln for ln in style_out.splitlines() if ":" in ln])
            checks["lint (flake8 style)"] = "clean" if style_ok else f"{n_style} style hint(s)"
            await self.step(
                "flake8 style: clean" if style_ok else f"flake8 style: {n_style} hint(s) (informational)"
            )
        else:
            checks["lint (flake8)"] = "skipped (flake8 not installed)"
            await self.step("flake8 not installed — skipping lint (pip install flake8)")

        # 3) Black formatting check (optionally auto-format in place).
        if tool_available("black"):
            black_ok, _ = await asyncio.to_thread(run_black, main_py)
            if black_ok:
                checks["format (black)"] = "passed (already formatted)"
                await self.step("black --check: passed — code is correctly formatted")
            elif settings.tester_autoformat:
                fmt_ok, _ = await asyncio.to_thread(black_format, main_py)
                checks["format (black)"] = "auto-formatted" if fmt_ok else "needs formatting"
                await self.step(
                    "black: reformatted backend/main.py" if fmt_ok else "black: formatting failed"
                )
            else:
                checks["format (black)"] = "needs formatting"
                await self.step("black --check: would reformat backend/main.py")
        else:
            checks["format (black)"] = "skipped (black not installed)"
            await self.step("black not installed — skipping format check (pip install black)")

        return ok

    # ----------------------------------------------------------------- pytest
    async def _pytest(self, backend_dir: Path, source: str, checks: Dict[str, Any]) -> bool:
        """Generate a pytest suite from the backend source, run it, self-heal."""
        if not tool_available("pytest"):
            checks["unit/api tests (pytest)"] = "skipped (pytest not installed)"
            await self.step("pytest not installed — skipping unit tests (pip install pytest)")
            return True

        test_file = backend_dir / "test_main.py"
        await self.step(f"Generating pytest suite (FastAPI TestClient) with {settings.codegen_model}...")
        tests = await self._generate_tests(source)
        if "def test_" not in tests:
            checks["unit/api tests (pytest)"] = "generation incomplete (skipped)"
            await self.step("Test generation returned no test functions — skipping pytest")
            return True

        tests_ok = False
        output = ""
        for attempt in range(1, settings.tester_test_retries + 2):
            test_file.write_text(tests, encoding="utf-8")
            await self.step(f"Running pytest (attempt {attempt})...")
            tests_ok, output = await asyncio.to_thread(run_pytest, backend_dir)
            summary = _pytest_summary(output)
            if tests_ok:
                await self.step(f"pytest: {summary}")
                break
            await self.step(f"pytest failed: {summary}")
            if attempt > settings.tester_test_retries:
                break
            await self.step(f"Auto-fixing test_main.py from the pytest output (self-heal {attempt})...")
            tests = await self._repair_tests(source, tests, output)

        await self.preview("backend/test_main.py", tests)
        checks["unit/api tests (pytest)"] = _pytest_summary(output)
        return tests_ok

    async def _generate_tests(self, source: str) -> str:
        prompt = (
            f'Write pytest tests for this FastAPI backend of "{self.project.name}".\n\n'
            f"backend/main.py:\n{source[:9000]}\n\n"
            "Output the complete test_main.py now (TestClient-based, covering the real "
            "endpoints above). The file MUST be complete and importable."
        )
        raw = await asyncio.to_thread(
            nvidia.complete_code,
            prompt,
            system=TESTER_SYSTEM,
            model=settings.codegen_model,
            max_tokens=2048,
            temperature=0.3,
        )
        return _strip_fences(raw)

    async def _repair_tests(self, source: str, tests: str, output: str) -> str:
        prompt = (
            f'The pytest file for "{self.project.name}" is failing.\n\n'
            f"PYTEST OUTPUT:\n{output[-1800:]}\n\n"
            f"CURRENT test_main.py:\n{tests[:6000]}\n\n"
            f"backend/main.py (for reference):\n{source[:6000]}\n\n"
            "Return the COMPLETE corrected test_main.py. Fix the failures by matching "
            "the app's real behaviour."
        )
        raw = await asyncio.to_thread(
            nvidia.complete_code,
            prompt,
            system=TESTER_FIX_SYSTEM,
            model=settings.codegen_model,
            max_tokens=2048,
            temperature=0.2,
        )
        fixed = _strip_fences(raw)
        return fixed if "def test_" in fixed else tests

    # ------------------------------------------------------------- suggestions
    async def _suggest(self, source: str) -> list[str]:
        """Ask the reasoning model for extra QA/test-coverage suggestions."""
        await self.step("Requesting additional QA / test-coverage suggestions...")
        prompt = (
            f'Review this FastAPI backend for "{self.project.name}" and suggest the '
            f"highest-value additional tests / edge cases.\n\nmain.py:\n{source[:6000]}"
        )
        raw = await asyncio.to_thread(
            nvidia.complete,
            prompt,
            system=SUGGEST_SYSTEM,
            model=settings.nemotron_model,
            max_tokens=400,
            temperature=0.4,
        )
        suggestions = _bullets(raw)
        for s in suggestions[:6]:
            await self.step(f"  suggestion: {s}")
        if suggestions:
            await self.emit(
                "test_suggestions",
                "QA suggestions for further test coverage",
                {"suggestions": suggestions},
            )
        return suggestions


def _used(names: tuple[str, ...], code: str) -> set[str]:
    """Names referenced in `code` as standalone identifiers.

    The negative lookbehind on `.`/word-chars avoids false positives like
    matching `Route` inside `Routes` or `Navigate` inside `useNavigate`.
    """
    return {n for n in names if re.search(rf"(?<![\w.]){n}\b", code)}


def _imported_names(code: str, module: str) -> set[str]:
    """Names already pulled in via `import { ... } from '<module>'` (if any)."""
    pat = rf"import\s+(?:\w+\s*,\s*)?\{{([^}}]*)\}}\s*from\s*['\"]{re.escape(module)}['\"]"
    m = re.search(pat, code)
    return {x.strip() for x in m.group(1).split(",") if x.strip()} if m else set()


def _merge_named_import(code: str, module: str, used: set[str], default: str = "") -> str:
    """Ensure `code` imports every name in `used` from `module`.

    Merges into an existing `import { ... } from '<module>'` (preserving any
    default import) or prepends a fresh import line when none exists.
    """
    if not used:
        return code
    pat = rf"import\s+(?:(\w+)\s*,\s*)?\{{([^}}]*)\}}\s*from\s*['\"]{re.escape(module)}['\"]\s*;?"
    m = re.search(pat, code)
    if m:
        existing_default = m.group(1) or default
        names = {x.strip() for x in m.group(2).split(",") if x.strip()} | used
        lead = f"{existing_default}, " if existing_default else ""
        repl = f"import {lead}{{ {', '.join(sorted(names))} }} from '{module}';"
        return code[: m.start()] + repl + code[m.end() :]
    if default:
        pat2 = rf"import\s+{re.escape(default)}\s+from\s*['\"]{re.escape(module)}['\"]\s*;?"
        m2 = re.search(pat2, code)
        if m2:
            repl = f"import {default}, {{ {', '.join(sorted(used))} }} from '{module}';"
            return code[: m2.start()] + repl + code[m2.end() :]
    lead = f"{default}, " if default else ""
    return f"import {lead}{{ {', '.join(sorted(used))} }} from '{module}';\n" + code


def _fix_imports(code: str) -> tuple[str, list[str]]:
    """Repair missing React-hook / react-router-dom named imports in JSX.

    Returns (possibly-rewritten code, sorted list of names that were added).
    """
    if not code or "import" not in code:
        return code, []
    added: list[str] = []
    for module, names, default in (
        ("react", _REACT_HOOKS, "React"),
        ("react-router-dom", _ROUTER_NAMES, ""),
    ):
        used = _used(names, code)
        missing = used - _imported_names(code, module)
        if missing:
            code = _merge_named_import(code, module, used, default=default)
            added += sorted(missing)
    return code, added


# Identifiers that are always in scope for a React component file, so a JSX tag
# referencing them is never "undefined" even without a local definition/import.
_JSX_BUILTINS = {"React", "Fragment"}


def _declared_names(code: str) -> set[str]:
    """All names brought into scope: imports (default/namespace/named, incl.
    `as` aliases) plus top-level function/class/const/let/var declarations."""
    names: set[str] = set()
    for m in re.finditer(r"import\s+(.*?)\s+from\s+['\"][^'\"]+['\"]", code, re.S):
        clause = m.group(1)
        block = re.search(r"\{([^}]*)\}", clause)
        if block:
            for part in block.group(1).split(","):
                part = part.strip()
                if part:
                    names.add(part.split(" as ")[-1].strip())
            clause = clause[: block.start()] + clause[block.end() :]
        for ns in re.finditer(r"\*\s+as\s+(\w+)", clause):
            names.add(ns.group(1))
        default = re.match(r"\s*(\w+)", clause)
        if default:
            names.add(default.group(1))
    for m in re.finditer(r"\b(?:function|class)\s+(\w+)", code):
        names.add(m.group(1))
    for m in re.finditer(r"\b(?:const|let|var)\s+(\w+)", code):
        names.add(m.group(1))
    return names


def _jsx_components(code: str) -> set[str]:
    """Root names of JSX elements that are components (start uppercase). Lowercase
    tags (div, span, ...) are intrinsic HTML and never need importing."""
    used: set[str] = set()
    for m in re.finditer(r"<([A-Z]\w*(?:\.\w+)*)", code):
        used.add(m.group(1).split(".")[0])
    return used


def _undefined_components(code: str) -> list[str]:
    """ESLint `no-undef`-style check for JSX: components referenced in markup
    that are neither imported nor defined locally (the runtime-crash class the
    Vite build silently lets through, e.g. `<NavLink/>` with no import)."""
    if not code:
        return []
    declared = _declared_names(code) | _JSX_BUILTINS
    return sorted(n for n in _jsx_components(code) if n not in declared)


def _pytest_summary(output: str) -> str:
    """Pull pytest's one-line result summary (e.g. '5 passed in 0.42s')."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        low = ln.lower()
        if ("passed" in low or "failed" in low or "error" in low or "no tests ran" in low):
            return ln.strip("= ").strip()
    return lines[-1][:120] if lines else "no output"


def _bullets(text: str) -> list[str]:
    """Normalize an LLM bullet list into clean strings.

    Keeps only genuine bullet lines: drops prose preamble/epilogue paragraphs
    (long lines) and bare section headers (lines ending in ':') so the streamed
    output stays a tidy, scannable list.
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        ln = raw.strip().lstrip("-*•0123456789.)( ").replace("**", "").strip()
        if len(ln) <= 3 or len(ln) > 200 or ln.endswith(":"):
            continue
        out.append(ln if len(ln) <= 160 else ln[:157] + "...")
    return out[:8]
