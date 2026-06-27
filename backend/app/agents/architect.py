"""Architect Agent: requirement -> concrete tech design via Nemotron."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import httpx

from ..config import settings
from ..logging_config import log
from ..models import Stage
from ..nvidia_client import nvidia
from .base import BaseAgent

SYSTEM = (
    "You are a senior Software Architect agent for a platform that builds and deploys "
    "every app on a FIXED stack: a React (Vite) + React Router single-page frontend, a "
    "FastAPI (Python) backend, SQLite / in-memory persistence (the demo frontend uses "
    "browser localStorage), and Docker containers deployed to AWS ECS Fargate.\n"
    "Design WITHIN this stack. Do NOT propose other technologies (no Node/Express, "
    "Next.js, Django, Material-UI/Chakra/Bootstrap, Kubernetes/EKS, MongoDB, etc.) — "
    "they are not supported and will not be generated.\n"
    "Respond ONLY with a compact JSON object with keys: frontend, backend, database, "
    "deployment, rationale. The frontend/backend/database/deployment values MUST name "
    "the platform stack above; put the requirement-specific design (key entities, user "
    "roles, pages/routes, and API endpoints) in 'rationale'."
)


class ArchitectAgent(BaseAgent):
    stage = Stage.architect
    title = "Architect Agent"

    async def execute(self) -> Dict[str, Any]:
        await self.step(f"Analyzing requirement: {self.project.description!r}")

        requirement = (
            f"Requirement: {self.project.description}\nApp type: {self.project.app_type}"
        )
        raw = await self._reason(requirement)
        design = _safe_json(raw)
        self.project.architecture = design

        await self.step(f"Frontend : {design.get('frontend')}")
        await self.step(f"Backend  : {design.get('backend')}")
        await self.step(f"Database : {design.get('database')}")
        await self.step(f"Deploy   : {design.get('deployment')}")
        return design

    async def _reason(self, requirement: str) -> str:
        """Route reasoning through the NAT server when explicitly enabled, else a
        fast direct NIM call. The Architect only emits a small JSON stack design,
        so the slow Nemotron reasoning path is opt-in (see `architect_use_nat`)."""
        if settings.architect_use_nat and settings.nat_url:
            await self.step("Reasoning via NeMo Agent Toolkit (LangGraph wrapper -> Qwen)...")
            try:
                async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                    resp = await client.post(
                        settings.nat_url,
                        json={"messages": [{"role": "user", "content": requirement}]},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                return _extract_nat_content(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("NAT call failed ({}); falling back to direct NIM", exc)
                await self.step("NAT unavailable; falling back to direct call")

        await self.step(f"Designing stack with {settings.architect_model}...")
        return await asyncio.to_thread(
            nvidia.complete,
            requirement,
            system=SYSTEM,
            model=settings.architect_model,
            max_tokens=700,
            temperature=0.3,
            thinking=False,
        )


def _extract_nat_content(data: Any) -> str:
    """Pull the design text out of the langgraph_wrapper `/generate` response.

    The wrapper returns ``{"messages": [<human>, <ai>, ...]}``; the final
    message's ``content`` is the model output. We also tolerate the simpler
    ``{"value": ...}`` and ``{"output": ...}`` shapes other front ends emit.
    """
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                return str(last.get("content", ""))
        for key in ("value", "output", "result"):
            if key in data:
                return str(data[key])
    return str(data)


def _safe_json(text: str) -> Dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return {
        "frontend": "React (Vite) + React Router",
        "backend": "FastAPI (Python)",
        "database": "SQLite / in-memory (browser localStorage in demo)",
        "deployment": "Docker + AWS ECS Fargate",
        "rationale": "Default platform stack.",
    }
