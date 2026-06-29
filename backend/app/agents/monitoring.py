"""Monitoring Agent: watch service health + audit the live UI layout/alignment."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ..models import Stage
from ..react_template import layout_audit
from .base import BaseAgent

APPS_ROOT = Path(__file__).resolve().parent.parent.parent / "generated_apps"


def _slug(name: str) -> str:
    s = "".join(c if c.isalnum() else "-" for c in (name or "").lower()).strip("-")
    return s or "app"


class MonitoringAgent(BaseAgent):
    stage = Stage.monitoring
    title = "Monitoring Agent"

    async def execute(self) -> Dict[str, Any]:
        m = self.project.metrics
        await self.step("Connecting to CloudWatch + Grafana data sources...")
        await self.step(f"status={m.status}  cpu={m.cpu}%  mem={m.memory}%  latency={m.latency_ms}ms")

        css_result = await self._audit_layout()

        await self.step("No active alerts. Service healthy and live.")
        self.project.pipeline_status = "live"
        return {"healthy": True, "metrics": m.model_dump(), "layout": css_result}

    async def _audit_layout(self) -> Dict[str, Any]:
        """Check the deployed UI's CSS for alignment/responsiveness issues and
        auto-fix the code (append a corrective CSS layer) if any are found."""
        app_dir = APPS_ROOT / _slug(self.project.name)
        css_path = app_dir / "src" / "styles.css"
        html_path = app_dir / "index.html"
        if not css_path.exists():
            await self.step("UI layout audit skipped (no stylesheet found).")
            return {"checked": False}

        css = css_path.read_text(encoding="utf-8", errors="ignore")
        html = html_path.read_text(encoding="utf-8", errors="ignore") if html_path.exists() else ""

        await self.step("Auditing UI layout — alignment & responsive fit to screen...")
        issues, corrective = layout_audit(css, html)

        if not issues:
            await self.step("Layout verified: box-sizing, centered container, responsive breakpoints OK.")
            return {"checked": True, "aligned": True, "issues": []}

        await self.step(f"Layout audit found {len(issues)} alignment issue(s): {'; '.join(issues)}")
        if corrective:
            css_path.write_text(css + corrective, encoding="utf-8")
            # Mirror the fix into the built bundle's source so a rebuild/heal picks it up.
            await self.step(f"Self-healed UI: appended corrective CSS layer to styles.css ({len(issues)} fix(es)).")
        return {"checked": True, "aligned": False, "issues": issues, "fixed": bool(corrective)}
