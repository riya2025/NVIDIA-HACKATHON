"""Generates a real Vite + React Router multi-page project for a given project.

Returns a {relative_path: content} mapping. The app is a working multi-page SPA
(Login -> Home -> Create -> Admin) with client-side state in localStorage and an
optional Leaflet map (loaded via CDN) for location-based apps. Served by running
the Vite dev server (`npm run dev`) on an assigned port.
"""
from __future__ import annotations

import json
from typing import Any, Dict


def _as_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("framework") or value.get("technology") or value.get("tech")
                   or ", ".join(f"{k}: {v}" for k, v in value.items()))
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value) if value is not None else ""


def react_project_files(project) -> Dict[str, str]:
    arch = project.architecture or {}
    name = project.name
    desc = project.description
    use_map = any(
        kw in (desc + " " + _as_text(arch.get("frontend", ""))).lower()
        for kw in ("map", "location", "geo", "pothole", "track", "delivery")
    )
    app_cfg = json.dumps({"name": name, "desc": desc, "useMap": use_map})
    stack = {k: _as_text(arch.get(k)) for k in ("frontend", "backend", "database", "deployment")}

    files: Dict[str, str] = {}

    files["package.json"] = json.dumps(
        {
            "name": _npm_name(name),
            "private": True,
            "version": "0.1.0",
            "type": "module",
            "scripts": {
                "dev": "vite",
                "build": "vite build",
                "preview": "vite preview",
            },
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

    files["vite.config.js"] = (
        "import { defineConfig } from 'vite'\n"
        "import react from '@vitejs/plugin-react'\n\n"
        "export default defineConfig({ plugins: [react()] })\n"
    )

    leaflet_head = (
        '    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />\n'
        '    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>\n'
        if use_map else ""
    )
    files["index.html"] = (
        "<!doctype html>\n<html lang=\"en\">\n  <head>\n"
        "    <meta charset=\"UTF-8\" />\n"
        "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        f"    <title>{_esc(name)}</title>\n"
        f"{leaflet_head}"
        "  </head>\n  <body>\n    <div id=\"root\"></div>\n"
        "    <script type=\"module\" src=\"/src/main.jsx\"></script>\n"
        "  </body>\n</html>\n"
    )

    files["src/main.jsx"] = (
        "import React from 'react'\n"
        "import ReactDOM from 'react-dom/client'\n"
        "import { HashRouter } from 'react-router-dom'\n"
        "import App from './App.jsx'\n"
        "import './styles.css'\n\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(\n"
        "  <React.StrictMode>\n    <HashRouter>\n      <App />\n    </HashRouter>\n"
        "  </React.StrictMode>\n)\n"
    )

    files["src/config.js"] = f"export const APP = {app_cfg}\n"

    files["src/store.js"] = (
        "import { APP } from './config.js'\n"
        "const KEY = 'app_' + APP.name.replace(/\\W+/g, '_')\n"
        "export const STATUSES = ['New', 'In Progress', 'Resolved']\n"
        "export const loadRecords = () => JSON.parse(localStorage.getItem(KEY) || '[]')\n"
        "export const saveRecords = (r) => localStorage.setItem(KEY, JSON.stringify(r))\n"
        "export const getUser = () => localStorage.getItem(KEY + '_user')\n"
        "export const setUser = (u) => localStorage.setItem(KEY + '_user', u)\n"
        "export const clearUser = () => localStorage.removeItem(KEY + '_user')\n"
    )

    files["src/App.jsx"] = _APP_JSX
    files["src/components/Nav.jsx"] = _NAV_JSX
    files["src/pages/Login.jsx"] = _LOGIN_JSX
    files["src/pages/Home.jsx"] = _HOME_JSX
    files["src/pages/Create.jsx"] = _CREATE_JSX if use_map else _CREATE_NOMAP_JSX
    files["src/pages/Admin.jsx"] = _ADMIN_JSX
    files["src/styles.css"] = _STYLES_CSS

    files["README.md"] = _readme(name, desc, stack, use_map)
    return files


def _npm_name(name: str) -> str:
    s = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    return s or "generated-app"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def scaffold_user_guide(use_map: bool) -> str:
    """The accurate 'how to use' walkthrough for the FIXED fallback scaffold.

    The fallback scaffold is always Login -> Home -> Create -> Admin with the same
    controls, so this guide is always correct for it. Used as the README body when
    an app was built from the scaffold (or when LLM guide generation is unavailable
    and the app uses this same structure).
    """
    map_bullet = (
        "- **Location** — click anywhere on the map to drop a pin for this item.\n"
        if use_map else ""
    )
    create_map_step = " (and click the map to set a location)" if use_map else ""
    return f"""## Using the app

After you log in, a top bar with **Home · Create · Admin · Logout** stays visible
on every page. Here's what each screen and button does:

### 1. Login
- Type **any** username and password — it's a demo login, no real account needed.
- Click **Sign in** to enter the app.

### 2. Home — your list
- Shows every item you've added, newest first, with its title, details, who added
  it, and a colored **status** tag (New / In Progress / Resolved).
- Click **+ New** (top-right) to add an item.

### 3. Create — add an item
- **Title** — short name for the item (required).
- **Details** — a longer description (optional).
{map_bullet}- Click **Submit** to save it and jump back to Home.

### 4. Admin — dashboard & management
- The top boxes show live counts: **Total**, **New**, **In Progress**, **Resolved**.
- The table lists every item with two action buttons:
  - **Advance** — moves the status forward: New → In Progress → Resolved (then loops back).
  - **Delete** — removes the item permanently.

Click **Logout** (top-right) any time to return to the Login screen.

## Quick test
1. Log in, then go to **Create** and add an item (title + details{create_map_step}).
2. Open **Home** — your new item shows up in the list.
3. Open **Admin** — the **Total** count went up; click **Advance** and watch the
   status tag change colour.
4. **Refresh** the page — your data is still there (it's saved in the browser)."""


def compose_readme(name: str, desc: str, stack: Dict[str, str], guide_md: str) -> str:
    """Wrap a 'how to use / test' guide (LLM- or scaffold-authored) in the full
    README: title, quick start, the guide, backend check, tech stack, build, deploy.

    `guide_md` is Markdown that should lead with a `## Using the app` heading and
    (ideally) include a `## Quick test` checklist — everything specific to the
    actual generated app.
    """
    rows = "\n".join(f"| {k.title()} | {v or '-'} |" for k, v in stack.items())
    body = (guide_md or "").strip()
    return f"""# {name}

> {desc}

Generated by **AI Foundry** — architecture designed by **NVIDIA Nemotron** via the
NeMo Agent Toolkit.

## Quick start

Prerequisites: **Node.js 18+**.

```bash
cd {_npm_name(name)}
npm install
npm run dev
```

Open the URL Vite prints (usually **http://localhost:5173/**).

> Your data is saved in the browser (localStorage), so the app works fully on its
> own — no backend or sign-up required.

{body}

## Is the backend connected?
A FastAPI service is generated under `backend/`. The UI also works without it, but
to run and check the API:

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000/health** — a `{{"status": "ok"}}` response means the
backend is up. If the frontend talks to the backend, it reads its base URL from
`window.API_BASE`; when that's unset it falls back to local browser storage, so the
app keeps working either way.

## Tech Stack (designed by Nemotron)

| Layer | Choice |
| --- | --- |
{rows}

## Production build

```bash
npm run build      # outputs static assets to dist/
npm run preview    # serve the production build locally
```

## Deployment
Designed for **{stack.get('deployment', 'AWS ECS Fargate')}**. See `deploy/README.md`
for the GitHub Actions workflow that builds the Docker image, pushes it to Amazon
ECR, and deploys to ECS Fargate behind an Application Load Balancer.
"""


def _readme(name: str, desc: str, stack: Dict[str, str], use_map: bool) -> str:
    """Full README for the fixed fallback scaffold (accurate Login/Home/Create/Admin guide)."""
    return compose_readme(name, desc, stack, scaffold_user_guide(use_map))


_APP_JSX = """import { Routes, Route, Navigate } from 'react-router-dom'
import Nav from './components/Nav.jsx'
import Login from './pages/Login.jsx'
import Home from './pages/Home.jsx'
import Create from './pages/Create.jsx'
import Admin from './pages/Admin.jsx'
import { getUser } from './store.js'

function Protected({ children }) {
  return getUser() ? children : <Navigate to="/login" replace />
}

export default function App() {
  const authed = !!getUser()
  return (
    <div>
      {authed && <Nav />}
      <div className="wrap">
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={<Protected><Home /></Protected>} />
          <Route path="/create" element={<Protected><Create /></Protected>} />
          <Route path="/admin" element={<Protected><Admin /></Protected>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </div>
  )
}
"""

_NAV_JSX = """import { NavLink, useNavigate } from 'react-router-dom'
import { APP } from '../config.js'
import { getUser, clearUser } from '../store.js'

export default function Nav() {
  const nav = useNavigate()
  const logout = () => { clearUser(); nav('/login') }
  return (
    <header>
      <div className="brand">{APP.name}<small>Generated &amp; deployed by AI Foundry · NVIDIA Nemotron</small></div>
      <nav>
        <NavLink to="/" end>Home</NavLink>
        <NavLink to="/create">Create</NavLink>
        <NavLink to="/admin">Admin</NavLink>
        <button onClick={logout}>Logout ({getUser()})</button>
      </nav>
    </header>
  )
}
"""

_LOGIN_JSX = """import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { APP } from '../config.js'
import { setUser } from '../store.js'

export default function Login() {
  const [u, setU] = useState('')
  const nav = useNavigate()
  const submit = (e) => { e.preventDefault(); setUser(u || 'demo'); nav('/') }
  return (
    <div className="center">
      <form className="card login" onSubmit={submit}>
        <h1>{APP.name}</h1>
        <p className="muted">{APP.desc}</p>
        <label>Username</label>
        <input value={u} onChange={(e) => setU(e.target.value)} placeholder="any username" />
        <label>Password</label>
        <input type="password" placeholder="any password" />
        <button className="primary" style={{ width: '100%' }}>Sign in</button>
        <div className="muted center-text">Demo login — any credentials work</div>
      </form>
    </div>
  )
}
"""

_HOME_JSX = """import { Link } from 'react-router-dom'
import { loadRecords } from '../store.js'

export default function Home() {
  const records = loadRecords()
  return (
    <div className="card">
      <div className="row">
        <h2>Records</h2>
        <Link className="primary" to="/create">+ New</Link>
      </div>
      {records.length === 0 && <p className="muted">No records yet. Click “+ New”.</p>}
      {records.map((r) => (
        <div className="rec" key={r.id}>
          <div className="t">{r.title}</div>
          <div className="m">{r.details}</div>
          <div className="m">by {r.by} · {r.at}{r.loc ? ` · 📍 ${r.loc.lat.toFixed(4)}, ${r.loc.lng.toFixed(4)}` : ''}</div>
          <span className={'pill ' + r.status.replace(/\\s/g, '')}>{r.status}</span>
        </div>
      ))}
    </div>
  )
}
"""

_CREATE_JSX = """import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { loadRecords, saveRecords, getUser } from '../store.js'

export default function Create() {
  const [title, setTitle] = useState('')
  const [details, setDetails] = useState('')
  const [loc, setLoc] = useState(null)
  const mapRef = useRef(null)
  const nav = useNavigate()

  useEffect(() => {
    if (!window.L || !mapRef.current || mapRef.current._map) return
    const map = window.L.map(mapRef.current).setView([20.5937, 78.9629], 4)
    mapRef.current._map = map
    window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map)
    let marker
    map.on('click', (e) => {
      setLoc({ lat: e.latlng.lat, lng: e.latlng.lng })
      if (marker) marker.remove()
      marker = window.L.marker(e.latlng).addTo(map)
    })
  }, [])

  const submit = (e) => {
    e.preventDefault()
    const recs = loadRecords()
    recs.unshift({ id: Date.now(), title, details, status: 'New', by: getUser(), at: new Date().toLocaleString(), loc })
    saveRecords(recs)
    nav('/')
  }

  return (
    <div className="card">
      <h2>Create a record</h2>
      <form onSubmit={submit}>
        <label>Title</label>
        <input value={title} onChange={(e) => setTitle(e.target.value)} required placeholder="e.g. Large pothole on Main St" />
        <label>Details</label>
        <textarea rows="3" value={details} onChange={(e) => setDetails(e.target.value)} placeholder="Describe it..." />
        <label>Location (click the map)</label>
        <div ref={mapRef} id="map" style={{ height: 260, borderRadius: 10, marginTop: 8 }} />
        <button className="primary">Submit</button>
      </form>
    </div>
  )
}
"""

_CREATE_NOMAP_JSX = """import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { loadRecords, saveRecords, getUser } from '../store.js'

export default function Create() {
  const [title, setTitle] = useState('')
  const [details, setDetails] = useState('')
  const nav = useNavigate()

  const submit = (e) => {
    e.preventDefault()
    const recs = loadRecords()
    recs.unshift({ id: Date.now(), title, details, status: 'New', by: getUser(), at: new Date().toLocaleString() })
    saveRecords(recs)
    nav('/')
  }

  return (
    <div className="card">
      <h2>Create a record</h2>
      <form onSubmit={submit}>
        <label>Title</label>
        <input value={title} onChange={(e) => setTitle(e.target.value)} required placeholder="Title" />
        <label>Details</label>
        <textarea rows="3" value={details} onChange={(e) => setDetails(e.target.value)} placeholder="Describe it..." />
        <button className="primary">Submit</button>
      </form>
    </div>
  )
}
"""

_ADMIN_JSX = """import { useState } from 'react'
import { loadRecords, saveRecords, STATUSES } from '../store.js'

export default function Admin() {
  const [, force] = useState(0)
  const recs = loadRecords()
  const count = (s) => recs.filter((r) => r.status === s).length
  const advance = (id) => {
    const r = loadRecords()
    const x = r.find((o) => o.id === id)
    if (x) { x.status = STATUSES[(STATUSES.indexOf(x.status) + 1) % STATUSES.length]; saveRecords(r); force((n) => n + 1) }
  }
  const del = (id) => { saveRecords(loadRecords().filter((o) => o.id !== id)); force((n) => n + 1) }

  return (
    <div>
      <div className="stat">
        <div className="box"><div className="n">{recs.length}</div><div className="l">Total</div></div>
        {STATUSES.map((s) => (
          <div className="box" key={s}><div className="n">{count(s)}</div><div className="l">{s}</div></div>
        ))}
      </div>
      <div className="card" style={{ marginTop: 20 }}>
        <h2>Admin dashboard</h2>
        <table>
          <thead><tr><th>Title</th><th>Status</th><th>By</th><th>Actions</th></tr></thead>
          <tbody>
            {recs.map((r) => (
              <tr key={r.id}>
                <td>{r.title}</td>
                <td><span className={'pill ' + r.status.replace(/\\s/g, '')}>{r.status}</span></td>
                <td>{r.by}</td>
                <td>
                  <button onClick={() => advance(r.id)}>Advance</button>{' '}
                  <button onClick={() => del(r.id)}>Delete</button>
                </td>
              </tr>
            ))}
            {recs.length === 0 && <tr><td colSpan="4" className="muted">No records</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
"""

_STYLES_CSS = """:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Segoe UI', system-ui, sans-serif; background: #0b0f17; color: #e6edf3; }
a { color: inherit; }
header { display: flex; align-items: center; justify-content: space-between; padding: 14px 22px; background: #0f1622; border-bottom: 1px solid #1f2a3a; position: sticky; top: 0; }
header .brand { font-weight: 700; font-size: 18px; }
header .brand small { display: block; color: #79c0ff; font-weight: 400; font-size: 11px; }
nav a, nav button { color: #9aa7b4; font-size: 14px; padding: 8px 12px; border-radius: 8px; text-decoration: none; background: none; border: none; cursor: pointer; }
nav a.active, nav a:hover, nav button:hover { color: #fff; background: #16324f; }
.wrap { max-width: 900px; margin: 0 auto; padding: 24px; }
.card { background: #0f1622; border: 1px solid #1f2a3a; border-radius: 14px; padding: 20px; margin-bottom: 20px; }
.row { display: flex; align-items: center; justify-content: space-between; }
h1 { font-size: 26px; } h2 { font-size: 16px; margin: 0 0 14px; color: #c9d6e4; }
label { display: block; font-size: 12px; color: #9aa7b4; margin: 10px 0 4px; }
input, textarea, select { width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #25324a; background: #0b1119; color: #e6edf3; font-size: 14px; }
.primary { display: inline-block; margin-top: 14px; background: #2f81f7; color: #fff; border: none; padding: 11px 16px; border-radius: 9px; font-size: 14px; cursor: pointer; font-weight: 600; text-decoration: none; }
.primary:hover { background: #4b95ff; }
.rec { border: 1px solid #1f2a3a; border-radius: 10px; padding: 12px; margin-top: 10px; }
.rec .t { font-weight: 600; } .rec .m { color: #9aa7b4; font-size: 12px; margin-top: 4px; }
.pill { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; margin-top: 6px; }
.New { background: #3a2a10; color: #f0b95b; } .InProgress { background: #10283a; color: #5bb8f0; } .Resolved { background: #0f1a12; color: #3fb950; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid #1a2433; }
.stat { display: flex; gap: 14px; }
.stat .box { flex: 1; background: #0b1119; border: 1px solid #1f2a3a; border-radius: 10px; padding: 14px; text-align: center; }
.stat .n { font-size: 26px; font-weight: 700; } .stat .l { font-size: 11px; color: #9aa7b4; }
.center { min-height: 90vh; display: flex; align-items: center; justify-content: center; }
.login { width: 340px; } .muted { color: #5b6b7b; font-size: 12px; } .center-text { text-align: center; margin-top: 16px; }
"""
