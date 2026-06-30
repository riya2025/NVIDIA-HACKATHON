"""LangGraph node wrappers around the existing agents.

Each node loads the durable `Project`, runs the corresponding agent (which emits
live events and mutates the Project as it always has), then returns the slice of
`FoundryState` it owns plus a one-line `context_log` entry. Returning distinct
keys keeps the parallel codegen branches conflict-free.
"""
from __future__ import annotations

from typing import Any, Dict

from ..agents.architect import ArchitectAgent
from ..agents.deployment import DeploymentAgent
from ..agents.developer import BackendAgent, DevOpsAgent, FrontendAgent
from ..agents.healing import HealingAgent
from ..agents.monitoring import MonitoringAgent
from ..agents.rca import RCAAgent
from ..agents.tester import TesterAgent
from ..models import Incident
from .state import FoundryState, load_project


async def architect_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    out = await ArchitectAgent(project).run()
    return {
        "architecture": out,
        "context_log": [
            f"architect: {out.get('frontend')} / {out.get('backend')} / "
            f"{out.get('database')} on {out.get('deployment')}"
        ],
    }


async def frontend_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    out = await FrontendAgent(project).run()
    return {
        "frontend_result": out,
        "context_log": [f"frontend: {out.get('frontend')} (fallback={out.get('frontend_fallback')})"],
    }


async def backend_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    out = await BackendAgent(project).run()
    return {
        "backend_result": out,
        "context_log": [f"backend: {out.get('backend')}"],
    }


async def devops_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    out = await DevOpsAgent(project).run()
    return {
        "devops_result": out,
        "context_log": [f"devops: {out.get('deployment')}"],
    }


def codegen_join(state: FoundryState) -> Dict[str, Any]:
    """Barrier node: runs after all three codegen subagents complete."""
    files = []
    for key in ("frontend_result", "backend_result", "devops_result"):
        files += list((state.get(key) or {}).get("files", []))
    return {"context_log": [f"codegen complete: {len(files)} files written"]}


async def tester_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    out = await TesterAgent(project).run()
    return {"test_results": out, "context_log": [f"tests passed={out.get('passed')}"]}


async def deployment_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    out = await DeploymentAgent(project).run()
    return {
        "deploy_url": out.get("deploy_url"),
        "database_url": out.get("database_url"),
        "healthy": out.get("healthy", False),
        "metrics": project.metrics.model_dump(),
        "context_log": [
            f"deployed: {out.get('deploy_url')} api={out.get('api_url')} "
            f"db={out.get('database_url')} healthy={out.get('healthy')}"
        ],
    }


async def monitoring_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    out = await MonitoringAgent(project).run()
    return {
        "healthy": out.get("healthy", False),
        "metrics": out.get("metrics", {}),
        "context_log": [f"monitoring: healthy={out.get('healthy')}"],
    }


def _incident_for(project, state: FoundryState) -> Incident:
    """Resolve the live Incident object this heal run is about."""
    iid = state.get("incident_id")
    for inc in project.incidents:
        if inc.id == iid:
            return inc
    # Fall back to the most recent incident if the id wasn't seeded.
    return project.incidents[-1]


def _enriched_logs(state: FoundryState) -> str:
    """Give RCA the architecture + code summary as context, not just raw logs."""
    arch = state.get("architecture") or {}
    arch_line = ", ".join(f"{k}={v}" for k, v in arch.items() if k != "rationale") or "unknown"
    ctx = " | ".join(state.get("context_log", [])[-6:])
    base = state.get("logs") or ""
    return (
        f"{base}\n"
        f"Stack: {arch_line}\n"
        f"Build/runtime context: {ctx}"
    ).strip()


async def rca_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    incident = _incident_for(project, state)
    out = await RCAAgent(project, incident, logs=_enriched_logs(state)).run()
    return {
        "root_cause": out.get("root_cause", ""),
        "context_log": [f"rca: {str(out.get('root_cause', ''))[:120]}"],
    }


async def healing_node(state: FoundryState) -> Dict[str, Any]:
    project = load_project(state)
    incident = _incident_for(project, state)
    out = await HealingAgent(project, incident, auto_approve=True).run()
    return {
        "action": out.get("action", ""),
        "deploy_url": project.deploy_url,
        "healthy": project.metrics.status == "healthy",
        "metrics": project.metrics.model_dump(),
        "context_log": [f"healing: resolved={out.get('resolved')}"],
    }
