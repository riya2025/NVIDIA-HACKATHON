"""Continuous monitoring watchdog (multi-platform).

Runs from application startup (see main.py lifespan). On a fixed interval it
health-checks every deployed project across all of its platforms:

  - Backend   -> Render (or local) : GET <api_url>/health
  - Frontend  -> Vercel (or local) : GET <deploy_url>
  - Database  -> Supabase          : GET <supabase>/auth/v1/health

For each target it keeps a per-platform failure counter and:
  - keeps metrics green + emits a periodic combined "healthy" heartbeat, or
  - after `monitor_fail_threshold` consecutive failures, fires recovery:
      * healable target (backend) -> orchestrator.auto_heal (restart / Render redeploy)
      * managed target (Supabase / Vercel) -> orchestrator.auto_rca (RCA-only;
        we can't restart a managed platform, but we surface the incident + RCA).

This keeps the Monitoring + RCA + Self-Healing agents active for the whole
lifetime of the app and aware of changes on Render, Vercel and Supabase.
"""
from __future__ import annotations

import asyncio
from typing import List, NamedTuple, Optional

import httpx

from . import cloud_deploy, github_deploy, orchestrator
from .config import settings
from .events import Event, bus
from .logging_config import log
from .models import PROJECTS, Project

_task: Optional[asyncio.Task] = None
# Failure counter keyed by (project_id, platform_label).
_fail_counts: dict[tuple[str, str], int] = {}
# Projects we've already announced monitoring for (one-time "armed" stream line).
_armed: set[str] = set()
# Last GitHub Actions run id we've already handled per project (so a single
# failed CI run triggers RCA once, not on every tick).
_ci_seen: dict[str, int] = {}

# Pipeline states for which a project is considered "deployed and should be up".
_WATCHED = {"live", "healed", "degraded"}

_PLATFORM = {"backend": "Render", "frontend": "Vercel", "database": "Supabase"}


class Target(NamedTuple):
    label: str       # backend | frontend | database
    url: str
    healable: bool   # True if the watchdog can auto-recover it (restart/redeploy)


def _platform(label: str) -> str:
    return _PLATFORM.get(label, label)


def _targets(project: Project) -> List[Target]:
    """All health-check targets for a project across its platforms."""
    targets: List[Target] = []
    if project.api_url:
        targets.append(Target("backend", project.api_url.rstrip("/") + "/health", True))
    if project.deploy_url and project.deploy_url != project.api_url:
        # Frontend is healable when we can redeploy it (Vercel) or there's no
        # separate backend handling recovery (local preview).
        fe_healable = cloud_deploy.vercel_configured() or not project.api_url
        targets.append(Target("frontend", project.deploy_url, fe_healable))
    db_health = cloud_deploy.supabase_health_url()
    if db_health:
        # Supabase is managed — we can detect + RCA but not restart it.
        targets.append(Target("database", db_health, False))
    return targets


async def _healthy(url: str, retries: int = 2) -> bool:
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=4.0, verify=not settings.tls_verify_off) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        if attempt < retries:
            await asyncio.sleep(1.0)
    return False


def _ci_watched(project: Project) -> bool:
    """True when this project has a pushed GitHub repo we can poll for CI runs."""
    return bool(getattr(project, "repo_url", None)) and github_deploy.github_configured()


async def _check_ci(project: Project) -> None:
    """Poll the project's GitHub Actions and raise an incident + RCA when the
    latest workflow run has FAILED. CI lives on GitHub (not on the deployed
    service), so the HTTP health checks can't see it — this closes that gap.

    Fires RCA once per failed run (tracked via `_ci_seen`)."""
    if not _ci_watched(project):
        return
    run = await asyncio.to_thread(github_deploy.latest_workflow_run, project.repo_url)
    if not run or not run.get("id"):
        return
    # Only act on finished runs; ignore queued / in_progress.
    if run.get("status") != "completed":
        return
    run_id = run["id"]
    if _ci_seen.get(project.id) == run_id:
        return  # already handled this exact run
    _ci_seen[project.id] = run_id

    if run.get("conclusion") != "failure":
        return  # success / cancelled / skipped — nothing to investigate

    url = run.get("html_url") or project.repo_url
    title = (
        f"CI failed: GitHub Actions run '{run.get('name') or 'workflow'}' "
        f"on {run.get('branch') or 'main'}"
    )
    logs = (
        f"GitHub Actions workflow run {run_id} concluded 'failure' "
        f"(event={run.get('event')}, branch={run.get('branch')}). "
        f"Run URL: {url}. Repo: {project.repo_url}."
    )
    await bus.publish(
        Event(
            project_id=project.id,
            agent="monitoring",
            type="alert",
            message=f"ALERT: GitHub Actions CI failed for {project.name} -> {url}",
            data={"platform": "GitHub Actions", "label": "ci", "url": url, "run_id": run_id},
        )
    )
    log.bind(agent="monitoring").warning("CI failure detected for {}: {}", project.name, url)
    # CI can't be auto-restarted from here, so run RCA-only (like a managed platform).
    await orchestrator.auto_rca(project, title=title, logs=logs, component="ci")


async def _tick(beat: int) -> None:
    for project in list(PROJECTS.values()):
        if project.pipeline_status not in _WATCHED:
            continue
        # Don't probe a project that's mid-heal — it's expected to be down.
        if orchestrator.heal_in_progress(project.id):
            continue
        # GitHub Actions CI is part of the flow too — check it alongside runtime
        # health so a failed pipeline triggers RCA.
        await _check_ci(project)
        targets = _targets(project)
        if not targets:
            continue

        results = [(t, await _healthy(t.url)) for t in targets]

        # One-time: announce that the watchdog is now guarding this app.
        if project.id not in _armed:
            _armed.add(project.id)
            labels = [f"{_platform(t.label)} ({t.label})" for t in targets]
            data_targets = [
                {"label": t.label, "platform": _platform(t.label), "url": t.url} for t in targets
            ]
            if _ci_watched(project):
                labels.append("GitHub Actions (ci)")
                data_targets.append(
                    {"label": "ci", "platform": "GitHub Actions", "url": project.repo_url}
                )
            plats = ", ".join(labels)
            await bus.publish(
                Event(
                    project_id=project.id,
                    agent="monitoring",
                    type="monitoring_armed",
                    message=(
                        f"Continuous monitoring ARMED across {len(data_targets)} platform(s): {plats}. "
                        f"Checking every {int(settings.monitor_interval_s)}s. Auto RCA + Self-Heal on failure."
                    ),
                    data={
                        "targets": data_targets,
                        "interval_s": settings.monitor_interval_s,
                    },
                )
            )

        recovery_fired = False
        all_ok = True
        for t, ok in results:
            key = (project.id, t.label)
            if ok:
                _fail_counts[key] = 0
                continue

            all_ok = False
            _fail_counts[key] = _fail_counts.get(key, 0) + 1
            fails = _fail_counts[key]
            log.bind(agent="monitoring").warning(
                "{} ({}) health check failed ({}/{}): {}",
                _platform(t.label), t.label, fails, settings.monitor_fail_threshold, t.url,
            )
            await bus.publish(
                Event(
                    project_id=project.id,
                    agent="monitoring",
                    type="alert",
                    message=(
                        f"ALERT: {_platform(t.label)} {t.label} of {project.name} failed "
                        f"({fails}/{settings.monitor_fail_threshold}) -> {t.url}"
                    ),
                    data={"platform": _platform(t.label), "label": t.label, "url": t.url},
                )
            )

            if fails >= settings.monitor_fail_threshold and not recovery_fired:
                _fail_counts[key] = 0
                recovery_fired = True
                title = f"{_platform(t.label)} {t.label} unhealthy: {t.url} not responding 200"
                logs = (
                    f"Watchdog detected {fails} consecutive failed health checks for "
                    f"GET {t.url} ({_platform(t.label)} {t.label}). No HTTP 200."
                )
                if t.healable:
                    await orchestrator.auto_heal(project, title=title, logs=logs, component=t.label)
                else:
                    # Managed platform (Supabase DB) — can't restart it; RCA only.
                    await orchestrator.auto_rca(project, title=title, logs=logs, component=t.label)

        if all_ok:
            m = project.metrics
            if m.status != "healthy":
                m.status, m.error_rate = "healthy", 0.0
                if project.pipeline_status == "degraded":
                    project.pipeline_status = "live"
            # Heartbeat on EVERY sweep so the live stream continuously shows the
            # Monitoring agent actively watching (proves it's on the whole time).
            # Every `monitor_heartbeat_every` sweeps, include the full metrics
            # payload (drives the metrics widget); other sweeps are a light pulse.
            summary = "  ".join(f"{_platform(t.label)}:OK" for t, _ in results)
            full = beat % settings.monitor_heartbeat_every == 0
            await bus.publish(
                Event(
                    project_id=project.id,
                    agent="monitoring",
                    type="metrics",
                    message=(
                        f"Monitoring sweep #{beat}: all targets healthy "
                        f"({len(results)} checked) -> {summary}; status=healthy "
                        f"(next in {int(settings.monitor_interval_s)}s)"
                    ),
                    data={
                        "metrics": m.model_dump(),
                        "sweep": beat,
                        "checked": len(results),
                        "interval_s": settings.monitor_interval_s,
                        "full": full,
                    },
                )
            )


async def _loop() -> None:
    log.bind(agent="monitoring").info(
        "Continuous monitoring started (interval={}s, fail-threshold={})",
        settings.monitor_interval_s, settings.monitor_fail_threshold,
    )
    beat = 0
    while True:
        try:
            await _tick(beat)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.bind(agent="monitoring").warning("monitor tick error: {}", exc)
        beat += 1
        await asyncio.sleep(settings.monitor_interval_s)


def start() -> None:
    """Launch the watchdog (idempotent)."""
    global _task
    if not settings.monitor_enabled:
        log.bind(agent="monitoring").info("Continuous monitoring disabled (monitor_enabled=False)")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
