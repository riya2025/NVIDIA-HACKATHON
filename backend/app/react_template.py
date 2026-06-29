"""Shared styling + layout helpers for generated React (Vite) apps.

This module no longer contains any generic fallback/template *application*. The
Frontend agent always ships the real model-generated `src/App.jsx`; the only
things kept here are:

  - `_STYLES_CSS`  : the base stylesheet injected into every generated app.
  - `theme_css`    : a per-app color theme chosen deterministically from the name.
  - `layout_audit` : the static UI-alignment audit used by the Monitoring agent.
"""
from __future__ import annotations


# Per-app theme palettes. The base stylesheet is fixed (so generated code always
# compiles); we append one of these as overrides, chosen deterministically from
# the app name, so each app gets a distinct gradient accent + decorative
# background with zero build risk. Each theme is a two-stop gradient (accent ->
# accent2) on a deep, elegant dark canvas; `theme_css` paints soft, colorful
# radial "aurora" blobs from these colors so every app feels rich and polished.
_THEMES = [
    {"label": "indigo",  "accent": "#6366f1", "accent2": "#a855f7", "hover": "#818cf8", "soft_text": "#c4b5fd", "surface": "#161826", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
    {"label": "emerald", "accent": "#10b981", "accent2": "#06b6d4", "hover": "#34d399", "soft_text": "#6ee7d3", "surface": "#0f1a1a", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
    {"label": "sunset",  "accent": "#f43f5e", "accent2": "#fb923c", "hover": "#fb7185", "soft_text": "#fdba74", "surface": "#1c1417", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
    {"label": "ocean",   "accent": "#0ea5e9", "accent2": "#6366f1", "hover": "#38bdf8", "soft_text": "#7dd3fc", "surface": "#121826", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
    {"label": "orchid",  "accent": "#8b5cf6", "accent2": "#ec4899", "hover": "#a78bfa", "soft_text": "#f0abfc", "surface": "#1a1422", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
    {"label": "amber",   "accent": "#f59e0b", "accent2": "#f43f5e", "hover": "#fbbf24", "soft_text": "#fcd34d", "surface": "#1c1814", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
    {"label": "mint",    "accent": "#14b8a6", "accent2": "#22c55e", "hover": "#2dd4bf", "soft_text": "#5eead4", "surface": "#0f1a18", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
    {"label": "fuchsia", "accent": "#d946ef", "accent2": "#6366f1", "hover": "#e879f9", "soft_text": "#f5d0fe", "surface": "#1a1426", "font": "'Inter', 'Segoe UI', system-ui, sans-serif"},
]


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """'#6366f1', 0.2 -> 'rgba(99,102,241,0.2)'. Used to build soft, translucent
    accent washes (background blobs, focus rings, pills) from a theme color."""
    h = (hex_color or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        r, g, b = 0, 0, 0
    return f"rgba({r},{g},{b},{alpha})"


def layout_audit(css: str, html: str = "") -> tuple[list[str], str]:
    """Static UI-alignment audit used by the Monitoring agent.

    Checks the deployed app's stylesheet/markup against a checklist of layout &
    responsiveness rules (the things that visually break alignment on a real
    screen) and returns (issues, corrective_css). corrective_css is a small CSS
    layer that fixes every detected issue; it's empty when the layout is clean.
    """
    css_l = (css or "").lower()
    html_l = (html or "").lower()
    issues: list[str] = []
    fixes: list[str] = []

    if "box-sizing" not in css_l:
        issues.append("no box-sizing reset (padding/borders push elements out of alignment)")
        fixes.append("*,*::before,*::after{box-sizing:border-box}")
    if "max-width" not in css_l:
        issues.append("no max-width container (content stretches edge-to-edge on wide screens)")
        fixes.append(".wrap{max-width:960px;margin:0 auto}")
    if "overflow-x" not in css_l:
        issues.append("no horizontal-overflow guard (risk of sideways scroll / shifted layout)")
        fixes.append("html,body{max-width:100%;overflow-x:hidden}")
    if "@media" not in css_l:
        issues.append("no responsive breakpoint (layout misaligns on small screens)")
        fixes.append("@media(max-width:600px){header{flex-direction:column;align-items:flex-start}.row{flex-direction:column;align-items:stretch}}")
    if "margin:0" not in css_l.replace(" ", "") and "margin: 0" not in css_l:
        issues.append("body has default margin (content not flush to viewport)")
        fixes.append("body{margin:0}")
    if html and "width=device-width" not in html_l:
        issues.append("missing responsive viewport meta tag")
    # Common LLM pattern: a centering wrapper (.center) that also carries a width
    # constraint (.login) but never auto-centers -> the card hugs the left edge.
    compact = css_l.replace(" ", "")
    if ".login" in css_l and ".center" in css_l and "margin:0auto" not in compact and "margin:auto" not in compact:
        issues.append(".center/.login wrapper not auto-centered (login/setup card sticks to the left)")
        fixes.append(
            ".center{width:100%;display:flex;flex-direction:column;align-items:center;justify-content:center}\n"
            ".login{margin:0 auto}"
        )

    corrective = ""
    if fixes:
        corrective = (
            "\n/* --- auto-aligned by Monitoring agent (layout audit) --- */\n"
            + "\n".join(fixes)
            + "\n"
        )
    return issues, corrective


def theme_css(name: str) -> str:
    """Return CSS variable overrides for a per-app theme chosen from the app name.

    The base stylesheet is written entirely in terms of CSS custom properties
    (--accent, --accent2, --soft-bg, ...), so a theme only needs to redefine those
    variables and paint the decorative aurora background. Everything (buttons,
    pills, focus rings, gradient headings) re-colors automatically — no per-rule
    overrides, so there's zero risk of breaking the build-safe base layout.
    """
    import hashlib

    h = int(hashlib.md5((name or "app").encode("utf-8")).hexdigest(), 16)
    t = _THEMES[h % len(_THEMES)]
    a, a2 = t["accent"], t["accent2"]
    blob1 = _hex_to_rgba(a, 0.22)
    blob2 = _hex_to_rgba(a2, 0.20)
    blob3 = _hex_to_rgba(t["hover"], 0.14)
    return (
        f"\n/* --- per-app theme: {t['label']} (auto-selected from app name) --- */\n"
        ":root{"
        f"--accent:{a};--accent2:{a2};--accent-hover:{t['hover']};"
        f"--soft-bg:{_hex_to_rgba(a, 0.16)};--soft-text:{t['soft_text']};"
        f"--ring:{_hex_to_rgba(a, 0.35)};--surface-solid:{t['surface']};"
        f"--grad:linear-gradient(135deg,{a},{a2})"
        "}\n"
        f"body{{font-family:{t['font']};background:"
        f"radial-gradient(1100px 620px at 8% -12%, {blob1}, transparent 60%),"
        f"radial-gradient(1000px 560px at 104% 4%, {blob2}, transparent 55%),"
        f"radial-gradient(900px 600px at 50% 118%, {blob3}, transparent 60%),"
        "var(--bg);background-attachment:fixed}\n"
    )


_STYLES_CSS = """:root {
  color-scheme: dark;
  --maxw: 1040px;
  --radius: 18px;
  --radius-sm: 12px;
  --gap: 18px;
  /* Theme tokens (overridden per-app by theme_css; sane indigo defaults here). */
  --bg: #0a0b14;
  --accent: #6366f1;
  --accent2: #a855f7;
  --accent-hover: #818cf8;
  --soft-bg: rgba(99,102,241,.16);
  --soft-text: #c4b5fd;
  --ring: rgba(99,102,241,.35);
  --surface-solid: #161826;
  --grad: linear-gradient(135deg, var(--accent), var(--accent2));
  /* Neutral surface system (translucent for glassmorphism over the aurora bg). */
  --surface: rgba(255,255,255,.045);
  --surface-2: rgba(255,255,255,.07);
  --border: rgba(255,255,255,.10);
  --border-strong: rgba(255,255,255,.18);
  --text: #eef1f8;
  --muted: #9aa3b8;
  --shadow: 0 18px 50px -22px rgba(0,0,0,.75);
  --shadow-sm: 0 8px 24px -14px rgba(0,0,0,.7);
}
*, *::before, *::after { box-sizing: border-box; }
html, body { max-width: 100%; overflow-x: hidden; }
body {
  margin: 0;
  font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
  /* Default aurora background; theme_css repaints it per app. */
  background:
    radial-gradient(1100px 620px at 8% -12%, rgba(99,102,241,.22), transparent 60%),
    radial-gradient(1000px 560px at 104% 4%, rgba(168,85,247,.20), transparent 55%),
    radial-gradient(900px 600px at 50% 118%, rgba(129,140,248,.14), transparent 60%),
    var(--bg);
  background-attachment: fixed;
  min-height: 100vh;
  color: var(--text);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
img, svg, video { max-width: 100%; height: auto; display: block; }
a { color: var(--soft-text); text-decoration: none; transition: color .15s; }
a:hover { color: var(--accent-hover); }
::selection { background: var(--soft-bg); color: #fff; }
::-webkit-scrollbar { width: 11px; height: 11px; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,.14); border-radius: 999px; border: 3px solid transparent; background-clip: content-box; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.24); background-clip: content-box; }

/* ---- top bar / nav ---- */
header, .topbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 14px; flex-wrap: wrap;
  padding: 14px clamp(16px, 4vw, 32px);
  background: rgba(10,11,20,.55);
  backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 30;
}
header .brand, .topbar .brand { font-weight: 800; font-size: 19px; line-height: 1.2; letter-spacing: -.01em; }
header .brand small, .topbar .brand small { display: block; color: var(--soft-text); font-weight: 500; font-size: 11px; margin-top: 2px; }
nav, .nav { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
nav a, nav button, .nav a, .nav button {
  color: var(--muted); font-size: 14px; font-weight: 600; padding: 9px 14px; border-radius: 999px;
  text-decoration: none; background: none; border: none; cursor: pointer;
  font-family: inherit; line-height: 1; transition: background .18s, color .18s, transform .18s;
}
nav a:hover, nav button:hover, .nav a:hover, .nav button:hover { color: var(--text); background: var(--surface-2); }
nav a.active, .nav a.active { color: #fff; background: var(--grad); box-shadow: 0 8px 20px -10px var(--accent); }

/* ---- layout container ---- */
.wrap, .container { width: 100%; max-width: var(--maxw); margin: 0 auto; padding: clamp(18px, 4vw, 34px); }
.card {
  position: relative;
  background: var(--surface);
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: clamp(18px, 3vw, 26px);
  margin-bottom: 20px;
  box-shadow: var(--shadow);
  transition: transform .22s ease, box-shadow .22s ease, border-color .22s ease;
  animation: cardIn .5s cubic-bezier(.2,.7,.2,1) both;
}
.card:hover { transform: translateY(-3px); border-color: var(--border-strong); box-shadow: 0 26px 60px -24px rgba(0,0,0,.85); }
.row { display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--gap); }
.field { margin-bottom: 6px; }

/* ---- typography ---- */
h1 { font-size: clamp(26px, 4.5vw, 38px); margin: 0 0 8px; line-height: 1.12; font-weight: 800; letter-spacing: -.025em; }
h2 { font-size: clamp(17px, 2.5vw, 20px); margin: 0 0 14px; color: var(--text); font-weight: 700; letter-spacing: -.01em; }
h3 { font-size: 15px; margin: 0 0 10px; color: var(--text); font-weight: 700; }
p { margin: 0 0 12px; }
/* Gradient display heading when .primary lands on a heading (common LLM pattern). */
h1.primary, h2.primary, h3.primary {
  background: var(--grad); -webkit-background-clip: text; background-clip: text;
  color: transparent; -webkit-text-fill-color: transparent;
  display: inline-block; padding: 0; margin: 0 0 8px; border: none; box-shadow: none;
}

/* ---- hero ---- */
.hero { text-align: center; padding: clamp(28px, 6vw, 64px) 16px; }
.hero h1 { background: var(--grad); -webkit-background-clip: text; background-clip: text; color: transparent; -webkit-text-fill-color: transparent; }

/* ---- forms ---- */
label, .label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin: 12px 0 6px; letter-spacing: .02em; }
input, textarea, select, .input {
  width: 100%; padding: 12px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border);
  background: rgba(255,255,255,.04); color: var(--text); font-size: 14px; font-family: inherit;
  transition: border-color .15s, box-shadow .15s, background .15s;
}
input::placeholder, textarea::placeholder { color: #6b7488; }
textarea { resize: vertical; min-height: 84px; }
input:focus, textarea:focus, select:focus, .input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 4px var(--ring); background: rgba(255,255,255,.06); }

/* ---- buttons ---- */
button, .btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  cursor: pointer; border: 1px solid var(--border); background: var(--surface-2); color: var(--text);
  padding: 11px 18px; border-radius: var(--radius-sm); font-size: 14px; font-weight: 600;
  font-family: inherit; line-height: 1.2; text-decoration: none;
  transition: transform .16s ease, box-shadow .16s ease, filter .16s ease, background .16s ease;
}
button:hover, .btn:hover { transform: translateY(-1px); background: rgba(255,255,255,.12); }
button:disabled, .btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }
.btn-primary, button.btn-primary, .primary {
  margin-top: 0; background: var(--grad); border: none; color: #fff;
  box-shadow: 0 10px 24px -10px var(--accent);
}
.btn-primary:hover, button.btn-primary:hover, .primary:hover { filter: brightness(1.08); transform: translateY(-2px); box-shadow: 0 16px 32px -10px var(--accent); }
.primary { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 11px 20px; border-radius: var(--radius-sm); font-weight: 700; cursor: pointer; }

/* ---- lists / records ---- */
.list { list-style: none; padding: 0; margin: 0; }
.list-item, .rec {
  border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 15px; margin-top: 12px;
  background: var(--surface); transition: border-color .18s, transform .18s, background .18s;
}
.list-item:hover, .rec:hover { border-color: var(--border-strong); background: var(--surface-2); transform: translateX(2px); }
.rec .t { font-weight: 700; } .rec .m { color: var(--muted); font-size: 12px; margin-top: 4px; word-break: break-word; }

/* ---- pills / badges / avatars ---- */
.pill {
  display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600;
  padding: 5px 12px; border-radius: 999px; background: var(--soft-bg); color: var(--soft-text);
  border: 1px solid var(--border); margin: 4px 4px 0 0;
}
.badge {
  display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 700;
  padding: 4px 11px; border-radius: 999px; background: var(--grad); color: #fff; letter-spacing: .3px; margin-top: 6px;
}
.avatar {
  display: inline-flex; align-items: center; justify-content: center;
  width: 56px; height: 56px; border-radius: 50%; font-size: 28px;
  background: var(--surface-2); border: 2px solid transparent;
  background-image: linear-gradient(var(--surface-solid), var(--surface-solid)), var(--grad);
  background-origin: border-box; background-clip: padding-box, border-box;
}
.New { background: rgba(245,158,11,.15); color: #fcd34d; } .InProgress { background: rgba(14,165,233,.15); color: #7dd3fc; } .Resolved { background: rgba(34,197,94,.15); color: #86efac; }

/* ---- table ---- */
.card table { width: 100%; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 11px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
th { color: var(--muted); font-weight: 700; text-transform: uppercase; font-size: 11px; letter-spacing: .04em; }
tbody tr { transition: background .15s; } tbody tr:hover { background: var(--surface); }

/* ---- stat boxes ---- */
.stat { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: var(--gap); }
.stat .box, .box {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 18px; text-align: center; box-shadow: var(--shadow-sm);
}
.stat .n { font-size: 30px; font-weight: 800; line-height: 1.1; background: var(--grad); -webkit-background-clip: text; background-clip: text; color: transparent; -webkit-text-fill-color: transparent; }
.stat .l { font-size: 11px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: .04em; }

/* ---- centered auth ---- */
.center { width: 100%; min-height: 84vh; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; }
.login { width: 100%; max-width: 400px; margin: 0 auto; }
.muted { color: var(--muted); font-size: 13px; } .center-text { text-align: center; margin-top: 16px; }

/* ---- motion ---- */
@keyframes cardIn { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }

/* ---- responsive ---- */
@media (max-width: 600px) {
  header, .topbar { flex-direction: column; align-items: flex-start; }
  nav, .nav { width: 100%; }
  .row { flex-direction: column; align-items: stretch; }
  .row .primary, .row .btn { width: 100%; }
  table { font-size: 12px; }
  th, td { padding: 8px 6px; }
}
"""
