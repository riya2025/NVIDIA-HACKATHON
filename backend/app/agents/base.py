"""Base agent: shared lifecycle, event emission, logging and state updates."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ..events import Event, bus
from ..logging_config import log
from ..models import AgentState, AgentStatus, Project, Stage

# Pace of streamed log lines (seconds) so the UI renders a live feed.
STEP_DELAY = 0.2


class BaseAgent:
    stage: Stage = Stage.architect
    title: str = "Agent"

    def __init__(self, project: Project) -> None:
        self.project = project
        if self.stage.value not in project.agents:
            project.agents[self.stage.value] = AgentState(stage=self.stage)
        self.state = project.agents[self.stage.value]
        self.log = log.bind(agent=self.stage.value)

    async def emit(self, type_: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        await bus.publish(
            Event(
                project_id=self.project.id,
                agent=self.stage.value,
                type=type_,
                message=message,
                data=data or {},
            )
        )

    async def step(self, message: str) -> None:
        """Log + stream a single progress line."""
        self.state.logs.append(message)
        self.log.info(message)
        await self.emit("agent_log", message)
        await asyncio.sleep(STEP_DELAY)

    async def run(self) -> Dict[str, Any]:
        self.state.status = AgentStatus.running
        self.log.info("{} started", self.title)
        await self.emit("agent_started", f"{self.title} started")
        try:
            output = await self.execute()
            self.state.output = output
            self.state.status = AgentStatus.success
            await self.emit("agent_completed", f"{self.title} completed", output)
            return output
        except Exception as exc:  # noqa: BLE001
            self.state.status = AgentStatus.failed
            self.log.error("{} failed: {}", self.title, exc)
            await self.emit("agent_failed", f"{self.title} failed: {exc}")
            raise

    async def execute(self) -> Dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError
