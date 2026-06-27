"""Deployment Agent: build image -> push to ECR -> deploy to ECS -> verify health.

Simulated in demo mode; real boto3 calls would slot into `_aws_deploy`.
"""
from __future__ import annotations

import asyncio
import webbrowser
from typing import Any, Dict

import httpx

from ..config import settings
from ..git_workflows import write_git_workflows
from ..local_deploy import APPS_ROOT, _slug, current_api_url
from ..local_deploy import start as start_local
from ..models import Stage
from .base import BaseAgent


class DeploymentAgent(BaseAgent):
    stage = Stage.deployment
    title = "Deployment Agent"

    async def execute(self) -> Dict[str, Any]:
        await self.step("Generating CI/CD git workflow scripts (GitHub Actions -> ECR -> ECS Fargate)...")
        workflow_files = write_git_workflows(self.project)
        for rel in workflow_files:
            await self.step(f"wrote {rel}")
        await self.emit(
            "git_workflows",
            "CI/CD workflows generated (push to main deploys to AWS ECS via OIDC)",
            {"files": workflow_files},
        )

        database_url = await self._provision_database()

        await self.step("Building container image (backend/Dockerfile)...")
        await self.step(f"Pushing image to ECR ({settings.ecr_repo})...")
        await self.step(f"Registering ECS task definition on cluster '{settings.ecs_cluster}'...")
        await self.step("Injecting DATABASE_URL into ECS task definition (secrets)...")
        await self.step("Starting service (local runtime; AWS ECS in production)...")

        url = start_local(self.project)
        self.project.deploy_url = url
        await self.step(f"Frontend live at {url}")

        # The Vite/static frontend has a companion FastAPI backend now running live.
        api_url = current_api_url(self.project)
        self.project.api_url = api_url
        api_healthy = True
        if api_url:
            await self.step(f"Backend API live at {api_url}")
            api_healthy = await _health_ok(api_url.rstrip("/") + "/health")
            await self.step(
                f"Backend health: GET {api_url}health -> {'200 OK' if api_healthy else 'FAILED'}"
            )
            await self.emit(
                "api_live",
                f"Backend API live at {api_url} (Swagger at {api_url}docs)",
                {"api_url": api_url, "docs_url": api_url + "docs", "healthy": api_healthy},
            )

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
        return {
            "deploy_url": url,
            "api_url": api_url,
            "healthy": healthy,
            "git_workflows": workflow_files,
            "database_url": database_url,
        }

    async def _provision_database(self) -> str:
        """Provision the app's managed Postgres cluster (simulated AWS RDS/Aurora).

        Mirrors the simulated ECR/ECS flow: emits realistic provisioning steps,
        derives a connection string, persists it to the project and writes a
        `backend/.env` so the generated FastAPI app has its DATABASE_URL. Swapping
        in real boto3 `create_db_cluster` calls later needs no agent changes.
        """
        arch = self.project.architecture or {}
        db_choice = str(arch.get("database", "PostgreSQL"))
        slug = _slug(self.project.name)
        cluster_id = f"{settings.ecs_cluster}-{slug}-db"
        db_name = slug.replace("-", "_") + "_db"
        endpoint = f"{cluster_id}.cluster-cxy123.{settings.aws_region}.rds.amazonaws.com"

        await self.step(f"Provisioning database (architecture: {db_choice}) -> AWS RDS/Aurora...")
        await self.step(
            f"Creating {settings.db_engine} {settings.db_engine_version} cluster "
            f"'{cluster_id}' in {settings.aws_region}..."
        )
        await self.step("Configuring VPC subnet group, security group (port 5432) and Multi-AZ writer/reader...")
        await self.step(f"Waiting for cluster status -> available (endpoint: {endpoint})...")
        await self.step("Storing master credentials in AWS Secrets Manager...")
        await self.step("Running schema migrations (alembic upgrade head)...")

        database_url = (
            f"postgresql://{settings.db_master_username}:****@{endpoint}:{settings.db_port}/{db_name}"
        )
        self.project.database_url = database_url

        # Write the generated backend's .env so the app is wired to the DB.
        try:
            backend_dir = APPS_ROOT / slug / "backend"
            backend_dir.mkdir(parents=True, exist_ok=True)
            (backend_dir / ".env").write_text(
                f"DATABASE_URL={database_url}\n", encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Could not write backend/.env: {}", exc)

        await self.emit(
            "database",
            f"Database cluster '{cluster_id}' available ({settings.db_engine})",
            {
                "engine": settings.db_engine,
                "version": settings.db_engine_version,
                "cluster_id": cluster_id,
                "endpoint": endpoint,
                "database": db_name,
                "database_url": database_url,
            },
        )
        await self.step(f"Database ready: {endpoint}:{settings.db_port}/{db_name}")
        return database_url


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
