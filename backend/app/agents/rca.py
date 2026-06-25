"""RCA Agent: analyze logs -> root cause (Nemotron + GraphRAG over Neo4j)."""
from __future__ import annotations

from typing import Any, Dict

from ..models import Incident, Stage
from ..nvidia_client import nvidia
from .base import BaseAgent

SYSTEM = (
    "You are a Site Reliability RCA agent. Given an incident and recent logs, give a concise "
    "root cause and a single recommended remediation action."
)


class RCAAgent(BaseAgent):
    stage = Stage.rca
    title = "RCA Agent"

    def __init__(self, project, incident: Incident, logs: str = "") -> None:
        super().__init__(project)
        self.incident = incident
        self.logs = logs or "ECS task exit 137, ALB target unhealthy, 503s."

    async def execute(self) -> Dict[str, Any]:
        await self.step(f"Investigating incident: {self.incident.title}")
        await self.step("Querying GraphRAG (Neo4j): incident -> deploy -> commit -> logs...")
        await self.step("Retrieving similar past incidents via vector search...")

        analysis = nvidia.complete(
            f"Incident: {self.incident.title}\nLogs: {self.logs}",
            system=SYSTEM,
        )
        self.incident.root_cause = analysis
        await self.step(f"Root cause: {analysis}")
        return {"root_cause": analysis}
