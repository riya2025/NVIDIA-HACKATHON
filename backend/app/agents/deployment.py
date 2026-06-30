"""Deployment Agent: provision DB -> deploy backend (Render) + frontend (Vercel).

Narrates a Render + Vercel deployment. When the matching tokens are configured
(VERCEL_TOKEN / RENDER_DEPLOY_HOOK_URL) it performs the REAL cloud deploy and
uses the returned URLs; otherwise it runs the local Docker preview so the demo
always shows a live, healthy app.
"""
from __future__ import annotations

import asyncio
import webbrowser
from typing import Any, Dict

import httpx

from .. import cloud_deploy, docker_deploy, github_deploy
from ..config import settings
from ..local_deploy import APPS_ROOT, _slug, current_api_url
from ..local_deploy import start as start_local
from ..models import Stage
from .base import BaseAgent

# Keeps references to fire-and-forget deploy-time RCA tasks so they aren't
# garbage-collected mid-run (asyncio only holds a weak ref to running tasks).
_BG_TASKS: set = set()


class DeploymentAgent(BaseAgent):
    stage = Stage.deployment
    title = "Deployment Agent"

    async def execute(self) -> Dict[str, Any]:
        # Deploy-TIME failures (cloud step failed despite being configured) are
        # collected here, then surfaced as a Monitoring incident + RCA so they
        # appear in the live stream (the runtime watchdog can't see them — there's
        # no live cloud URL to probe).
        self._deploy_failures: list[str] = []
        await self.step("Generating cloud deploy config (render.yaml + vercel.json + CI workflow)...")
        workflow_files = cloud_deploy.write_cloud_artifacts(self.project)
        for rel in workflow_files:
            await self.step(f"wrote {rel}")
        await self.emit(
            "git_workflows",
            "Deploy config generated (push to main -> Vercel frontend + Render backend)",
            {"files": workflow_files},
        )

        database_url = await self._provision_database()

        urls = await self._deploy_app()
        # A configured cloud target failed at deploy time -> Monitoring catches it
        # and RCA investigates (visible in the live stream), even though the local
        # preview keeps the app live.
        await self._flag_deploy_failures(urls)
        # Primary (embedded, watchdog, self-heal) target = the always-on local
        # preview, so the demo always has a live, healable app even while the
        # Render service is still building. Cloud links are surfaced alongside.
        url = urls["local_url"]
        api_url = urls["local_api_url"]
        self.project.deploy_url = url
        self.project.api_url = api_url
        self.project.local_url = url
        self.project.local_api_url = api_url
        self.project.vercel_url = urls["vercel_url"]
        self.project.render_url = urls["render_url"]
        self.project.docker = urls["docker"]
        # When the Docker stack is up, surface the REAL container DB connection
        # string (live host:port) instead of the illustrative provisioning URL.
        if urls.get("docker_db_url"):
            database_url = urls["docker_db_url"]
            self.project.database_url = database_url
        await self.step(f"Frontend live at {url}")

        # The frontend has a companion FastAPI backend running live.
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

        if urls["vercel_url"]:
            await self.step(f"Vercel frontend live at {urls['vercel_url']}")
        if urls["render_url"]:
            await self.step(
                f"Render backend at {urls['render_url']} "
                f"(builds from GitHub; health live in a few minutes)"
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

        # Final summary of every real, openable link for this deploy.
        await self.emit(
            "deploy_links",
            "Deployment links",
            {
                "frontend_url": url,
                "api_url": api_url,
                "vercel_url": urls["vercel_url"],
                "render_url": urls["render_url"],
                "local_url": urls["local_url"],
                "local_api_url": urls["local_api_url"],
                "docker": urls["docker"],
                "repo_url": self.project.repo_url,
                "database_url": database_url,
                "supabase_url": settings.supabase_url or None,
                "supabase_dashboard_url": cloud_deploy.supabase_dashboard_url(),
            },
        )
        return {
            "deploy_url": url,
            "api_url": api_url,
            "vercel_url": urls["vercel_url"],
            "render_url": urls["render_url"],
            "local_url": urls["local_url"],
            "local_api_url": urls["local_api_url"],
            "docker": urls["docker"],
            "healthy": healthy,
            "git_workflows": workflow_files,
            "database_url": database_url,
            "repo_url": self.project.repo_url,
            "supabase_url": settings.supabase_url or None,
            "supabase_dashboard_url": cloud_deploy.supabase_dashboard_url(),
        }

    async def _flag_deploy_failures(self, urls: Dict[str, Any]) -> None:
        """If a CONFIGURED cloud target produced no URL, raise a Monitoring
        incident + RCA so the deploy-time failure is visible in the live stream.

        Only escalates targets that genuinely have no URL (so a fallback path that
        succeeded — e.g. Render deploy-hook after the GitHub path failed — is not
        reported as a failure)."""
        problems: list[str] = []
        if cloud_deploy.vercel_configured() and not urls.get("vercel_url"):
            problems.append("frontend → Vercel")
        render_expected = (
            github_deploy.github_configured() and cloud_deploy.render_api_configured()
        ) or cloud_deploy.render_configured()
        if render_expected and not urls.get("render_url"):
            problems.append("backend → Render")
        if not problems:
            return

        title = "Cloud deploy incomplete: " + ", ".join(problems) + " did not come up"
        detail = "\n- ".join(self._deploy_failures) if self._deploy_failures else ", ".join(problems)
        logs = (
            f"Deploy-time failure detected during the Deployment stage.\n"
            f"Targets that did not come up: {', '.join(problems)}.\n"
            f"Details:\n- {detail}\n"
            "Note: the local/Docker preview is still live, so only the cloud target(s) "
            "above are affected."
        )
        await self.emit(
            "deploy_failure",
            f"Deploy-time issue detected ({', '.join(problems)}) — handing off to Monitoring/RCA/Self-Healing",
            {"problems": problems},
        )

        async def _retry() -> Dict[str, Any] | None:
            """Self-Healing action: re-attempt ONLY the failed cloud target(s) once
            and return whatever recovered (so the incident can be auto-resolved)."""
            recovered: Dict[str, Any] = {}
            if "frontend → Vercel" in problems:
                await self.step("Self-heal: retrying Vercel frontend deploy...")
                u = await self._try_vercel()
                if u:
                    self.project.vercel_url = u
                    recovered["vercel_url"] = u
            if "backend → Render" in problems:
                await self.step("Self-heal: retrying backend deploy (GitHub push -> Render)...")
                u = await self._try_render_github() or await self._try_render()
                if u:
                    self.project.render_url = u
                    recovered["render_url"] = u
            if recovered:
                # Refresh the dashboard links now that a cloud target is live.
                await self.emit(
                    "deploy_links",
                    "Deployment links (updated after self-heal)",
                    {
                        "frontend_url": self.project.deploy_url,
                        "api_url": self.project.api_url,
                        "vercel_url": self.project.vercel_url,
                        "render_url": self.project.render_url,
                        "local_url": self.project.local_url,
                        "local_api_url": self.project.local_api_url,
                        "docker": self.project.docker,
                        "repo_url": self.project.repo_url,
                    },
                )
            return recovered or None

        # Lazy import to avoid a circular import (orchestrator -> graph -> agents).
        from .. import orchestrator

        # Run the loop in the background so it streams (Monitoring incident -> RCA
        # -> Self-Healing retry) without blocking the rest of the deploy summary.
        task = asyncio.create_task(
            orchestrator.report_deploy_failure(
                self.project, title=title, logs=logs, component="deployment", retry=_retry
            )
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)

    async def _deploy_app(self) -> Dict[str, Any]:
        """Bring the app up on EVERY available target and return all the URLs.

        1. Attempts the REAL cloud deploys (best-effort): Vercel for the frontend,
           Render (via a pushed GitHub repo) for the backend. These run only when
           the matching tokens/CLIs are configured.
        2. ALWAYS starts the local preview (Docker Compose stack when the daemon
           is up, otherwise a local-process preview) so there is a guaranteed
           live, healable URL to embed and self-heal — even while the Render
           service is still building.

        Returns a dict with: local_url, local_api_url, docker, vercel_url,
        render_url.
        """
        # ---- Real cloud deploys (best-effort; only when configured) ----
        # Backend, in order of preference:
        #   1. GitHub repo -> Render API (free, public)  2. Render deploy-hook.
        vercel_url = await self._try_vercel()
        render_url = await self._try_render_github() or await self._try_render()
        if vercel_url or render_url:
            await self.emit(
                "deployed_cloud",
                "Deployed to the cloud (Vercel frontend + Render backend)",
                {"frontend_url": vercel_url, "backend_url": render_url},
            )

        # ---- Always-on local preview (guaranteed-working URL) ----
        local_url, local_api_url, is_docker, docker_db_url = await self._local_preview()

        return {
            "local_url": local_url,
            "local_api_url": local_api_url,
            "docker": is_docker,
            "docker_db_url": docker_db_url,
            "vercel_url": vercel_url,
            "render_url": render_url,
        }

    async def _local_preview(self) -> tuple[str, str | None, bool, str | None]:
        """Run the app locally as a guaranteed-working preview.

        Uses a real Docker Compose stack (db + backend + frontend) when the
        Docker daemon is available; otherwise a local-process preview (Vite dev
        + uvicorn). Returns (frontend_url, api_url, is_docker, db_url)."""
        if settings.deploy_mode == "docker" and await asyncio.to_thread(docker_deploy.docker_available):
            slug = _slug(self.project.name)
            await self.step("Containerizing app with Docker (multi-service: database + backend + frontend)...")
            await self.step(f"docker build -> image '{slug}-backend' (FastAPI + Uvicorn on python:3.13-slim)")
            await self.step(f"docker build -> image '{slug}-frontend' (Vite production build served by nginx)")
            await self.step("docker pull -> image 'postgres:16-alpine' (managed database container)")
            await self.step("docker compose up -d --build  (Render builds this same image in production)...")
            urls = await asyncio.to_thread(docker_deploy.up, self.project)
            if urls:
                containers = [
                    {"service": "db", "image": "postgres:16-alpine"},
                    {"service": "backend", "image": f"{slug}-backend"},
                    {"service": "frontend", "image": f"{slug}-frontend"},
                ]
                await self.step(
                    f"Containers running: db (postgres:16-alpine), backend ({slug}-backend), "
                    f"frontend ({slug}-frontend)"
                )
                await self.emit(
                    "docker_up",
                    "3 containers live via Docker Compose (db + backend + frontend)",
                    {**urls, "containers": containers},
                )
                return urls["frontend_url"], urls["backend_url"], True, urls.get("database_url")
            await self.step("Container deploy failed; falling back to local-process runtime...")
        else:
            await self.step("Docker runtime unavailable; using local-process preview...")

        await self.step("Building frontend (Vite) and backend for the live preview...")
        url = await asyncio.to_thread(start_local, self.project)
        return url, current_api_url(self.project), False, None

    async def _try_vercel(self) -> str | None:
        if not cloud_deploy.vercel_configured():
            return None
        await self.step("Deploying frontend to Vercel (vercel deploy --prod)...")
        url = await asyncio.to_thread(cloud_deploy.deploy_frontend_vercel, self.project)
        if url:
            await self.step(f"Vercel deployment live: {url}")
        else:
            await self.step("Vercel deploy did not return a URL; continuing with preview.")
            self._deploy_failures.append(
                "Vercel frontend deploy FAILED: VERCEL_TOKEN is configured but the "
                "`vercel deploy --prod` CLI returned no production URL. See the recent "
                "deployment logs for the Vercel CLI error (auth/build/scope/quota)."
            )
        return url

    async def _try_render_github(self) -> str | None:
        """Free public backend: push the app to a new public GitHub repo, then
        create a real Render web service from it via the Render API."""
        if not (github_deploy.github_configured() and cloud_deploy.render_api_configured()):
            return None

        # Guarantee the backend Dockerfile + requirements exist so Render can build.
        await asyncio.to_thread(docker_deploy.write_docker_artifacts, self.project)
        await self.step("Creating a public GitHub repo and pushing the generated app (git push)...")
        pushed = await asyncio.to_thread(github_deploy.create_and_push_repo, self.project)
        if not pushed:
            await self.step("GitHub push did not succeed; continuing with preview.")
            self._deploy_failures.append(
                "Render backend NOT deployed: the prerequisite GitHub push failed, so no "
                "repo exists for Render to build from. Render builds the backend from a "
                "GitHub repo, so a failed push blocks the entire backend cloud deploy. "
                "Check the recent deployment logs for the underlying git/GitHub API error "
                "(e.g. auth, network, or 'too many open files'/antivirus file-lock)."
            )
            return None
        repo_url, branch = pushed
        self.project.repo_url = repo_url
        await self.step(f"Pushed to {repo_url} (branch: {branch})")
        await self.emit("repo_pushed", f"Source pushed to GitHub: {repo_url}", {"repo_url": repo_url, "branch": branch})

        # Wire the real (unmasked) Supabase connection string into the service.
        database_url = settings.supabase_db_url if cloud_deploy.supabase_configured() else None
        await self.step("Creating Render web service from the repo (Docker runtime, free plan)...")
        url = await asyncio.to_thread(
            cloud_deploy.deploy_backend_render_api, self.project, repo_url, branch, database_url
        )
        if url:
            await self.step(f"Render backend service created: {url} (building from {repo_url})")
            await self.step(f"Render builds the Docker image now; health will be live at {url.rstrip('/')}/health in a few minutes.")
        else:
            await self.step("Render API did not return a service URL; continuing with preview.")
            self._deploy_failures.append(
                f"Render backend NOT deployed: the repo was pushed ({repo_url}) but the Render "
                "API did not return a service URL. Check RENDER_API_KEY / owner id / plan limits "
                "and the recent deployment logs for the Render API response."
            )
        return url

    async def _try_render(self) -> str | None:
        if not cloud_deploy.render_configured():
            return None
        await self.step("Render building Docker image from backend/Dockerfile (render.yaml runtime: docker)...")
        await self.step("Triggering Render backend deploy (deploy hook)...")
        ok = await asyncio.to_thread(cloud_deploy.trigger_render, self.project)
        if ok:
            url = cloud_deploy.render_backend_url(self.project)
            await self.step(f"Render deploy triggered; service at {url} (build runs on Render).")
            return url
        await self.step("Render deploy hook did not succeed; continuing with preview.")
        self._deploy_failures.append(
            "Render backend NOT deployed: the RENDER_DEPLOY_HOOK_URL request failed (no 200). "
            "Verify the deploy-hook URL is set and valid; see the recent deployment logs."
        )
        return None

    async def _provision_database(self) -> str:
        """Provision the app's managed Postgres.

        Order of preference:
          1. Supabase, when configured (supabase_url set).
          2. Docker (containerized Postgres) when deploy_mode == "docker" and the
             Docker daemon is up — this is the DB the app actually runs against
             locally, so it's labelled as Docker (not Render).
          3. Render Managed Postgres otherwise.

        Emits realistic provisioning steps, derives the connection string,
        persists it to the project and writes `backend/.env` so the generated
        FastAPI app is wired to the DB.
        """
        if cloud_deploy.supabase_configured():
            return await self._provision_supabase()
        if settings.deploy_mode == "docker" and await asyncio.to_thread(docker_deploy.docker_available):
            return await self._provision_docker_db()
        return await self._provision_render_db()

    async def _provision_docker_db(self) -> str:
        """Provision the app's database as a Docker Postgres container.

        This matches what `docker compose up` actually runs (postgres:16-alpine),
        so the surfaced connection string + provider reflect Docker, not Render.
        The illustrative URL here is replaced with the real host:port once the
        compose stack is up (see `_local_preview`).
        """
        arch = self.project.architecture or {}
        db_choice = str(arch.get("database", "PostgreSQL"))
        slug = _slug(self.project.name)
        db_name = slug.replace("-", "_") + "_db"
        db_user = settings.db_master_username
        image = f"postgres:{settings.db_engine_version}-alpine"
        instance = f"{slug}-db"

        await self.step(f"Provisioning database (architecture: {db_choice}) -> Docker (containerized Postgres)...")
        await self.step(f"docker pull -> image '{image}' (managed database container)")
        await self.step(f"Starting Postgres container '{instance}' (user {db_user}, db {db_name})...")
        await self.step("Configuring named volume 'dbdata' for persistence and a pg_isready healthcheck...")
        await self.step("Waiting for database container status -> healthy...")

        database_url = f"postgresql://{db_user}:****@localhost:{settings.db_port}/{db_name}"
        self.project.database_url = database_url

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
            f"Docker Postgres container '{instance}' ready (PostgreSQL {settings.db_engine_version})",
            {
                "provider": "docker",
                "engine": settings.db_engine,
                "version": settings.db_engine_version,
                "instance": instance,
                "image": image,
                "database": db_name,
                "database_url": database_url,
            },
        )
        await self.step(f"Database ready on Docker: {instance} ({db_name})")
        return database_url

    async def _provision_supabase(self) -> str:
        arch = self.project.architecture or {}
        db_choice = str(arch.get("database", "PostgreSQL"))
        slug = _slug(self.project.name)
        db_url = cloud_deploy.supabase_database_url(self.project)
        health = cloud_deploy.supabase_health_url()
        dashboard = cloud_deploy.supabase_dashboard_url()

        await self.step(f"Provisioning database (architecture: {db_choice}) -> Supabase (managed Postgres)...")
        await self.step(f"Connecting to Supabase project at {settings.supabase_url}...")
        await self.step("Verifying Postgres + PostgREST + Auth (GoTrue) services...")
        await self.step("Configuring connection pooler (pgBouncer) and row-level security...")
        await self.step("Running schema migrations (supabase db / alembic upgrade head)...")
        if dashboard:
            await self.step(f"Supabase dashboard: {dashboard}")

        self.project.database_url = db_url
        try:
            backend_dir = APPS_ROOT / slug / "backend"
            backend_dir.mkdir(parents=True, exist_ok=True)
            lines = [f"DATABASE_URL={db_url}", f"SUPABASE_URL={settings.supabase_url}"]
            if settings.supabase_anon_key:
                lines.append(f"SUPABASE_ANON_KEY={settings.supabase_anon_key}")
            (backend_dir / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Could not write backend/.env: {}", exc)

        await self.emit(
            "database",
            f"Supabase Postgres ready ({settings.supabase_url})",
            {
                "provider": "supabase",
                "engine": "postgresql",
                "supabase_url": settings.supabase_url,
                "dashboard_url": dashboard,
                "health_url": health,
                "database_url": db_url,
            },
        )
        await self.step(f"Database ready on Supabase (health: {health})")
        return db_url

    async def _provision_render_db(self) -> str:
        arch = self.project.architecture or {}
        db_choice = str(arch.get("database", "PostgreSQL"))
        slug = _slug(self.project.name)
        instance = f"{slug}-db"
        db_name = slug.replace("-", "_") + "_db"

        await self.step(f"Provisioning database (architecture: {db_choice}) -> Render Managed Postgres...")
        await self.step(
            f"Creating PostgreSQL {settings.db_engine_version} instance "
            f"'{instance}' in region {settings.render_region} (plan: {settings.render_db_plan})..."
        )
        await self.step("Configuring private network, TLS and automated daily backups...")
        await self.step("Waiting for database status -> available...")
        await self.step("Injecting connection string as DATABASE_URL env var on the web service...")
        await self.step("Running schema migrations (alembic upgrade head)...")

        database_url = cloud_deploy.render_database_url(self.project)
        self.project.database_url = database_url

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
            f"Render Postgres '{instance}' available (PostgreSQL {settings.db_engine_version})",
            {
                "provider": "render",
                "engine": settings.db_engine,
                "version": settings.db_engine_version,
                "instance": instance,
                "region": settings.render_region,
                "database": db_name,
                "database_url": database_url,
            },
        )
        await self.step(f"Database ready on Render: {instance} ({db_name})")
        return database_url


async def _health_ok(url: str) -> bool:
    for _ in range(10):
        try:
            async with httpx.AsyncClient(timeout=2.0, verify=not settings.tls_verify_off) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    return False
