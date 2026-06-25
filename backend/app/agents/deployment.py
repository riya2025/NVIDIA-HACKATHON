"""Deployment Agent: build image -> push to ECR -> deploy to ECS -> verify health.

Simulated in demo mode; real boto3 calls would slot into `_aws_deploy`.
"""
from __future__ import annotations

import asyncio
import webbrowser
from typing import Any, Dict

import httpx

from ..config import settings
from ..local_deploy import start as start_local
from ..models import Stage
from .base import BaseAgent


class DeploymentAgent(BaseAgent):
    stage = Stage.deployment
    title = "Deployment Agent"

    async def execute(self) -> Dict[str, Any]:
        await self.step("Building container image...")
        await self.step(f"Pushing image to ECR ({settings.ecr_repo})...")
        await self.step(f"Registering ECS task definition on cluster '{settings.ecs_cluster}'...")
        await self.step("Starting service (local runtime; AWS ECS in production)...")

        url = start_local(self.project)
        self.project.deploy_url = url
        await self.step(f"Service live at {url}")

        healthy = await _health_ok(url)
        await self.step(
            f"Health check: GET {url} -> {'200 OK' if healthy else 'FAILED'}"
        )

        if healthy:
            # Tell the UI to embed/open the app, and auto-open the user's browser.
            await self.emit("deployed", f"App deployed and live at {url}", {"url": url, "open": True})
            await self.step("Opening the live app in your browser...")
            try:
                await asyncio.to_thread(webbrowser.open, url)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Could not auto-open browser: {}", exc)

        self.project.metrics.status = "healthy" if healthy else "unhealthy"
        self.project.metrics.cpu = 23.0
        self.project.metrics.memory = 42.0
        self.project.metrics.latency_ms = 150.0
        self.project.metrics.error_rate = 0.0
        return {"deploy_url": url, "healthy": healthy}


async def _health_ok(url: str) -> bool:
    for _ in range(10):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    return False
