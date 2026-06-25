"""Pydantic models and in-memory state store."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Stage(str, Enum):
    architect = "architect"
    developer = "developer"
    tester = "tester"
    deployment = "deployment"
    monitoring = "monitoring"
    rca = "rca"
    healing = "healing"


class AgentStatus(str, Enum):
    idle = "idle"
    running = "running"
    success = "success"
    failed = "failed"


class CreateProjectRequest(BaseModel):
    name: str = Field(..., examples=["Pothole Reporting App"])
    description: str = Field(
        ...,
        examples=["Build a pothole reporting app with user login, map, and admin dashboard."],
    )
    app_type: str = "saas-web-app"


class AgentState(BaseModel):
    stage: Stage
    status: AgentStatus = AgentStatus.idle
    output: Optional[Dict[str, Any]] = None
    logs: List[str] = Field(default_factory=list)


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str
    root_cause: Optional[str] = None
    action: Optional[str] = None
    resolved: bool = False
    created_at: float = Field(default_factory=time.time)


class Metrics(BaseModel):
    status: str = "unknown"
    cpu: float = 0.0
    memory: float = 0.0
    latency_ms: float = 0.0
    error_rate: float = 0.0


class Project(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    description: str
    app_type: str
    created_at: float = Field(default_factory=time.time)
    agents: Dict[str, AgentState] = Field(default_factory=dict)
    architecture: Optional[Dict[str, Any]] = None
    deploy_url: Optional[str] = None
    metrics: Metrics = Field(default_factory=Metrics)
    incidents: List[Incident] = Field(default_factory=list)
    pipeline_status: str = "pending"  # pending|running|live|degraded|healed|failed


PROJECTS: Dict[str, Project] = {}
