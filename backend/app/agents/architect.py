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
    "You are a senior Software Architect agent. Given an app requirement, respond ONLY "
    "with a compact JSON object with keys: frontend, backend, database, deployment, rationale."
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
            await self.step("Reasoning via NeMo Agent Toolkit (nat serve -> Nemotron)...")
            try:
                async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                    resp = await client.post(
                        settings.nat_url, json={"requirement": requirement}
                    )
                    resp.raise_for_status()
                    data = resp.json()
                return data.get("value", "") if isinstance(data, dict) else str(data)
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


def _safe_json(text: str) -> Dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return {
        "frontend": "Next.js",
        "backend": "FastAPI",
        "database": "PostgreSQL",
        "deployment": "Docker + AWS ECS",
        "rationale": "Default SaaS stack.",
    }
