"""Orchestrates the 7-agent pipeline and the self-healing loop."""
from __future__ import annotations

from .agents.architect import ArchitectAgent
from .agents.deployment import DeploymentAgent
from .agents.developer import DeveloperAgent
from .agents.healing import HealingAgent
from .agents.monitoring import MonitoringAgent
from .agents.rca import RCAAgent
from .agents.tester import TesterAgent
from .events import Event, bus
from .local_deploy import stop as stop_local
from .logging_config import log
from .models import Incident, Project


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
    """Generate -> Test -> Deploy -> Monitor."""
    project.pipeline_status = "running"
    await _emit_pipeline(project, "Pipeline started")
    log.bind(agent="orchestrator").info("Build pipeline started for {}", project.name)

    try:
        await ArchitectAgent(project).run()
        await DeveloperAgent(project).run()
        await TesterAgent(project).run()
        await DeploymentAgent(project).run()
        await MonitoringAgent(project).run()
        await _emit_pipeline(project, "Application live and healthy")
    except Exception as exc:  # noqa: BLE001
        project.pipeline_status = "failed"
        await _emit_pipeline(project, f"Pipeline failed: {exc}")


async def trigger_incident(project: Project, title: str = "Backend service down (ECS task crashed)") -> None:
    """Simulate a failure, then run RCA -> Self-Healing (the demo's wow moment)."""
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

    await RCAAgent(project, incident).run()
    await HealingAgent(project, incident, auto_approve=True).run()

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
    Monitoring + RCA agents actually catch a frontend bug.
    """
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

    # RCA analyzes the real browser error/stack (Nemotron + GraphRAG).
    await RCAAgent(project, incident, logs=f"Browser error: {message}\nStack:\n{stack[:1500]}").run()
