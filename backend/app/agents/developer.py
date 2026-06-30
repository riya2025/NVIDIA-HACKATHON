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
import re
from pathlib import Path
from typing import Any, Dict, List

from ..config import settings
from ..local_deploy import (
    APPS_ROOT,
    _slug,
    backend_build,
    control_plane_url,
    npm_install,
    vite_build,
    write_files,
)
from ..models import Stage
from ..nvidia_client import nvidia
from ..react_template import _STYLES_CSS, theme_css
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
    "- A tailored global stylesheet is generated separately to match YOUR markup, so use "
    "clear, consistent, reusable className values on every element (containers, headers, cards, "
    "rows, grids, buttons, inputs, lists, badges/pills, avatars). Prefer these conventional "
    "classes where they fit: app, topbar, nav, container, wrap, card, row, btn, btn-primary, "
    "primary, input, label, field, list, list-item, rec, badge, pill, muted, grid, stat, box, "
    "center, login, avatar, hero. Reuse the same class for the same kind of element so styling "
    "is consistent. Do NOT write inline style objects for things a class should handle "
    "(layout, color, spacing) — rely on className.\n"
    "- Build a polished, real UI: a header/nav, a hero or page heading, content in cards/grids.\n"
    "- USE REAL PHOTOS, not emoji, for avatars/thumbnails/cards. Pull from free, no-key, "
    "deterministic image services and render them in <img> tags:\n"
    "    * People / user avatars / profiles: `https://i.pravatar.cc/300?img=${n}` where n is a "
    "stable number 1-70 derived from the item index (so each person keeps the same face).\n"
    "    * Topical photos (pets, food, products, places, nature, etc.): "
    "`https://loremflickr.com/400/300/<keyword>?lock=${n}` — use a relevant <keyword> (e.g. dog, "
    "cat, pizza) and a stable n for a consistent image.\n"
    "    * Generic fallback image: `https://picsum.photos/seed/${seed}/400/300`.\n"
    "  ALWAYS add an onError handler on every <img> that swaps to a safe fallback "
    "(e.g. a picsum URL or a colored initials block) so a failed load never shows a broken icon. "
    "Give images a className and size them with CSS (object-fit: cover, rounded). Emoji are fine "
    "only as small inline accents in text/buttons, never as the main avatar/photo.\n"
    "- Persist data in localStorage so the app ALWAYS works with no backend.\n"
    "- A FastAPI backend MAY be reachable at window.API_BASE (a string set on the page). "
    "You may optionally fetch from it (e.g. `fetch(window.API_BASE + '/health')`) when "
    "window.API_BASE is set, but you MUST wrap such calls in try/catch and fall back to "
    "localStorage so the app fully works whether or not the API responds.\n"
    "- Implement EXACTLY the features in the requirement — no generic placeholder app.\n"
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

CSS_SYSTEM = (
    "You are a world-class product designer and CSS engineer. You write the complete "
    "contents of a single global stylesheet `src/styles.css` for a React app. "
    "Output ONLY raw CSS — no markdown fences, no prose, no explanations, no <style> tag.\n"
    "GOAL: a beautiful, modern, PRODUCTION-READY design that looks like a polished SaaS / "
    "consumer product (think Linear, Vercel, Stripe, Airbnb quality).\n"
    "HARD REQUIREMENTS:\n"
    "- Style the EXACT className values used by the provided App.jsx. Every class the JSX "
    "uses must have matching, intentional styles (do not invent unused classes, do not "
    "miss used ones).\n"
    "- Use a cohesive color system via CSS custom properties in :root (a primary accent + a "
    "second accent for gradients, background, surface, text, muted, border, success/danger). "
    "Use rich, tasteful color — gradients (linear/radial), not flat grey.\n"
    "- Give the page a gorgeous background (subtle multi-stop radial 'aurora' gradients or a "
    "soft mesh), not a plain solid color.\n"
    "- Cards/surfaces: soft rounded corners (12-20px), layered shadows, subtle 1px borders, "
    "optional glassmorphism (backdrop-filter: blur). Buttons: gradient or solid accent, hover "
    "lift + shadow, smooth transitions. Inputs: clear focus ring using the accent.\n"
    "- Typography: a clean system font stack (you MAY add ONE Google Fonts @import at the very "
    "top, e.g. Inter/Poppins). Strong heading hierarchy, comfortable line-height.\n"
    "- Imagery: use CSS gradients and (optionally) inline SVG data-URI backgrounds for texture. "
    "The app uses REAL photos in <img> tags (from i.pravatar.cc / loremflickr.com / "
    "picsum.photos), so style image avatars/thumbnails well: fixed size, border-radius, "
    "`object-fit: cover`, a subtle gradient ring/border, and a soft shadow. Provide a tasteful "
    "look for broken/loading images too (background gradient placeholder).\n"
    "- Fully responsive: mobile-first, use clamp() for fluid spacing/type, flex/grid, and "
    "@media breakpoints. Include `*{box-sizing:border-box}`, reset body margin, `img{max-width:100%}`, "
    "and guard horizontal overflow.\n"
    "- Add small, tasteful motion: transitions on interactive elements and 1-2 subtle keyframe "
    "animations (e.g. fade/slide-in for cards). Keep it elegant, never gaudy.\n"
    "- Respect prefers-reduced-motion. Decent color contrast for accessibility.\n"
    "Return the FULL stylesheet, self-contained and valid CSS only."
)

# Sets window.API_BASE at runtime. On a Vercel build Vite replaces
# %VITE_API_BASE% with the env var (point it at the Render backend URL); if it's
# not replaced (local/docker), the value keeps whatever our runtime injector set
# (or stays empty, so the app falls back to localStorage).
API_BASE_SNIPPET = (
    "    <script>(function(){var b=\"%VITE_API_BASE%\";"
    "window.API_BASE=(b&&b.indexOf(\"%\")!==0)?b:(window.API_BASE||\"\");})();</script>\n"
)


def _error_beacon_snippet(project_id: str) -> str:
    """Inline script that reports browser JS crashes (window.onerror +
    unhandledrejection) to the control plane. This is what makes a real frontend
    error flow into Monitoring -> RCA -> Self-Healing (orchestrator.report_client_error).
    Plain (non-module) inline script so Vite preserves it verbatim into dist/."""
    cp = json.dumps(control_plane_url())
    pid = json.dumps(project_id)
    return (
        f"    <script>(function(){{var U={cp},P={pid};"
        "function s(m,k){try{fetch(U+'/api/projects/'+P+'/client-error',{method:'POST',"
        "headers:{'Content-Type':'application/json'},keepalive:true,"
        "body:JSON.stringify({message:String(m||'error'),stack:String(k||'')})});}catch(e){}}"
        "window.addEventListener('error',function(e){s(e.message,(e.error&&e.error.stack)||'');});"
        "window.addEventListener('unhandledrejection',function(e){"
        "s('unhandledrejection: '+((e.reason&&e.reason.message)||e.reason),(e.reason&&e.reason.stack)||'');});"
        "})();</script>\n"
    )

# Minimal reset only — the full, theme-driven design system lives in
# react_template._STYLES_CSS (concatenated right after this) and theme_css.
_BASE_CSS = """
:root{color-scheme:dark}
*,*::before,*::after{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0}
img,svg,video{max-width:100%;height:auto}
img.preview{border-radius:12px;margin-top:10px}
"""

def _fallback_css(project) -> str:
    """The deterministic, build-safe design system. Used as the scaffold default
    and as the safety net when LLM-authored CSS comes back empty/too small."""
    return _BASE_CSS + "\n" + _STYLES_CSS + "\n" + theme_css(project.name)


def _class_names(app_code: str) -> List[str]:
    """Pull every CSS class the JSX actually uses so the CSS model can target the
    real markup (incl. classes inside `className={cond ? 'a b' : 'c'}` expressions)."""
    classes: set[str] = set()
    for m in re.finditer(r'className\s*=\s*["\']([^"\']+)["\']', app_code):
        classes.update(m.group(1).split())
    for m in re.finditer(r"className\s*=\s*\{([^}]*)\}", app_code):
        for q in re.finditer(r"""["']([^"']+)["']""", m.group(1)):
            for tok in q.group(1).split():
                if re.match(r"^[A-Za-z][\w-]*$", tok):
                    classes.add(tok)
    return sorted(classes)


def _css_looks_valid(css: str) -> bool:
    """Cheap sanity gate before trusting model-authored CSS: non-trivial size,
    several rules, balanced braces (catches truncation), and no leaked HTML."""
    c = (css or "").strip()
    if len(c) < 200:
        return False
    if c.count("{") < 5 or c.count("{") != c.count("}"):
        return False
    if c[:80].lstrip().startswith("<"):
        return False
    return True


def _wants_map(project) -> bool:
    text = (project.description or "") + " " + _arch_text(project.architecture or {})
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
        f"{API_BASE_SNIPPET}"
        f"{_error_beacon_snippet(project.id)}"
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
        "src/styles.css": _fallback_css(project),
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
    "Enable permissive CORS. Implement EXACTLY the endpoints implied by the "
    "requirement. For email/2FA, generate a 6-digit code and RETURN it in the response "
    "(dev mode) with a comment that production would email it. Include a /health "
    "endpoint. Expose the app as a module-level `app = FastAPI(...)` so it runs with "
    "`uvicorn main:app`.\n"
    "PERSISTENCE: a helper module `db` is already provided in the same folder. At startup "
    "call `db.init_db()`. Persist data with `db.add_record(collection, data_dict)`, "
    "`db.list_records(collection)`, `db.update_record(id, data_dict)` and "
    "`db.delete_record(id)` (each record dict includes its 'id'). You may ALSO mount the "
    "ready-made generic CRUD router with `app.include_router(db.router)`. Use `db` for "
    "storage so data survives restarts and persists to PostgreSQL (Supabase/Render) in "
    "production. Do NOT create your own engine or import sqlalchemy/sqlite3 directly — use `db`.\n"
    "IMPORTANT: import ONLY the Python standard library plus fastapi, pydantic, starlette "
    "and the local `db` module. Do NOT import other third-party packages such as "
    "jose/python-jose, passlib, bcrypt, requests, etc. — they are not installed and will "
    "break the build. The file MUST be complete and compile (every brace/paren/bracket "
    "closed) — never stop mid-statement."
)

BACKEND_FIX_SYSTEM = (
    "You are a senior backend engineer fixing a Python/FastAPI error in a single main.py. "
    "Output ONLY the corrected, complete main.py — no markdown fences, no prose. Use only "
    "the standard library plus fastapi/pydantic/starlette and the local `db` module "
    "(db.init_db / db.add_record / db.list_records / db.update_record / db.delete_record / "
    "db.router) for persistence; do NOT import sqlalchemy/sqlite3 directly. Expose a "
    "module-level `app = FastAPI(...)`; the file MUST be complete and compile (no missing "
    "imports, no undefined names, every bracket closed) — never stop mid-statement."
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

        # 2b) Let the model author a tailored, production-grade stylesheet for THIS
        # app (matched to the exact class names it just used). Falls back to the
        # built-in design system if the generated CSS is junk/truncated.
        await self.step("Designing a tailored stylesheet (src/styles.css) with the LLM...")
        css = await self._generate_css(req, app_code)
        use_llm_css = _css_looks_valid(css)
        write_files(
            self.project,
            {"src/styles.css": css if use_llm_css else _fallback_css(self.project)},
            ensure_index=False,
        )
        await self.step(
            f"Tailored stylesheet generated ({len(css):,} chars)" if use_llm_css
            else "LLM CSS was insufficient — using the built-in design system"
        )

        # 3) Install the toolchain (reused across build retries / heals).
        await self.step("Installing dependencies (react, react-router-dom, vite)...")
        deps_ok = await asyncio.to_thread(npm_install, app_dir)
        if not deps_ok:
            # No generic fallback app: write the REAL generated source and skip
            # the build gate. The Vite scaffold (index.html -> /src/main.jsx) is
            # already on disk, so the app is what actually ships.
            await self.step("npm unavailable — writing the generated React source (build gate skipped)")
            write_files(self.project, {"src/App.jsx": app_code}, ensure_index=False)
            await self.preview("frontend (src/App.jsx)", app_code)
            return {
                "files": ["index.html", "package.json", "vite.config.js", "src/main.jsx", "src/App.jsx", "src/styles.css"],
                "frontend": "React (Vite multi-file project; build gate skipped, npm unavailable)",
                "frontend_fallback": False,
            }

        # 4) Real compile gate + auto-fix loop (code self-healing at build time).
        # Re-assert the Vite scaffold (esp. index.html -> /src/main.jsx) so a
        # stale page can never become the build entry point and shadow App.jsx.
        # The scaffold rewrites styles.css to the fallback, so restore the
        # model-authored stylesheet right after.
        write_files(self.project, _vite_scaffold(self.project), ensure_index=False)
        if use_llm_css:
            write_files(self.project, {"src/styles.css": css}, ensure_index=False)
        build_ok = False
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

        # 5a) If the build never passed AND we used model CSS, rule out the
        # stylesheet by reverting to the safe design system and rebuilding once.
        if not build_ok and use_llm_css:
            await self.step("Build still red — reverting to the safe design-system CSS to rule it out...")
            write_files(self.project, {"src/styles.css": _fallback_css(self.project)}, ensure_index=False)
            build_ok, _ = await asyncio.to_thread(vite_build, app_dir)
            if build_ok:
                use_llm_css = False
                await self.step("Reverting CSS fixed the build — the generated stylesheet was the issue.")

        # 5b) Always ship the REAL generated app — never a generic template.
        if not build_ok:
            await self.step(
                "Build gate did not fully pass; shipping the generated React source as-is "
                "(no generic fallback template)."
            )

        await self.preview("frontend (src/App.jsx)", app_code)
        await self.step(f"wrote React (Vite) project -> {app_dir}")
        css_kind = "LLM-tailored" if use_llm_css else "design-system"
        return {
            "files": ["index.html", "package.json", "vite.config.js", "src/main.jsx", "src/App.jsx", "src/styles.css"],
            "frontend": (f"React (Vite multi-file project, build-verified; {css_kind} CSS)" if build_ok
                         else f"React (Vite multi-file project; build gate did not pass; {css_kind} CSS)"),
            "frontend_fallback": False,
        }

    async def _generate_css(self, req: str, app_code: str) -> str:
        """Author a bespoke, production-grade src/styles.css tailored to the exact
        class names used by the just-generated App.jsx."""
        classes = _class_names(app_code)
        class_hint = ", ".join(classes) if classes else "(infer the classes from the JSX below)"
        prompt = (
            f"Application name: {self.project.name}\n"
            f"What the app is / does:\n{req[:700]}\n\n"
            "Style EXACTLY these CSS class names the React app uses (every one must be "
            f"styled, intentionally):\n{class_hint}\n\n"
            "Here is the actual src/App.jsx so you can match its structure precisely:\n"
            "-----\n"
            f"{app_code[:7000]}\n"
            "-----\n"
            "Now output the COMPLETE src/styles.css — beautiful, elegant, production-ready, "
            "colorful, responsive. CSS only."
        )
        raw = await asyncio.to_thread(
            nvidia.complete_code,
            prompt,
            system=CSS_SYSTEM,
            model=settings.codegen_model,
            max_tokens=8192,
            temperature=0.6,
        )
        return _strip_fences(raw)

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
            # Generous budget so a full single-file React app is never truncated
            # mid-file (truncation -> "Unexpected end of file" build failure).
            max_tokens=8192,
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
            max_tokens=8192,
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

        # requirements.txt + db.py are fixed boilerplate; write them up front.
        # SQLAlchemy + psycopg give the container real PostgreSQL (Supabase/Render)
        # persistence; db.py falls back to stdlib sqlite3 if they're unavailable.
        write_files(
            self.project,
            {
                "backend/requirements.txt": (
                    "fastapi==0.138.0\nuvicorn[standard]==0.49.0\n"
                    "pydantic==2.11.0\npython-multipart==0.0.9\n"
                    "SQLAlchemy==2.0.36\npsycopg[binary]==3.2.3\n"
                ),
                "backend/db.py": _DB_PY,
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
        await self.step("wrote backend/main.py + backend/requirements.txt + backend/db.py (Postgres/SQLite persistence)")

        return {
            "files": ["backend/main.py", "backend/requirements.txt", "backend/db.py"],
            "backend": "FastAPI + SQLAlchemy persistence (backend/main.py, compile-verified)",
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
    """Generates deployment artifacts (Dockerfile, docker-compose, README)."""

    stage = Stage.devops
    title = "DevOps Agent"

    async def execute(self) -> Dict[str, Any]:
        from .. import docker_deploy

        arch_summary = _arch_text(self.project.architecture or {})

        await self.step("Generating full-stack Docker artifacts (compose, Dockerfiles, README)...")

        # Runnable full-stack artifacts (nginx frontend + uvicorn backend).
        docker_files = docker_deploy.write_docker_artifacts(self.project)
        # README is owned by the DevOps agent.
        write_files(self.project, {"README.md": _readme(self.project, arch_summary)}, ensure_index=False)

        files = docker_files + ["README.md"]
        for path in files:
            await self.step(f"wrote {path}")
        await self.step("Deployment artifacts ready (docker compose up --build)")

        return {
            "files": files,
            "deployment": "docker-compose + frontend/backend Dockerfiles + README",
        }


def _fallback_backend(name: str) -> str:
    return (
        '"""Minimal FastAPI backend with persistent generic store (fallback scaffold)."""\n'
        "from fastapi import FastAPI\n"
        "from fastapi.middleware.cors import CORSMiddleware\n"
        "import db\n\n"
        f'app = FastAPI(title="{name} API")\n'
        "app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])\n"
        "db.init_db()\n"
        "if db.router is not None:\n"
        "    app.include_router(db.router)\n\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'status': 'ok', 'db': db.backend_name()}\n"
    )


# Deterministic persistence module written into every generated backend. Uses
# SQLAlchemy + DATABASE_URL (PostgreSQL on Supabase/Render) when available, and
# transparently falls back to the stdlib sqlite3 module so the app always runs.
_DB_PY = r'''"""Persistence layer for the generated app.

Stores records in PostgreSQL when DATABASE_URL is set (Supabase / Render), else
in a local SQLite file. If SQLAlchemy/psycopg aren't installed, falls back to
the stdlib sqlite3 module so the app still runs anywhere. Exposes a generic CRUD
router at /api/store/{collection} plus helper functions for custom endpoints.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def _normalize(url: str) -> str:
    # SQLAlchemy needs the psycopg v3 driver prefix.
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


_BACKEND = None  # "sqlalchemy" | "sqlite3"
try:
    from sqlalchemy import (Column, Float, MetaData, String, Table, Text,
                            create_engine, delete, insert, select, update)

    _engine_url = _normalize(DATABASE_URL) if DATABASE_URL else "sqlite:///app.db"
    _engine = create_engine(_engine_url, future=True, pool_pre_ping=True)
    _meta = MetaData()
    _records = Table(
        "records", _meta,
        Column("id", String, primary_key=True),
        Column("collection", String, index=True),
        Column("data", Text),
        Column("created_at", Float),
    )
    _BACKEND = "sqlalchemy"
except Exception:  # noqa: BLE001
    import sqlite3

    _conn = sqlite3.connect("app.db", check_same_thread=False)
    _BACKEND = "sqlite3"


def backend_name() -> str:
    if _BACKEND == "sqlalchemy":
        return "postgres" if str(_engine.url).startswith("postgresql") else "sqlite"
    return "sqlite3-stdlib"


def init_db() -> None:
    global _engine
    if _BACKEND == "sqlalchemy":
        try:
            # Verify the configured DB is actually reachable before using it.
            with _engine.connect():
                pass
            _meta.create_all(_engine)
        except Exception:  # noqa: BLE001
            # DATABASE_URL set but unreachable (or no DB at all): fall back to a
            # local SQLite file so the app ALWAYS runs — no hard dependency on
            # the database URL. Data persists locally instead.
            _engine = create_engine("sqlite:///app.db", future=True, pool_pre_ping=True)
            _meta.create_all(_engine)
    else:
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS records "
            "(id TEXT PRIMARY KEY, collection TEXT, data TEXT, created_at REAL)"
        )
        _conn.commit()


def _row(rid: str, collection: str, data: str, created_at: float) -> Dict[str, Any]:
    out = {"id": rid, "collection": collection, "created_at": created_at}
    try:
        out.update(json.loads(data))
    except Exception:  # noqa: BLE001
        out["data"] = data
    return out


def list_records(collection: str) -> List[Dict[str, Any]]:
    if _BACKEND == "sqlalchemy":
        with _engine.begin() as c:
            rows = c.execute(select(_records).where(_records.c.collection == collection)).fetchall()
            return [_row(r.id, r.collection, r.data, r.created_at) for r in rows]
    cur = _conn.execute(
        "SELECT id, collection, data, created_at FROM records WHERE collection=?", (collection,)
    )
    return [_row(*r) for r in cur.fetchall()]


def add_record(collection: str, data: Dict[str, Any]) -> Dict[str, Any]:
    rid = uuid.uuid4().hex[:12]
    ts = time.time()
    payload = json.dumps(data)
    if _BACKEND == "sqlalchemy":
        with _engine.begin() as c:
            c.execute(insert(_records).values(id=rid, collection=collection, data=payload, created_at=ts))
    else:
        _conn.execute("INSERT INTO records VALUES (?,?,?,?)", (rid, collection, payload, ts))
        _conn.commit()
    return _row(rid, collection, payload, ts)


def update_record(rid: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = json.dumps(data)
    if _BACKEND == "sqlalchemy":
        with _engine.begin() as c:
            res = c.execute(update(_records).where(_records.c.id == rid).values(data=payload))
            if res.rowcount == 0:
                return None
            row = c.execute(select(_records).where(_records.c.id == rid)).first()
            return _row(row.id, row.collection, row.data, row.created_at)
    cur = _conn.execute("UPDATE records SET data=? WHERE id=?", (payload, rid))
    _conn.commit()
    if cur.rowcount == 0:
        return None
    r = _conn.execute(
        "SELECT id, collection, data, created_at FROM records WHERE id=?", (rid,)
    ).fetchone()
    return _row(*r) if r else None


def delete_record(rid: str) -> bool:
    if _BACKEND == "sqlalchemy":
        with _engine.begin() as c:
            return c.execute(delete(_records).where(_records.c.id == rid)).rowcount > 0
    cur = _conn.execute("DELETE FROM records WHERE id=?", (rid,))
    _conn.commit()
    return cur.rowcount > 0


# Generic CRUD router (mounted by main.py). Optional: helper functions above can
# back custom endpoints instead.
try:
    from fastapi import APIRouter
    from pydantic import BaseModel

    router = APIRouter(prefix="/api/store", tags=["store"])

    class RecordIn(BaseModel):
        data: Dict[str, Any] = {}

    @router.get("/{collection}")
    def _list(collection: str):
        return list_records(collection)

    @router.post("/{collection}")
    def _create(collection: str, body: RecordIn):
        return add_record(collection, body.data)

    @router.put("/{collection}/{rid}")
    def _update(collection: str, rid: str, body: RecordIn):
        rec = update_record(rid, body.data)
        return rec if rec is not None else {"error": "not found"}

    @router.delete("/{collection}/{rid}")
    def _delete(collection: str, rid: str):
        return {"deleted": delete_record(rid)}
except Exception:  # noqa: BLE001
    router = None
'''


def _readme(project, arch_summary: str) -> str:
    return (
        f"# {project.name}\n\n"
        f"{project.description}\n\n"
        "_Generated by AI Foundry (NVIDIA Nemotron)._\n\n"
        f"**Architecture:** {arch_summary}\n\n"
        "## Run the full stack with Docker\n"
        "```bash\n"
        "docker compose up --build\n"
        "```\n"
        "- Frontend (nginx, Vite build): http://localhost:${FRONTEND_PORT}\n"
        "- Backend (FastAPI / uvicorn): http://localhost:${BACKEND_PORT} (see `/docs`)\n\n"
        "Host ports are written to `.env` by the platform when it brings the stack up.\n\n"
        "## Run the backend directly (without Docker)\n"
        "```bash\n"
        "cd backend\n"
        "pip install -r requirements.txt\n"
        "uvicorn main:app --reload\n"
        "```\n"
        "The API runs at http://localhost:8000 (see `/docs` for Swagger UI).\n\n"
        "## Deploy to Render\n"
        "```bash\n"
        "# Launch the render.yaml Blueprint (web service + Postgres) from the\n"
        "# Render dashboard, or build the image locally:\n"
        "cd backend\n"
        "docker build -t app .\n"
        "```\n"
    )
