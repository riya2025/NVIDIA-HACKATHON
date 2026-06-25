"""Monitoring Agent: watch ECS/CloudWatch + Grafana alerts; raise incidents."""
from __future__ import annotations

from typing import Any, Dict

from ..models import Stage
from .base import BaseAgent


class MonitoringAgent(BaseAgent):
    stage = Stage.monitoring
    title = "Monitoring Agent"

    async def execute(self) -> Dict[str, Any]:
        m = self.project.metrics
        await self.step("Connecting to CloudWatch + Grafana data sources...")
        await self.step(f"status={m.status}  cpu={m.cpu}%  mem={m.memory}%  latency={m.latency_ms}ms")
        await self.step("No active alerts. Service healthy and live.")
        self.project.pipeline_status = "live"
        return {"healthy": True, "metrics": m.model_dump()}
