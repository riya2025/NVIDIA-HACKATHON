"""Drives the agent pipeline via in-process LangGraph graphs.

The public entry points (`run_build_pipeline`, `trigger_incident`,
`report_client_error`) keep their signatures so the FastAPI layer is unchanged.
Their bodies now invoke compiled LangGraph `StateGraph`s; each node wraps an
existing agent, so live events still stream to the WebSocket bus.
"""
from __future__ import annotations

import asyncio
import time

from . import docker_deploy
from .config import settings
from .events import Event, bus
from .graph import build_graph, heal_graph, hydrate, rca_graph
from .graph.state import hydrate_incident
from .local_deploy import stop as stop_local
from .logging_config import log
from .models import Incident, Project

# Debounce browser client-error beacons: a single broken generated app fires the
# beacon on every (re)load / event, and each one used to spawn a full RCA. Track
# the last error signature + time per project, and which projects have an RCA in
# flight, so we collapse the storm into one investigation.
_last_client_error: dict[str, tuple[float, str]] = {}
_rca_in_flight: set[str] = set()
# Projects with a heal (RCA -> Self-Healing) loop currently running, so the
# watchdog and the manual incident button can't stack overlapping heals.
_heal_in_flight: set[str] = set()


def heal_in_progress(project_id: str) -> bool:
    return project_id in _heal_in_flight


def _config(project: Project, suffix: str) -> dict:
    """Per-graph thread id so build/heal/rca checkpoints never collide."""
    return {"configurable": {"thread_id": f"{project.id}:{suffix}"}}


async def _emit_pipeline(project: Project, message: str) -> None:
    await bus.publish(
        Event(
            project_id=project.id,
            agent="orchestrator",
            type="pipeline",
            message=message,
            data={"pipeline_status": project.pipeline_status},
        )
    )


async def run_build_pipeline(project: Project) -> None:
    """architect -> codegen (parallel) -> tester -> deploy -> monitor (LangGraph)."""
    project.pipeline_status = "running"
    await _emit_pipeline(project, "Pipeline started")
    log.bind(agent="orchestrator").info("Build pipeline started for {}", project.name)

    try:
        await _emit_pipeline(project, "Generating code in parallel (frontend + backend + devops)")
        await build_graph.ainvoke(hydrate(project), _config(project, "build"))
        await _emit_pipeline(project, "Application live and healthy")
    except Exception as exc:  # noqa: BLE001
        project.pipeline_status = "failed"
        await _emit_pipeline(project, f"Pipeline failed: {exc}")


async def _run_heal(project: Project, incident: Incident, logs: str = "") -> None:
    """Run the LangGraph heal loop (RCA -> Self-Healing) and announce recovery."""
    state = hydrate_incident(project, incident, logs=logs)
    await heal_graph.ainvoke(state, _config(project, "heal"))
    await bus.publish(
        Event(
            project_id=project.id,
            agent="healing",
            type="healed",
            message="Self-healing complete. Service restored.",
            data={"incident_id": incident.id, "metrics": project.metrics.model_dump()},
        )
    )


async def trigger_incident(project: Project, title: str = "Backend service down (Render web service crashed)") -> None:
    """Simulate a failure (chaos test), then run RCA -> Self-Healing."""
    if project.id in _heal_in_flight:
        log.bind(agent="monitoring").info("incident ignored (heal already in flight)")
        return
    _heal_in_flight.add(project.id)
    try:
        project.pipeline_status = "degraded"
        # Really take the running service down so the deploy URL goes unreachable.
        if docker_deploy.current_urls(project):
            await asyncio.to_thread(docker_deploy.kill, project)
        else:
            stop_local(project)
        m = project.metrics
        m.status, m.cpu, m.memory, m.latency_ms, m.error_rate = "unhealthy", 0.0, 0.0, 0.0, 100.0

        incident = Incident(title=title)
        project.incidents.append(incident)

        await bus.publish(
            Event(
                project_id=project.id,
                agent="monitoring",
                type="incident",
                message=f"INCIDENT: {title}",
                data={"incident_id": incident.id, "metrics": m.model_dump()},
            )
        )
        log.bind(agent="monitoring").warning("Incident raised: {}", title)

        await _run_heal(project, incident, logs="Simulated outage: service stopped by operator (chaos test).")
    finally:
        _heal_in_flight.discard(project.id)


async def auto_heal(project: Project, title: str, logs: str = "", component: str = "backend") -> None:
    """Watchdog-driven recovery: a real health check failed, so run RCA ->
    Self-Healing without simulating an outage (the service is already down)."""
    if project.id in _heal_in_flight or project.id in _rca_in_flight:
        return
    _heal_in_flight.add(project.id)
    try:
        project.pipeline_status = "degraded"
        m = project.metrics
        m.status, m.error_rate = "unhealthy", 100.0

        incident = Incident(title=title, component=component)
        project.incidents.append(incident)

        await bus.publish(
            Event(
                project_id=project.id,
                agent="monitoring",
                type="incident",
                message=f"INCIDENT (auto-detected): {title}",
                data={"incident_id": incident.id, "metrics": m.model_dump()},
            )
        )
        log.bind(agent="monitoring").warning("Auto-detected incident: {}", title)

        await _run_heal(project, incident, logs=logs)
    finally:
        _heal_in_flight.discard(project.id)


async def auto_rca(project: Project, title: str, logs: str = "", component: str = "database") -> None:
    """Watchdog-driven RCA for a platform we can't auto-fix (e.g. a managed
    Supabase database). Raises an incident and runs the RCA-only LangGraph path
    (no rollback). Debounced so one outage doesn't spawn repeated investigations."""
    if project.id in _rca_in_flight or project.id in _heal_in_flight:
        return

    project.pipeline_status = "degraded"
    m = project.metrics
    m.status, m.error_rate = "unhealthy", 100.0

    incident = Incident(title=title, component=component)
    project.incidents.append(incident)
    await bus.publish(
        Event(
            project_id=project.id,
            agent="monitoring",
            type="incident",
            message=f"INCIDENT (auto-detected): {title}",
            data={"incident_id": incident.id, "metrics": m.model_dump()},
        )
    )
    log.bind(agent="monitoring").warning("Auto-detected platform incident: {}", title)

    _rca_in_flight.add(project.id)
    try:
        await rca_graph.ainvoke(hydrate_incident(project, incident, logs=logs), _config(project, "rca"))
    finally:
        _rca_in_flight.discard(project.id)


async def report_deploy_failure(
    project: Project,
    title: str,
    logs: str = "",
    component: str = "deployment",
    retry=None,
) -> None:
    """A deploy-TIME step failed (e.g. GitHub push, Render service create, Vercel
    deploy). The runtime watchdog can't see this — there's no live cloud URL to
    health-check — so this is how the Monitoring + RCA (+ Self-Healing) agents
    catch "the backend never deployed". Surfaces a Monitoring incident (visible in
    the live stream), runs RCA, then — if a `retry` coroutine is provided —
    Self-Healing re-attempts the failed cloud step once and marks the incident
    resolved if it recovers. We do NOT mark the whole pipeline failed because the
    always-on local/Docker preview stays live; this only flags the cloud target
    that didn't come up. Debounced via the shared in-flight guards.

    `retry`: optional `async () -> dict | None` that re-runs the failed deploy and
    returns any recovered URLs (truthy = healed)."""
    if project.id in _rca_in_flight or project.id in _heal_in_flight:
        log.bind(agent="monitoring").debug("deploy-failure RCA skipped (already in flight)")
        return

    _heal_in_flight.add(project.id)
    try:
        incident = Incident(title=title, component=component)
        project.incidents.append(incident)
        await bus.publish(
            Event(
                project_id=project.id,
                agent="monitoring",
                type="incident",
                message=f"INCIDENT (deploy-time, auto-detected): {title}",
                data={"incident_id": incident.id, "component": component, "phase": "deploy"},
            )
        )
        log.bind(agent="monitoring").warning("Deploy-time incident auto-detected: {}", title)

        # ---- RCA (root cause) ----
        _rca_in_flight.add(project.id)
        try:
            await rca_graph.ainvoke(hydrate_incident(project, incident, logs=logs), _config(project, "rca"))
        finally:
            _rca_in_flight.discard(project.id)

        # ---- Self-Healing (retry the failed cloud step once) ----
        if retry is None:
            return
        await bus.publish(
            Event(
                project_id=project.id,
                agent="healing",
                type="step",
                message="Self-Healing: re-attempting the failed cloud deploy step (1 retry)...",
                data={"incident_id": incident.id},
            )
        )
        log.bind(agent="healing").info("Self-healing retry for deploy-time failure: {}", title)
        recovered = None
        try:
            recovered = await retry()
        except Exception as exc:  # noqa: BLE001
            log.bind(agent="healing").warning("Self-heal retry raised: {}", exc)

        if recovered:
            incident.resolved = True
            incident.action = "Re-ran the failed cloud deploy step; target is now live."
            await bus.publish(
                Event(
                    project_id=project.id,
                    agent="healing",
                    type="healed",
                    message=f"Self-healing complete: cloud target recovered on retry ({recovered}).",
                    data={"incident_id": incident.id, "recovered": recovered},
                )
            )
            log.bind(agent="healing").info("Deploy-time self-heal succeeded: {}", recovered)
        else:
            incident.action = "Retry did not recover the cloud target; manual intervention required (see RCA)."
            await bus.publish(
                Event(
                    project_id=project.id,
                    agent="healing",
                    type="step",
                    message="Self-healing retry did not recover the cloud target — manual fix needed (see RCA root cause).",
                    data={"incident_id": incident.id},
                )
            )
            log.bind(agent="healing").warning("Deploy-time self-heal retry did not recover the target.")
    finally:
        _heal_in_flight.discard(project.id)


async def report_client_error(project: Project, message: str, stack: str = "") -> None:
    """A real JS error in the deployed app reported back via the client beacon.

    The HTTP health check can't see browser-side crashes, so this is how the
    Monitoring + RCA agents actually catch a frontend bug. Runs the RCA-only
    LangGraph path (no automatic rollback). Debounced so a single broken app
    can't spawn an RCA storm.
    """
    if not settings.client_error_rca:
        return

    signature = (message or "").strip()[:120]
    now = time.time()

    # Skip if an RCA for this project is already running (collapse concurrent beacons).
    if project.id in _rca_in_flight:
        log.bind(agent="monitoring").debug("client-error ignored (RCA already in flight)")
        return
    # Skip repeats of the same error within the cooldown window.
    last = _last_client_error.get(project.id)
    if last is not None and last[1] == signature and (now - last[0]) < settings.client_error_cooldown_s:
        log.bind(agent="monitoring").debug("client-error debounced (repeat within cooldown)")
        return
    _last_client_error[project.id] = (now, signature)

    project.pipeline_status = "degraded"
    m = project.metrics
    m.status, m.error_rate = "unhealthy", 100.0

    incident = Incident(title=f"Frontend JS error: {message[:140]}")
    project.incidents.append(incident)

    await bus.publish(
        Event(
            project_id=project.id,
            agent="monitoring",
            type="client_error",
            message=f"CLIENT ERROR detected in browser: {message[:160]}",
            data={"incident_id": incident.id, "stack": stack[:600]},
        )
    )
    log.bind(agent="monitoring").warning("Client-side error reported: {}", message[:160])

    logs = f"Browser error: {message}\nStack:\n{stack[:1500]}"
    _rca_in_flight.add(project.id)
    try:
        await rca_graph.ainvoke(hydrate_incident(project, incident, logs=logs), _config(project, "rca"))
    finally:
        _rca_in_flight.discard(project.id)
