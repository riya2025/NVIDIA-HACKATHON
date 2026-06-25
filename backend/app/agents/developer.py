"""Developer Agent: real code generation with NVIDIA Nemotron.

Unlike a fixed template, this agent reads the project's *requirement* (the user's
description, refined by the Architect agent) and asks Nemotron to generate:

  - a tailored, runnable frontend (single-file React app, served live), and
  - a real FastAPI backend (main.py) implementing the described API, plus
    requirements.txt, Dockerfile and a README.

The served app is the generated frontend; the backend is generated as real source
artifacts that the Deployment agent can run / package for AWS (Phase 2).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Dict

from ..config import settings
from ..local_deploy import APPS_ROOT, _slug, working_app_html, write_files
from ..models import Stage
from ..nvidia_client import nvidia
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
    "You are a senior React engineer. Output ONLY JavaScript/JSX (no markdown fences, "
    "no prose, no HTML, no <script> tags, no import/require, and DO NOT call "
    "ReactDOM.render — the host page already mounts <App/>). Your code runs inside a "
    "<script type='text/babel'> where these globals are ALREADY defined for you:\n"
    "  React, ReactDOM\n"
    "  hooks: useState, useEffect, useReducer, useContext, createContext, useRef, useMemo, useCallback\n"
    "  router (react-router-dom v6): HashRouter, Routes, Route, Link, NavLink, useNavigate, Navigate, useParams\n"
    "  exifr  (read GPS from an image File: `const d = await exifr.gps(file)` -> {latitude, longitude} or undefined)\n"
    "  DEFAULT_LOCATION = { lat, lng }  (use this when a photo has no GPS)\n"
    "  api(path, options)  (fetch helper to window.API_BASE; falls back to localStorage on failure)\n"
    "RULES:\n"
    "- Define a top-level component named `App` that returns <HashRouter>...<Routes>...</Routes></HashRouter>.\n"
    "- Use ONLY plain JSX elements (div, button, input, etc.) with className. DO NOT use Material-UI, "
    "Chakra, Bootstrap, or any external component library (they are NOT loaded).\n"
    "- Persist data in localStorage so the app works with no backend.\n"
    "- Implement EXACTLY the features in the requirement — no generic placeholder app.\n"
    "- Reference the CSS classes provided by the host (app, topbar, nav, container, card, btn, "
    "btn-primary, input, label, field, list, list-item, badge, muted, grid) for a clean look.\n"
    "- Write complete, working code with no TODOs and no undefined references.\n"
    "CRITICAL — your output MUST follow EXACTLY this shape (the host renders <App/>):\n"
    "  function App() {\n"
    "    return (\n"
    "      <HashRouter>\n"
    "        {/* topbar + <Routes>...</Routes> implementing the features */}\n"
    "      </HashRouter>\n"
    "    );\n"
    "  }\n"
    "The root component MUST be named exactly `App` (not Main, not Root, not PotholeApp) and "
    "MUST render <HashRouter>. Do NOT use `export`, `export default`, or `import`."
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

_FRONTEND_SKELETON = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>__TITLE__</title>
<script>
/* AI Foundry client-error beacon: reports browser JS crashes to Monitoring/RCA. */
window.__FOUNDRY__ = { base: "__FOUNDRY_BASE__", pid: "__PROJECT_ID__" };
(function(){
  var sent = false;
  function report(message, stack){
    if (sent || !window.__FOUNDRY__.pid) return; sent = true;
    try {
      fetch(window.__FOUNDRY__.base + "/api/projects/" + window.__FOUNDRY__.pid + "/client-error", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: String(message), stack: String(stack || "") })
      });
    } catch (e) {}
  }
  window.addEventListener("error", function(e){
    report(e.message || "script error", (e.error && e.error.stack) || (e.filename + ":" + e.lineno));
  });
  window.addEventListener("unhandledrejection", function(e){
    var r = e.reason || {}; report("unhandledrejection: " + (r.message || r), r.stack || "");
  });
})();
</script>
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone@7/babel.min.js"></script>
<script src="https://unpkg.com/exifr@7.1.3/dist/full.umd.js"></script>
<style>__BASE_CSS__</style>
</head>
<body>
<div id="root"></div>
<script type="text/babel" data-presets="react">
const { useState, useEffect, useReducer, useContext, createContext, useRef, useMemo, useCallback } = React;
const DEFAULT_LOCATION = { lat: 28.6139, lng: 77.2090 };

/* ---- Built-in hash router (react-router v6 compatible subset, no CDN) ---- */
const __RouterCtx = React.createContext({ path: '/', navigate: function(){} });
function __norm(p){ if(p===undefined||p===null) return '/'; p=String(p); if(p.length>1&&p.endsWith('/')) p=p.slice(0,-1); if(!p.startsWith('/')) p='/'+p; return p; }
function __getHash(){ var h=window.location.hash.replace(/^#/,''); if(!h) h='/'; if(!h.startsWith('/')) h='/'+h; return h; }
function HashRouter(props){
  const [path,setPath]=useState(__getHash());
  useEffect(function(){ var on=function(){ setPath(__getHash()); }; window.addEventListener('hashchange',on); return function(){ window.removeEventListener('hashchange',on); }; },[]);
  const navigate=useCallback(function(to){ if(typeof to==='number'){ window.history.go(to); return; } var t=String(to); if(!t.startsWith('/')) t='/'+t; window.location.hash=t; },[]);
  return React.createElement(__RouterCtx.Provider,{ value:{ path:path, navigate:navigate } }, props.children);
}
const BrowserRouter = HashRouter;
function useNavigate(){ return useContext(__RouterCtx).navigate; }
function useLocation(){ return { pathname: useContext(__RouterCtx).path, hash: window.location.hash, search: '' }; }
function useParams(){ return {}; }
function Route(){ return null; }
function Outlet(){ return null; }
function Routes(props){
  const cur=__norm(useContext(__RouterCtx).path);
  let match=null, fallback=null;
  React.Children.forEach(props.children,function(child){
    if(!child||!child.props) return;
    const p=child.props;
    if(p.path==='*'){ fallback=p.element; return; }
    const rp = p.index ? '/' : __norm(p.path);
    if(rp===cur && match===null) match=p.element;
  });
  return match || fallback || null;
}
function Link(props){
  const navigate=useNavigate();
  const to=props.to;
  const rest=Object.assign({},props); delete rest.to; delete rest.children;
  rest.href='#'+__norm(to);
  rest.onClick=function(e){ if(props.onClick) props.onClick(e); e.preventDefault(); navigate(to); };
  return React.createElement('a',rest,props.children);
}
function NavLink(props){
  const path=__norm(useContext(__RouterCtx).path);
  const active=(path===__norm(props.to));
  let cn=props.className;
  if(typeof cn==='function') cn=cn({ isActive:active });
  else if(active) cn=(cn?cn+' ':'')+'active';
  const np=Object.assign({},props,{ className:cn });
  return Link(np);
}
function Navigate(props){
  const navigate=useNavigate();
  useEffect(function(){ navigate(props.to); },[props.to]);
  return null;
}
/* ------------------------------------------------------------------------ */
async function api(path, options) {
  if (!window.API_BASE) throw new Error("no api");
  const res = await fetch(window.API_BASE + path, options);
  if (!res.ok) throw new Error("api " + res.status);
  return res.json();
}
try {
// ===== AI-GENERATED APP CODE START =====
__APP_CODE__
// ===== AI-GENERATED APP CODE END =====
const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
} catch (err) {
  document.getElementById('root').innerHTML =
    '<div class="container"><div class="card"><h2>App failed to start</h2>' +
    '<pre class="muted">' + (err && err.message ? err.message : err) + '</pre></div></div>';
  console.error(err);
  try {
    fetch(window.__FOUNDRY__.base + "/api/projects/" + window.__FOUNDRY__.pid + "/client-error", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: String(err && err.message ? err.message : err), stack: String(err && err.stack || "") })
    });
  } catch (e) {}
}
</script>
</body>
</html>
"""

BACKEND_SYSTEM = (
    "You are a senior backend engineer. You output ONE complete FastAPI application "
    "(main.py) and NOTHING else (no markdown fences, no prose). Use FastAPI + Pydantic. "
    "Enable permissive CORS. Use an in-memory store (or sqlite via sqlite3) so it runs "
    "with zero external services. Implement EXACTLY the endpoints implied by the "
    "requirement. For email/2FA, generate a 6-digit code and RETURN it in the response "
    "(dev mode) with a comment that production would email it. Include a /health "
    "endpoint. Make it runnable with: uvicorn main:app."
)


class DeveloperAgent(BaseAgent):
    stage = Stage.developer
    title = "Developer Agent"

    async def _preview(self, path: str, code: str, lines: int = 14) -> None:
        """Stream a peek at the generated code so the UI shows what's inside."""
        head = "\n".join(code.splitlines()[:lines])
        await self.emit(
            "code_preview",
            f"Generated {path}",
            {"path": path, "preview": head, "chars": len(code)},
        )
        await self.step(f"--- {path} ({len(code):,} chars) ---")
        for ln in head.splitlines():
            self.state.logs.append(ln)
            await self.emit("agent_log", ln)
        await asyncio.sleep(0.2)

    async def execute(self) -> Dict[str, Any]:
        req = self.project.description
        arch = self.project.architecture or {}
        arch_summary = _arch_text(arch)

        await self.step(f"Reading requirement: {req[:90]}...")
        await self.step(f"Target stack (Architect): {arch_summary}")

        frontend_prompt = (
            f"Application name: {self.project.name}\n"
            f"Requirement (implement these features precisely):\n{req}\n\n"
            "Write the React component code (define `App` and any sub-components) now. "
            "Remember: JSX only, no HTML/script tags, no imports, no ReactDOM.render."
        )
        backend_prompt = (
            f"Application name: {self.project.name}\n"
            f"Requirement (implement the matching API precisely):\n{req}\n\n"
            f"Architecture context: {arch_summary}\n\n"
            "Generate the complete FastAPI main.py now."
        )

        await self.step(f"Generating frontend + backend with {settings.codegen_model} (concurrent)...")
        frontend_raw, backend_raw = await asyncio.gather(
            asyncio.to_thread(
                nvidia.complete,
                frontend_prompt,
                system=FRONTEND_SYSTEM,
                model=settings.codegen_model,
                max_tokens=5000,
                temperature=0.4,
                thinking=False,
            ),
            asyncio.to_thread(
                nvidia.complete,
                backend_prompt,
                system=BACKEND_SYSTEM,
                model=settings.codegen_model,
                max_tokens=2500,
                temperature=0.3,
                thinking=False,
            ),
        )

        app_code = _strip_fences(frontend_raw)
        backend = _strip_fences(backend_raw)

        # Salvage common deviations rather than discarding otherwise-good code:
        #  - only HashRouter is a provided global (rewrite BrowserRouter), and
        #  - the inline Babel runtime has no module system, so strip export/import.
        app_code = app_code.replace("BrowserRouter", "HashRouter")
        app_code = re.sub(r"^\s*export\s+default\s+", "", app_code, flags=re.MULTILINE)
        app_code = re.sub(r"^\s*export\s+", "", app_code, flags=re.MULTILINE)

        used_fallback = False
        # The generated code must define an App component and use the router.
        reason = None
        if len(app_code) < 200:
            reason = f"too short ({len(app_code)} chars)"
        elif "App" not in app_code:
            reason = "no `App` component defined"
        elif "HashRouter" not in app_code:
            reason = "no router (HashRouter) found"
        if reason:
            await self.step(f"Frontend codegen incomplete ({reason}) — using fallback app")
            frontend = working_app_html(self.project)
            used_fallback = True
        else:
            frontend = (
                _FRONTEND_SKELETON.replace("__TITLE__", self.project.name)
                .replace("__BASE_CSS__", _BASE_CSS)
                .replace("__FOUNDRY_BASE__", settings.public_api_base)
                .replace("__PROJECT_ID__", self.project.id)
                .replace("__APP_CODE__", app_code)
            )
            await self.step(
                f"Frontend generated ({len(app_code):,} chars of React) — tailored to your prompt"
            )

        if "fastapi" not in backend.lower():
            await self.step("Backend codegen incomplete — writing minimal FastAPI scaffold")
            backend = _fallback_backend(self.project.name)
        else:
            await self.step(f"Backend generated ({len(backend):,} chars) — FastAPI main.py")

        await self._preview("frontend (React App)", app_code if not used_fallback else frontend)
        await self._preview("backend/main.py", backend)

        files = {
            "index.html": frontend,
            "backend/main.py": backend,
            "backend/requirements.txt": (
                "fastapi==0.138.0\nuvicorn[standard]==0.49.0\n"
                "pydantic==2.11.0\npython-multipart==0.0.9\n"
            ),
            "backend/Dockerfile": _dockerfile(),
            "README.md": _readme(self.project, arch_summary),
        }

        # Remove a stale React-template package.json so the served app is the
        # generated single-file frontend (static serve), not an old Vite project.
        stale_pkg = APPS_ROOT / _slug(self.project.name) / "package.json"
        if stale_pkg.exists():
            stale_pkg.unlink()

        app_dir: Path = write_files(self.project, files)
        for path in files:
            await self.step(f"wrote {path}")
        await self.step(f"Project written -> {app_dir}")

        return {
            "files": list(files.keys()),
            "file_count": len(files),
            "frontend": "React (single-file, served live)",
            "backend": "FastAPI (backend/main.py)",
            "frontend_fallback": used_fallback,
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


def _readme(project, arch_summary: str) -> str:
    return (
        f"# {project.name}\n\n"
        f"{project.description}\n\n"
        "_Generated by AI Foundry (NVIDIA Nemotron)._\n\n"
        f"**Architecture:** {arch_summary}\n\n"
        "## Run the frontend\n"
        "Just open `index.html` in a browser (it is served automatically by the platform).\n\n"
        "## Run the backend (FastAPI)\n"
        "```bash\n"
        "cd backend\n"
        "pip install -r requirements.txt\n"
        "uvicorn main:app --reload\n"
        "```\n"
        "The API runs at http://localhost:8000 (see `/docs` for Swagger UI).\n\n"
        "## Deploy to AWS (Phase 2)\n"
        "```bash\n"
        "cd backend\n"
        "docker build -t app .\n"
        "# push to ECR and run on ECS Fargate behind an ALB\n"
        "```\n"
    )
