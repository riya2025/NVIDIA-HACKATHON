"""Drives the agent pipeline via in-process LangGraph graphs.

The public entry points (`run_build_pipeline`, `trigger_incident`,
`report_client_error`) keep their signatures so the FastAPI layer is unchanged.
Their bodies now invoke compiled LangGraph `StateGraph`s; each node wraps an
existing agent, so live events still stream to the WebSocket bus.
"""
from __future__ import annotations

import time

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


async def trigger_incident(project: Project, title: str = "Backend service down (ECS task crashed)") -> None:
    """Simulate a failure, then run the LangGraph heal loop: RCA -> Self-Healing."""
    project.pipeline_status = "degraded"
    # Really take the running service down so the deploy URL goes unreachable.
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

    await heal_graph.ainvoke(hydrate_incident(project, incident), _config(project, "heal"))

    await bus.publish(
        Event(
            project_id=project.id,
            agent="healing",
            type="healed",
            message="Self-healing complete. Service restored.",
            data={"incident_id": incident.id, "metrics": project.metrics.model_dump()},
        )
    )


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
