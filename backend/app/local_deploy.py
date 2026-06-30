"""Real local deployment for generated apps.

The Developer agent writes a self-contained `index.html` for the project, and the
Deployment agent serves it on a free local port via a child `http.server` process.
This makes `deploy_url` a real, openable URL. The same start/stop hooks power the
self-healing demo: killing the service really stops the process; healing restarts it.

Phase 2 (AWS ECS/Fargate via boto3) can replace `start`/`stop` without touching
the agents that call them.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .logging_config import log

_NPM = shutil.which("npm") or shutil.which("npm.cmd") or "npm.cmd"

APPS_ROOT = Path(__file__).resolve().parent.parent / "generated_apps"

# project_id -> {"port": int, "proc": Popen, "dir": Path}
_RUNNING: Dict[str, Dict[str, Any]] = {}
# project_id -> {"port": int, "proc": Popen, "dir": Path} for the live FastAPI backend
_RUNNING_API: Dict[str, Dict[str, Any]] = {}


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or "app"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build_app(project, page_html: Optional[str] = None) -> Path:
    """Write the generated app's index.html and return its directory.

    If `page_html` is provided it is used as-is; otherwise the minimal Vite entry
    (which loads the real generated `src/main.jsx`) is written. There is NO
    generic placeholder app — we only ever serve the actual generated source.
    """
    app_dir = APPS_ROOT / _slug(project.name)
    app_dir.mkdir(parents=True, exist_ok=True)
    page = page_html if (page_html and "<" in page_html) else _vite_entry_html(project)
    (app_dir / "index.html").write_text(page, encoding="utf-8")
    return app_dir


def write_files(project, files: Dict[str, str], ensure_index: bool = True) -> Path:
    """Write a multi-file generated project to disk and return its directory.

    `files` maps relative path -> content. When `ensure_index` is True, guarantees
    a working `index.html` exists at the root (so the served app always opens),
    falling back to the interactive template if codegen didn't produce one.

    Concurrency: the Frontend/Backend/DevOps agents write in parallel. Only the
    Frontend agent owns `index.html`, so the others pass `ensure_index=False` to
    avoid racing in a premature fallback page.
    """
    app_dir = APPS_ROOT / _slug(project.name)
    app_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        if not content:
            continue
        dest = app_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    if ensure_index:
        index = app_dir / "index.html"
        # Only guarantee a *correct Vite entry* exists — never a generic app.
        if not index.exists() or 'src="/src/main.jsx"' not in index.read_text(encoding="utf-8", errors="ignore"):
            index.write_text(_vite_entry_html(project), encoding="utf-8")
    return app_dir


def control_plane_url() -> str:
    """Browser-reachable base URL of THIS control plane, for client-error beacons.

    Generated apps run on a different port/container (and may be opened from a
    phone on the LAN), so a bare `localhost` beacon target would hit the wrong
    machine. When `public_api_base` points at localhost we swap in this machine's
    LAN IP (keeping the configured port) so the beacon reaches us from anywhere
    on the network.
    """
    from urllib.parse import urlparse

    from .config import settings

    base = settings.public_api_base.rstrip("/")
    try:
        p = urlparse(base)
        host = p.hostname or "localhost"
        if host in ("localhost", "127.0.0.1", "0.0.0.0"):
            from . import docker_deploy  # deferred (avoids import cycle)

            lan = docker_deploy._host()
            port = f":{p.port}" if p.port else ""
            return f"{p.scheme}://{lan}{port}"
    except Exception:  # noqa: BLE001
        pass
    return base


def _vite_entry_html(project) -> str:
    """Minimal, correct Vite entry page that loads the REAL generated app
    (`src/main.jsx` -> `src/App.jsx`). This is the only HTML we ever fall back
    to: there is no generic placeholder/template app anymore."""
    title = html.escape(project.name)
    api_base = (
        '<script>(function(){var b="%VITE_API_BASE%";'
        'window.API_BASE=(b&&b.indexOf("%")!==0)?b:(window.API_BASE||"");})();</script>'
    )
    cp = json.dumps(control_plane_url())
    pid = json.dumps(project.id)
    beacon = (
        f"<script>(function(){{var U={cp},P={pid};"
        "function s(m,k){try{fetch(U+'/api/projects/'+P+'/client-error',{method:'POST',"
        "headers:{'Content-Type':'application/json'},keepalive:true,"
        "body:JSON.stringify({message:String(m||'error'),stack:String(k||'')})});}catch(e){}}"
        "window.addEventListener('error',function(e){s(e.message,(e.error&&e.error.stack)||'');});"
        "window.addEventListener('unhandledrejection',function(e){"
        "s('unhandledrejection: '+((e.reason&&e.reason.message)||e.reason),(e.reason&&e.reason.stack)||'');});"
        "})();</script>"
    )
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n'
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"    <title>{title}</title>\n"
        f"    {api_base}\n"
        f"    {beacon}\n"
        '  </head>\n  <body>\n    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.jsx"></script>\n'
        "  </body>\n</html>\n"
    )


def npm_install(app_dir: Path) -> bool:
    """Install node deps for a generated React project. Returns success.

    Reuses an existing node_modules (so build-gate retries and self-heals don't
    repeatedly re-download the same Vite/React toolchain).
    """
    if (app_dir / "node_modules").exists():
        return True
    if not (shutil.which("npm") or shutil.which("npm.cmd")):
        log.bind(agent="developer").warning("npm not found on PATH; cannot install node deps")
        return False
    try:
        result = subprocess.run(
            [_NPM, "install", "--no-audit", "--no-fund"],
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            shell=False,
        )
        return result.returncode == 0 and (app_dir / "node_modules").exists()
    except Exception as exc:  # noqa: BLE001
        log.bind(agent="developer").warning("npm install failed: {}", exc)
        return False


def vite_build(app_dir: Path) -> tuple[bool, str]:
    """Run `npm run build` (Vite/Rollup) as a real compile gate.

    Returns (ok, combined_output). This is what catches JSX syntax errors,
    undefined references, bad imports, etc. BEFORE the app is ever served — the
    same signal the Frontend agent feeds back to the model to auto-fix the code.
    """
    try:
        result = subprocess.run(
            [_NPM, "run", "build"],
            cwd=str(app_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            shell=False,
        )
        output = result.stdout or ""
        ok = result.returncode == 0 and (app_dir / "dist").exists()
        return ok, output
    except subprocess.TimeoutExpired:
        return False, "vite build timed out after 240s"
    except Exception as exc:  # noqa: BLE001
        return False, f"vite build error: {exc}"


_IMPORT_CHECK = (
    "import importlib.util;"
    "spec = importlib.util.spec_from_file_location('genmain', 'main.py');"
    "m = importlib.util.module_from_spec(spec);"
    "spec.loader.exec_module(m);"
    "assert hasattr(m, 'app'), 'no FastAPI app object named `app` (uvicorn main:app needs it)'"
)


def backend_build(app_dir: Path) -> tuple[bool, str]:
    """Compile-gate the generated FastAPI backend.

    1) `py_compile` catches syntax errors / truncated files (the main risk).
    2) Best-effort import of backend/main.py with the control-plane interpreter
       catches NameErrors, bad app definitions, missing `app`, etc. A
       ModuleNotFoundError for an uninstalled third-party package is NOT treated
       as a failure (we don't pip-install the generated backend's deps here).

    Returns (ok, output).
    """
    backend_dir = app_dir / "backend"
    main = backend_dir / "main.py"
    if not main.exists():
        return False, "backend/main.py missing"

    try:
        syntax = subprocess.run(
            [sys.executable, "-m", "py_compile", str(main)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, shell=False,
        )
        if syntax.returncode != 0:
            return False, "py_compile failed:\n" + (syntax.stderr or syntax.stdout or "syntax error")

        imp = subprocess.run(
            [sys.executable, "-c", _IMPORT_CHECK],
            cwd=str(backend_dir),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, shell=False,
        )
        if imp.returncode != 0:
            err = (imp.stderr or imp.stdout or "").strip()
            if "ModuleNotFoundError" in err or "No module named" in err:
                # External dependency we don't install in this env — syntax is valid.
                return True, "syntax OK (import skipped: external dependency not installed)"
            return False, "import failed:\n" + err
        return True, "backend OK (py_compile + import + app present)"
    except subprocess.TimeoutExpired:
        return False, "backend build timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"backend build error: {exc}"


def tool_available(module: str) -> bool:
    """True if `python -m <module>` runs (e.g. pytest/black/flake8 installed)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", module, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, shell=False,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def run_black(target: Path) -> tuple[bool, str]:
    """Lint-format-check a Python file/dir with Black (`--check`, no rewrite).

    Returns (ok, output). ok=True means the code is already correctly formatted.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "black", "--check", "--quiet", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, shell=False,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, out.strip()
    except subprocess.TimeoutExpired:
        return False, "black timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"black error: {exc}"


def black_format(target: Path) -> tuple[bool, str]:
    """Auto-format a Python file/dir in place with Black. Returns (ok, output)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "black", "--quiet", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, shell=False,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, out.strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"black error: {exc}"


def run_flake8(target: Path, select: Optional[str] = None) -> tuple[bool, str]:
    """Lint a Python file/dir with flake8. Returns (ok, output).

    When `select` is given (e.g. "E9,F63,F7,F82") flake8 reports ONLY those
    checks — the set that catches real bugs (syntax errors, undefined names,
    duplicate args) rather than cosmetic style. With no `select` it runs a
    lenient style pass (long lines allowed) for an informational warning count.
    """
    cmd = [sys.executable, "-m", "flake8", "--show-source"]
    if select:
        cmd += [f"--select={select}"]
    else:
        # Lenient style pass: ignore line-length + whitespace-before-colon noise
        # that the generated code commonly trips, keep the rest informational.
        cmd += ["--max-line-length=120", "--extend-ignore=E203,W503"]
    cmd.append(str(target))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, shell=False,
        )
        out = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "flake8 timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"flake8 error: {exc}"


def run_pytest(backend_dir: Path) -> tuple[bool, str]:
    """Run pytest in the generated backend dir. Returns (ok, output).

    Tests live next to `main.py` (so `from main import app` resolves under
    pytest's default prepend import mode). `-p no:cacheprovider` keeps the
    generated app tree clean; `-q` keeps the streamed output compact.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header"],
            cwd=str(backend_dir),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=240, shell=False,
        )
        out = ((result.stdout or "") + (result.stderr or "")).strip()
        # pytest exit codes: 0 = all passed, 5 = no tests collected.
        return result.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "pytest timed out after 240s"
    except Exception as exc:  # noqa: BLE001
        return False, f"pytest error: {exc}"


def start_backend(project) -> Optional[str]:
    """Start (or restart) the generated FastAPI backend with uvicorn.

    Runs `uvicorn main:app` from the project's backend/ dir using the
    control-plane interpreter (so fastapi/uvicorn/pydantic are available).
    Returns the live API URL, or None if there's no backend to run.
    """
    stop_backend(project)
    backend_dir = APPS_ROOT / _slug(project.name) / "backend"
    if not (backend_dir / "main.py").exists():
        return None
    port = _free_port()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(backend_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.bind(agent="deployment").warning("could not start backend uvicorn: {}", exc)
        return None
    _RUNNING_API[project.id] = {"port": port, "proc": proc, "dir": backend_dir}
    _wait_ready(port, timeout=20)
    url = f"http://localhost:{port}/"
    log.bind(agent="deployment").info("Backend API up at {}", url)
    return url


def stop_backend(project) -> None:
    """Stop the project's live FastAPI backend if running."""
    entry = _RUNNING_API.pop(project.id, None)
    if not entry:
        return
    _kill_proc(entry["proc"])
    log.bind(agent="deployment").info("Backend API stopped (port {})", entry["port"])


def current_api_url(project) -> Optional[str]:
    entry = _RUNNING_API.get(project.id)
    return f"http://localhost:{entry['port']}/" if entry else None


def _inject_api_base(app_dir: Path, api_url: str) -> None:
    """Make the live backend reachable from the browser by setting
    window.API_BASE in the served index.html (runs before the React bundle)."""
    idx = app_dir / "index.html"
    if not idx.exists():
        return
    try:
        text = idx.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"<script>window\.API_BASE=.*?</script>\s*", "", text, flags=re.DOTALL)
        snippet = f"<script>window.API_BASE={json.dumps(api_url.rstrip('/'))};</script>"
        if "<head>" in text:
            text = text.replace("<head>", "<head>\n    " + snippet, 1)
        else:
            text = snippet + "\n" + text
        idx.write_text(text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.bind(agent="deployment").warning("could not inject API_BASE: {}", exc)


def start(project) -> str:
    """Start (or restart) the project's local servers. Returns the frontend URL.

    Brings up the generated FastAPI backend first (so its URL can be injected as
    window.API_BASE), then serves the frontend: React/Vite projects via the Vite
    dev server; plain static bundles via http.server.
    """
    stop(project)
    app_dir = APPS_ROOT / _slug(project.name)

    # Live backend first, so the frontend can be pointed at it.
    api_url = start_backend(project)
    if api_url:
        _inject_api_base(app_dir, api_url)

    port = _free_port()

    is_node = (app_dir / "package.json").exists() and (app_dir / "node_modules").exists()
    if is_node:
        proc = subprocess.Popen(
            [_NPM, "run", "dev", "--", "--port", str(port), "--strictPort",
             "--host", "127.0.0.1"],
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        _RUNNING[project.id] = {"port": port, "proc": proc, "dir": app_dir, "kind": "node"}
        _wait_ready(port, timeout=30)
        url = f"http://localhost:{port}/"
        log.bind(agent="deployment").info("Vite dev server up at {}", url)
        return url

    if not (app_dir / "index.html").exists():
        build_app(project)
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
         "--directory", str(app_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _RUNNING[project.id] = {"port": port, "proc": proc, "dir": app_dir, "kind": "static"}
    _wait_ready(port, timeout=10)
    url = f"http://localhost:{port}/"
    log.bind(agent="deployment").info("Local app server up at {}", url)
    return url


def _wait_ready(port: int, timeout: int = 20) -> bool:
    """Block until the port accepts TCP connections (server is up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def _kill_proc(proc) -> None:
    """Kill a child process and its tree (npm/vite/uvicorn spawn children)."""
    try:
        if proc.poll() is not None:
            return
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def stop(project) -> None:
    """Stop the project's local servers (frontend + backend) if running."""
    stop_backend(project)
    entry = _RUNNING.pop(project.id, None)
    if not entry:
        return
    _kill_proc(entry["proc"])
    log.bind(agent="monitoring").warning("App server stopped (port {})", entry["port"])


def current_url(project) -> Optional[str]:
    entry = _RUNNING.get(project.id)
    return f"http://localhost:{entry['port']}/" if entry else None
