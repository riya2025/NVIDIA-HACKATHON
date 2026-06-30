"""Code-generation agents: real codegen with NVIDIA NIM models.

Code generation is split across three agents that the orchestrator runs
concurrently (asyncio.gather), so frontend, backend and deployment artifacts are
produced in parallel:

  - FrontendAgent  -> a tailored, runnable single-file React app (served live).
  - BackendAgent   -> a real FastAPI backend (main.py) + requirements.txt.
  - DevOpsAgent    -> deployment artifacts (Dockerfile, docker-compose, README).

Each agent reads the project's *requirement* (the user's description, refined by
the Architect agent) and writes only the files it owns. The served app is the
generated frontend; the backend/devops files are real source artifacts the
Deployment agent runs / packages for AWS (Phase 2).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

from ..config import settings
from ..local_deploy import (
    APPS_ROOT,
    _slug,
    backend_build,
    npm_install,
    vite_build,
    working_app_html,
    write_files,
)
from ..models import Stage
from ..nvidia_client import nvidia
from ..react_template import (
    _STYLES_CSS,
    _as_text,
    compose_readme,
    react_project_files,
    scaffold_user_guide,
)
from .base import BaseAgent


def _strip_fences(text: str) -> str:
    """Extract code from a model response, tolerating a prose preamble/epilogue.

    Models (incl. Qwen) sometimes ignore "no markdown" and emit
    `Here's the code:\n```jsx\n...\n````. We pull out the largest fenced block
    when present; otherwise we return the trimmed text as-is.
    """
    t = (text or "").strip()
    if "```" in t:
        first = t.find("```")
        # Skip the opening fence + optional language tag on that line.
        nl = t.find("\n", first)
        if nl != -1:
            after = t[nl + 1 :]
            close = after.rfind("```")
            return (after[:close] if close != -1 else after).strip()
    return t.strip()


def _arch_text(arch: Dict[str, Any]) -> str:
    parts = []
    for k in ("frontend", "backend", "database", "deployment"):
        v = arch.get(k)
        if v:
            parts.append(f"{k}: {v if not isinstance(v, dict) else v.get('technology', v)}")
    return "; ".join(parts) if parts else "frontend: React; backend: FastAPI; database: SQLite"


def _stack(project) -> Dict[str, str]:
    """Architecture choices as a flat {layer: text} map for the README table."""
    arch = project.architecture or {}
    return {k: _as_text(arch.get(k)) for k in ("frontend", "backend", "database", "deployment")}


README_SYSTEM = (
    "You are a technical writer creating the 'how to use and test' part of a README "
    "for a web app, aimed at a non-technical person who will click around to confirm "
    "the app works. You are given the COMPLETE React source (src/App.jsx). Write "
    "concise GitHub-flavored Markdown that:\n"
    "1. Begins with a `## Using the app` heading, then walks through EACH screen / page "
    "/ view the app actually has. For each one, list in plain language what the key "
    "buttons, links, inputs and controls do. Describe ONLY features that genuinely exist "
    "in the code — never invent pages or buttons.\n"
    "2. Ends with a `## Quick test` heading followed by a numbered checklist (4-7 short "
    "steps) walking the reader through the main happy path to verify the app end to end, "
    "including confirming data appears where expected. If the code calls the backend "
    "(e.g. fetch / window.API_BASE), add a step to confirm the backend responds.\n"
    "Keep it tight and skimmable. Output ONLY the Markdown — no code fences around the "
    "whole thing, no preamble, no sign-off. Do NOT include install, build, or deployment "
    "instructions; those are added separately."
)


FRONTEND_SYSTEM = (
    "You are a senior React engineer. Output ONLY the complete contents of a single "
    "file `src/App.jsx` — no markdown fences, no prose, no explanation, no filename header.\n"
    "This is a REAL Vite + React 18 project (ES modules), so you MUST use imports:\n"
    "  import React, { useState, useEffect, useRef, useMemo, useReducer } from 'react';\n"
    "  import { HashRouter, Routes, Route, Link, NavLink, useNavigate, useParams, Navigate } from 'react-router-dom';\n"
    "RULES:\n"
    "- `export default function App()` MUST return <HashRouter>...<Routes>...</Routes></HashRouter>.\n"
    "- Define ALL sub-components in THIS SAME FILE. Do NOT import any local files "
    "(no './pages', './store', etc.) — only 'react' and 'react-router-dom'.\n"
    "- A global stylesheet is already loaded; style with className using these classes: "
    "app, topbar, nav, container, wrap, card, row, btn, btn-primary, primary, input, label, "
    "field, list, list-item, rec, badge, pill, muted, grid, stat, box, center, login.\n"
    "- DATA STORAGE: a FastAPI backend is reachable at the global string `window.API_BASE` "
    "(set on the page) and is the PRIMARY data store. For the app's main list of records, "
    "use this exact REST contract (the backend implements the same one):\n"
    "    GET    window.API_BASE + '/api/items'        -> returns a JSON array of records\n"
    "    POST   window.API_BASE + '/api/items'        -> body = the new record (JSON); returns the saved record\n"
    "    PUT    window.API_BASE + '/api/items/' + id  -> body = the updated record; returns it\n"
    "    DELETE window.API_BASE + '/api/items/' + id  -> deletes the record\n"
    "  Each record is a JSON object with a string `id` plus this app's fields. Load the list "
    "with GET inside a useEffect on mount, and call POST/PUT/DELETE on every change, using "
    "async/await wrapped in try/catch.\n"
    "- RESILIENCE: ALWAYS mirror the records to localStorage too, and render from localStorage "
    "immediately so the UI is instant. If `window.API_BASE` is unset or any fetch throws, fall "
    "back to localStorage so the app fully works offline / with no backend. Never let a failed "
    "request crash the UI or block rendering.\n"
    "- Implement EXACTLY the features in the requirement — no generic placeholder app. The app's "
    "domain fields go INSIDE each record object; the /api/items routes above stay the same.\n"
    "- Do NOT use Material-UI, Chakra, Bootstrap, antd or ANY external component library "
    "(they are NOT installed and will FAIL the build).\n"
    "- JSX comments MUST be written as {/* ... */}. NEVER put // or /* */ comments inside JSX tags.\n"
    "- Write complete, COMPILING code: no TODOs, no undefined variables, no missing imports.\n"
    "The default export MUST be a component named exactly `App` that renders <HashRouter>."
)

FRONTEND_FIX_SYSTEM = (
    "You are a senior React engineer fixing a Vite build error in a single-file React "
    "component. Output ONLY the corrected, complete contents of `src/App.jsx` — no "
    "markdown fences, no prose. ES module imports only from 'react' and 'react-router-dom'; "
    "`export default function App()`; JSX comments must be {/* ... */}; no external UI "
    "libraries; no undefined references; no missing imports."
)

_BASE_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;background:#0b0f17;color:#e6edf3}
a{color:#58a6ff;text-decoration:none}
.topbar{display:flex;align-items:center;gap:18px;padding:14px 22px;background:#0f1622;border-bottom:1px solid #1f2a3a;position:sticky;top:0;z-index:10}
.topbar .brand{font-weight:700;font-size:18px;margin-right:auto}
.nav a,.nav button{color:#9aa7b4;background:none;border:none;font-size:14px;padding:8px 12px;border-radius:8px;cursor:pointer}
.nav a.active,.nav a:hover,.nav button:hover{color:#fff;background:#16324f}
.container{max-width:760px;margin:0 auto;padding:24px}
.card{background:#0f1622;border:1px solid #1f2a3a;border-radius:14px;padding:22px;margin-bottom:18px}
h1,h2,h3{color:#e6edf3}
.label{display:block;font-size:12px;color:#9aa7b4;margin:12px 0 4px}
.input,input[type=text],input[type=email],input[type=password],input[type=number],textarea,select{
  width:100%;padding:11px;border-radius:9px;border:1px solid #25324a;background:#0b1119;color:#e6edf3;font-size:14px}
input[type=file]{color:#9aa7b4}
.btn,button{cursor:pointer;border:1px solid #25324a;background:#16202e;color:#e6edf3;padding:11px 16px;border-radius:9px;font-size:14px;font-weight:600}
.btn-primary,button.btn-primary{background:#2f81f7;border-color:#2f81f7;color:#fff}
.btn-primary:hover{background:#4b95ff}
.btn:disabled{opacity:.5;cursor:not-allowed}
.field{margin-bottom:6px}
.list{list-style:none;padding:0;margin:0}
.list-item{border:1px solid #1f2a3a;border-radius:10px;padding:14px;margin-top:10px;background:#0b1119}
.badge{display:inline-block;font-size:11px;padding:2px 9px;border-radius:999px;background:#16324f;color:#79c0ff;margin-top:6px}
.muted{color:#8b97a6;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
img.preview{max-width:100%;border-radius:10px;margin-top:10px}
"""

def _wants_map(project) -> bool:
    # Map support is a FRONTEND feature, so detect it from the description + the
    # frontend layer only — matching react_template.react_project_files() and
    # local_deploy.working_app_html(). Using all architecture layers here caused
    # inconsistent use_map values (e.g. "delivery"/"track" in the backend/database
    # layer would inject Leaflet and a README map note the scaffold doesn't have).
    arch = project.architecture or {}
    text = (project.description or "") + " " + _as_text(arch.get("frontend", ""))
    return any(k in text.lower() for k in ("map", "location", "geo", "pothole", "track", "delivery"))


def _vite_scaffold(project) -> Dict[str, str]:
    """Fixed, known-good Vite + React project files. The model only authors
    `src/App.jsx`; everything here is boilerplate that always builds.

    `src/main.jsx` renders <App/> directly, so the generated App owns the
    <HashRouter> (matching the codegen contract).
    """
    name = project.name
    pkg = json.dumps(
        {
            "name": _slug(name),
            "private": True,
            "version": "0.1.0",
            "type": "module",
            "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
            "dependencies": {
                "react": "^18.3.1",
                "react-dom": "^18.3.1",
                "react-router-dom": "^6.26.2",
            },
            "devDependencies": {
                "@vitejs/plugin-react": "^4.3.1",
                "vite": "^5.4.8",
            },
        },
        indent=2,
    )

    leaflet = (
        '    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />\n'
        '    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>\n'
        if _wants_map(project)
        else ""
    )
    title = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    index_html = (
        '<!doctype html>\n<html lang="en">\n  <head>\n'
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"    <title>{title}</title>\n"
        f"{leaflet}"
        '  </head>\n  <body>\n    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.jsx"></script>\n'
        "  </body>\n</html>\n"
    )
    main_jsx = (
        "import React from 'react'\n"
        "import ReactDOM from 'react-dom/client'\n"
        "import App from './App.jsx'\n"
        "import './styles.css'\n\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(\n"
        "  <React.StrictMode>\n    <App />\n  </React.StrictMode>\n)\n"
    )
    vite_cfg = (
        "import { defineConfig } from 'vite'\n"
        "import react from '@vitejs/plugin-react'\n\n"
        "export default defineConfig({ plugins: [react()] })\n"
    )
    return {
        "package.json": pkg,
        "vite.config.js": vite_cfg,
        "index.html": index_html,
        "src/main.jsx": main_jsx,
        "src/styles.css": _BASE_CSS + "\n" + _STYLES_CSS,
    }


def _build_error(output: str) -> str:
    """Pull a concise summary out of a Vite/Rollup build log.

    Vite often prints a generic "error during build:" header followed by the real
    message (file + reason) on the next line, so we include the following line too.
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "error" in low and "0 error" not in low:
            summary = ln
            if low.rstrip().endswith("error during build:") and i + 1 < len(lines):
                summary = f"{ln} {lines[i + 1]}"
            return summary[:240]
    return lines[-1][:240] if lines else "unknown build error"


def _backend_error(output: str) -> str:
    """Summarize a py_compile / import traceback to one informative line."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "unknown backend error"
    # The final traceback line is usually `ErrorType: message`.
    for ln in reversed(lines):
        if any(tok in ln for tok in ("Error:", "Error ", "assert", "SyntaxError")):
            return ln[:240]
    return lines[-1][:240]

BACKEND_SYSTEM = (
    "You are a senior backend engineer. You output ONE complete FastAPI application "
    "(main.py) and NOTHING else (no markdown fences, no prose). Use FastAPI + Pydantic. "
    "Enable permissive CORS (allow_origins=['*']).\n"
    "PERSISTENCE: store data with the stdlib `sqlite3` in an ON-DISK file named `app.db` "
    "next to main.py — open it with `sqlite3.connect('app.db', check_same_thread=False)`. "
    "Do NOT use ':memory:': data MUST survive across requests and process restarts. Run "
    "`CREATE TABLE IF NOT EXISTS ...` at startup and `commit()` after every write.\n"
    "REST CONTRACT (the frontend calls EXACTLY these for the app's main records — implement "
    "them precisely):\n"
    "    GET    /api/items        -> return a JSON array of all records\n"
    "    POST   /api/items        -> body = new record; assign a string `id` if missing, save, return the record\n"
    "    PUT    /api/items/{id}   -> body = updated record; update by id, return it\n"
    "    DELETE /api/items/{id}   -> delete by id\n"
    "Each record is an ARBITRARY JSON object with a string `id` plus whatever fields this app "
    "uses (e.g. name, done, checked, status, priority, notes...). CRITICAL: persist and return "
    "the WHOLE record with ALL its fields. Do NOT define a narrow Pydantic body model that "
    "lists only a few fields — FastAPI silently DROPS any field not in the model, so a toggled "
    "`checked`/`done`/`status` would never be saved and would reset on reload. Instead accept "
    "the body as a free-form object (e.g. `item: dict = Body(...)`), store the ENTIRE object as "
    "JSON in a TEXT column, and return the ENTIRE object (GET returns each full stored object). "
    "You MAY add extra endpoints the requirement implies, but the /api/items routes above must "
    "exist and round-trip every field unchanged. "
    "For email/2FA, generate a 6-digit code and RETURN it in the response (dev mode) with a "
    "comment that production would email it. Include a `/health` endpoint returning "
    "{'status': 'ok'}. Expose the app as a module-level `app = FastAPI(...)` so it runs with "
    "`uvicorn main:app`.\n"
    "IMPORTANT: import ONLY the Python standard library plus fastapi, pydantic and starlette "
    "(already installed). Do NOT import third-party packages such as sqlalchemy, "
    "jose/python-jose, passlib, bcrypt, requests, etc. — they are not installed and will "
    "break the build. The file MUST be complete and compile (every brace/paren/bracket "
    "closed) — never stop mid-statement."
)

BACKEND_FIX_SYSTEM = (
    "You are a senior backend engineer fixing a Python/FastAPI error in a single main.py. "
    "Output ONLY the corrected, complete main.py — no markdown fences, no prose. Use only "
    "the standard library plus fastapi/pydantic/starlette; expose a module-level "
    "`app = FastAPI(...)`; the file MUST be complete and compile (no missing imports, no "
    "undefined names, every bracket closed) — never stop mid-statement."
)


class FrontendAgent(BaseAgent):
    """Generates a real Vite + React project, then build-gates it.

    Flow: scaffold a known-good Vite project -> the model authors `src/App.jsx`
    -> `npm install` -> `vite build` (real compile gate). If the build fails, the
    error is fed back to the model to auto-fix the code (real code self-healing),
    up to `frontend_build_retries` times, before falling back to a verified
    scaffold. The output is genuine React source, served by the Vite dev server.
    """

    stage = Stage.frontend
    title = "Frontend Agent"

    async def execute(self) -> Dict[str, Any]:
        req = self.project.description
        arch_summary = _arch_text(self.project.architecture or {})
        app_dir: Path = APPS_ROOT / _slug(self.project.name)

        await self.step(f"Reading requirement: {req[:90]}...")
        await self.step(f"Target stack (Architect): {arch_summary}")

        # 1) Lay down the known-good Vite scaffold (everything except src/App.jsx).
        write_files(self.project, _vite_scaffold(self.project), ensure_index=False)

        # 2) Ask the model for a real, tailored src/App.jsx.
        await self.step(f"Generating React app (src/App.jsx) with {settings.codegen_model}...")
        app_code = await self._generate(req)

        # 3) Install the toolchain (reused across build retries / heals).
        await self.step("Installing dependencies (react, react-router-dom, vite)...")
        deps_ok = await asyncio.to_thread(npm_install, app_dir)
        if not deps_ok:
            await self.step("npm unavailable — serving a static fallback app instead")
            fallback_html = working_app_html(self.project)
            write_files(self.project, {"index.html": fallback_html}, ensure_index=True)
            await self.preview("frontend (static fallback)", fallback_html)
            await self._write_readme(app_code, used_fallback=True)
            return {
                "files": ["index.html", "README.md"],
                "frontend": "static fallback (npm unavailable)",
                "frontend_fallback": True,
            }

        # 4) Real compile gate + auto-fix loop (code self-healing at build time).
        build_ok = False
        used_fallback = False
        for attempt in range(1, settings.frontend_build_retries + 2):
            write_files(self.project, {"src/App.jsx": app_code}, ensure_index=False)
            await self.step(f"Compiling with Vite (attempt {attempt})...")
            build_ok, output = await asyncio.to_thread(vite_build, app_dir)
            if build_ok:
                await self.step(f"Vite build passed ({len(app_code):,} chars) — code compiles cleanly")
                break
            await self.step(f"Build failed: {_build_error(output)}")
            if attempt > settings.frontend_build_retries:
                break
            await self.step(f"Auto-fixing src/App.jsx from the build error (self-heal {attempt})...")
            app_code = await self._repair(req, app_code, output)

        # 5) Couldn't make the generated code compile — fall back to a verified scaffold.
        if not build_ok:
            await self.step("Auto-fix exhausted — falling back to a verified React scaffold")
            fb_files = react_project_files(self.project)
            write_files(self.project, fb_files, ensure_index=True)
            ok, _ = await asyncio.to_thread(vite_build, app_dir)
            used_fallback = True
            app_code = fb_files.get("src/App.jsx", app_code)
            await self.step("Verified scaffold build " + ("passed" if ok else "completed"))

        await self.preview("frontend (src/App.jsx)", app_code)
        await self._write_readme(app_code, used_fallback=used_fallback)
        await self.step(f"wrote React (Vite) project -> {app_dir}")
        return {
            "files": ["index.html", "package.json", "vite.config.js", "src/main.jsx", "src/App.jsx", "README.md"],
            "frontend": "React (Vite multi-file project, build-verified)",
            "frontend_fallback": used_fallback,
        }

    async def _write_readme(self, app_code: str, used_fallback: bool) -> None:
        """Generate the user-facing README (usage walkthrough + test checklist).

        For AI-generated apps the guide is authored by the model FROM the actual
        `src/App.jsx`, so it describes whatever pages/buttons that specific app has.
        For the fixed fallback scaffold (or when the model guide is unavailable) we
        use the accurate scaffold walkthrough instead.
        """
        guide = ""
        if not used_fallback and nvidia.live:
            await self.step("Writing a usage & test guide (README) from the generated code...")
            guide = await self._guide(app_code)
        if not guide:
            guide = scaffold_user_guide(_wants_map(self.project))
        readme = compose_readme(
            self.project.name, self.project.description, _stack(self.project), guide
        )
        write_files(self.project, {"README.md": readme}, ensure_index=False)
        await self.step("wrote README.md (usage & test guide)")

    async def _guide(self, app_code: str) -> str:
        """Ask the model for a 'how to use / test' guide tailored to this app's code."""
        prompt = (
            f"Application name: {self.project.name}\n"
            f"What it's meant to do: {self.project.description}\n\n"
            "Complete src/App.jsx for the app:\n\n"
            f"{app_code[:12000]}\n\n"
            "Write the `## Using the app` walkthrough and `## Quick test` checklist now."
        )
        raw = await asyncio.to_thread(
            nvidia.complete,
            prompt,
            system=README_SYSTEM,
            model=settings.codegen_model,
            max_tokens=1200,
            temperature=0.3,
        )
        text = (raw or "").strip()
        if text.startswith("```"):
            text = _strip_fences(text).strip()
        # Reject empty / demo-mode canned output so we fall back to the scaffold guide.
        if len(text) < 80 or text.lower().startswith("[demo]"):
            return ""
        return text

    async def _generate(self, req: str) -> str:
        prompt = (
            f"Application name: {self.project.name}\n"
            f"Requirement (implement these features precisely):\n{req}\n\n"
            "Write the complete src/App.jsx now (imports + App + sub-components in one file). "
            "The file MUST be complete and balanced (every { ( [ closed) — never stop mid-statement."
        )
        raw = await asyncio.to_thread(
            nvidia.complete_code,
            prompt,
            system=FRONTEND_SYSTEM,
            model=settings.codegen_model,
            max_tokens=4096,
            temperature=0.4,
        )
        return _strip_fences(raw)

    async def _repair(self, req: str, code: str, build_output: str) -> str:
        prompt = (
            f'The src/App.jsx for "{self.project.name}" fails to build with Vite.\n\n'
            f"BUILD ERROR:\n{build_output[-1800:]}\n\n"
            f"CURRENT src/App.jsx:\n{code[:9000]}\n\n"
            "Return the COMPLETE corrected src/App.jsx (the whole file). Keep the same "
            "features and requirement. Fix the error above. The file MUST be complete "
            "(every brace/paren/bracket closed) — never stop mid-statement."
        )
        raw = await asyncio.to_thread(
            nvidia.complete_code,
            prompt,
            system=FRONTEND_FIX_SYSTEM,
            model=settings.codegen_model,
            max_tokens=4096,
            temperature=0.2,
        )
        fixed = _strip_fences(raw)
        return fixed if len(fixed) > 120 else code


class BackendAgent(BaseAgent):
    """Generates the FastAPI backend (main.py) and build-gates it.

    The generated main.py is py_compiled and import-booted (see local_deploy.
    backend_build). On failure the error is fed back to the model to auto-fix the
    code, up to `backend_build_retries` times, before falling back to a minimal
    FastAPI scaffold that always compiles.
    """

    stage = Stage.backend
    title = "Backend Agent"

    async def execute(self) -> Dict[str, Any]:
        req = self.project.description
        app_dir: Path = APPS_ROOT / _slug(self.project.name)

        # requirements.txt is fixed boilerplate; write it up front.
        write_files(
            self.project,
            {
                "backend/requirements.txt": (
                    "fastapi==0.138.0\nuvicorn[standard]==0.49.0\n"
                    "pydantic==2.11.0\npython-multipart==0.0.9\n"
                )
            },
            ensure_index=False,
        )

        await self.step(f"Reading requirement: {req[:90]}...")
        await self.step(f"Generating FastAPI backend with {settings.codegen_model}...")
        backend = await self._generate(req)
        if "fastapi" not in backend.lower():
            await self.step("Backend codegen incomplete — using minimal FastAPI scaffold")
            backend = _fallback_backend(self.project.name)

        # Real compile gate + auto-fix loop (backend code self-healing).
        build_ok = False
        used_fallback = False
        for attempt in range(1, settings.backend_build_retries + 2):
            write_files(self.project, {"backend/main.py": backend}, ensure_index=False)
            await self.step(f"Compiling backend (py_compile + import, attempt {attempt})...")
            build_ok, output = await asyncio.to_thread(backend_build, app_dir)
            if build_ok:
                await self.step(f"Backend compiles cleanly — {output}")
                break
            await self.step(f"Backend build failed: {_backend_error(output)}")
            if attempt > settings.backend_build_retries:
                break
            await self.step(f"Auto-fixing backend/main.py from the error (self-heal {attempt})...")
            backend = await self._repair(req, backend, output)

        if not build_ok:
            await self.step("Auto-fix exhausted — falling back to a minimal FastAPI scaffold")
            backend = _fallback_backend(self.project.name)
            write_files(self.project, {"backend/main.py": backend}, ensure_index=False)
            used_fallback = True

        await self.preview("backend/main.py", backend)
        await self.step("wrote backend/main.py + backend/requirements.txt")

        return {
            "files": ["backend/main.py", "backend/requirements.txt"],
            "backend": "FastAPI (backend/main.py, compile-verified)",
            "backend_fallback": used_fallback,
        }

    async def _generate(self, req: str) -> str:
        arch_summary = _arch_text(self.project.architecture or {})
        prompt = (
            f"Application name: {self.project.name}\n"
            f"Requirement (implement the matching API precisely):\n{req}\n\n"
            f"Architecture context: {arch_summary}\n\n"
            "Generate the complete FastAPI main.py now."
        )
        raw = await asyncio.to_thread(
            nvidia.complete_code,
            prompt,
            system=BACKEND_SYSTEM,
            model=settings.codegen_model,
            max_tokens=4096,
            temperature=0.3,
        )
        return _strip_fences(raw)

    async def _repair(self, req: str, code: str, build_output: str) -> str:
        prompt = (
            f'The backend/main.py for "{self.project.name}" fails to build.\n\n'
            f"ERROR:\n{build_output[-1800:]}\n\n"
            f"CURRENT main.py:\n{code[:9000]}\n\n"
            "Return the COMPLETE corrected main.py (the whole file). Keep the same "
            "endpoints and requirement. Fix the error above."
        )
        raw = await asyncio.to_thread(
            nvidia.complete_code,
            prompt,
            system=BACKEND_FIX_SYSTEM,
            model=settings.codegen_model,
            max_tokens=4096,
            temperature=0.2,
        )
        fixed = _strip_fences(raw)
        return fixed if len(fixed) > 80 else code


class DevOpsAgent(BaseAgent):
    """Generates deployment artifacts (Dockerfile, docker-compose).

    The user-facing README is authored by the Frontend agent (it owns the app code
    and tailors the usage guide to it), so DevOps does not write README.md.
    """

    stage = Stage.devops
    title = "DevOps Agent"

    async def execute(self) -> Dict[str, Any]:
        await self.step("Generating deployment artifacts (Dockerfile, compose)...")

        files = {
            "backend/Dockerfile": _dockerfile(),
            "backend/.dockerignore": "__pycache__/\n*.pyc\n.env\n.venv/\nnode_modules/\n",
            "docker-compose.yml": _compose(self.project),
        }
        write_files(self.project, files, ensure_index=False)
        for path in files:
            await self.step(f"wrote {path}")
        await self.step("Deployment artifacts ready for ECS/Fargate packaging")

        return {
            "files": list(files.keys()),
            "deployment": "Dockerfile + docker-compose",
        }


def _fallback_backend(name: str) -> str:
    return (
        '"""Minimal FastAPI backend (fallback scaffold)."""\n'
        "from fastapi import FastAPI\n"
        "from fastapi.middleware.cors import CORSMiddleware\n\n"
        f'app = FastAPI(title="{name} API")\n'
        "app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])\n\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'status': 'ok'}\n"
    )


def _dockerfile() -> str:
    return (
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY . .\n"
        "EXPOSE 8000\n"
        'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
    )


def _compose(project) -> str:
    service = _slug(project.name)
    return (
        "version: \"3.9\"\n"
        "services:\n"
        f"  {service}-api:\n"
        "    build: ./backend\n"
        "    ports:\n"
        "      - \"8000:8000\"\n"
        "    restart: unless-stopped\n"
        "    healthcheck:\n"
        "      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\"]\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 3\n"
    )


