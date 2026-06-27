"""Shared graph state and context-management helpers.

`FoundryState` is the typed context that flows between LangGraph nodes. Most
keys are written by exactly one node (so parallel codegen branches never clash);
`context_log` is an accumulator channel with an ``operator.add`` reducer so every
node can append a short narrative line that downstream agents can read.

The `Project` object in `PROJECTS` remains the durable cross-graph store (it
already persists architecture, metrics and incidents), so nodes load it via
`load_project` and mutate it exactly as the agents do today.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from ..models import PROJECTS, Project


class FoundryState(TypedDict, total=False):
    # Identity / requirement (seeded once at graph entry).
    project_id: str
    name: str
    description: str
    app_type: str

    # Produced by the build graph.
    architecture: Dict[str, Any]
    frontend_result: Dict[str, Any]
    backend_result: Dict[str, Any]
    devops_result: Dict[str, Any]
    test_results: Dict[str, Any]
    deploy_url: Optional[str]
    database_url: Optional[str]
    healthy: bool
    metrics: Dict[str, Any]

    # Self-healing loop context.
    incident_id: str
    incident_title: str
    logs: str
    root_cause: str
    action: str

    # Accumulating narrative shared across nodes (reducer channel).
    context_log: Annotated[List[str], operator.add]


def load_project(state: FoundryState) -> Project:
    """Fetch the durable Project for this graph run.

    Raises KeyError if the project was evicted; callers run inside the graph
    where the project is guaranteed to exist for the lifetime of the pipeline.
    """
    return PROJECTS[state["project_id"]]


def hydrate(project: Project) -> FoundryState:
    """Seed initial graph state from a Project (build-graph entry)."""
    return FoundryState(
        project_id=project.id,
        name=project.name,
        description=project.description,
        app_type=project.app_type,
        context_log=[f"build started for {project.name!r}"],
    )


def hydrate_incident(project: Project, incident, logs: str = "") -> FoundryState:
    """Seed graph state for the heal/rca path, carrying build context forward.

    The architecture and prior context live on the Project, so the RCA agent can
    reason with awareness of the stack and generated code, not just raw logs.
    """
    return FoundryState(
        project_id=project.id,
        name=project.name,
        description=project.description,
        app_type=project.app_type,
        architecture=project.architecture or {},
        incident_id=incident.id,
        incident_title=incident.title,
        logs=logs,
        context_log=[f"incident raised: {incident.title}"],
    )
