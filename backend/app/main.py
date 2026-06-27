"""AI Foundry control-plane API: REST + WebSocket live event stream."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .cache import install_langchain_llm_cache
from .config import settings
from .events import bus
from .logging_config import log
from .models import PROJECTS, CreateProjectRequest, Project
from .nvidia_client import nvidia
from .orchestrator import report_client_error, run_build_pipeline, trigger_incident
from pydantic import BaseModel

app = FastAPI(title="AI Foundry Control Plane", version=__version__)

# Cache ChatNVIDIA (RCA ReAct agent) responses via LangChain's global cache.
install_langchain_llm_cache()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "nvidia_live": nvidia.live, "demo_mode": settings.demo_mode}


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    return [p.model_dump() for p in PROJECTS.values()]


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    project = PROJECTS.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project.model_dump()


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest) -> dict:
    project = Project(name=req.name, description=req.description, app_type=req.app_type)
    PROJECTS[project.id] = project
    log.info("Created project {} ({})", project.name, project.id)
    # Run the build pipeline in the background; UI follows via WebSocket.
    asyncio.create_task(run_build_pipeline(project))
    return project.model_dump()


@app.post("/api/projects/{project_id}/incident")
async def cause_incident(project_id: str) -> dict:
    project = PROJECTS.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    asyncio.create_task(trigger_incident(project))
    return {"ok": True, "message": "Incident triggered; self-healing loop started."}


class ClientError(BaseModel):
    message: str = ""
    source: str = ""
    line: int = 0
    col: int = 0
    stack: str = ""


@app.post("/api/projects/{project_id}/client-error")
async def client_error(project_id: str, err: ClientError) -> dict:
    """Beacon endpoint: generated apps post browser-side JS crashes here."""
    project = PROJECTS.get(project_id)
    if not project:
        return {"ok": False}
    asyncio.create_task(report_client_error(project, err.message, err.stack))
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue = bus.subscribe()
    log.info("WebSocket client connected")
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
        log.info("WebSocket client disconnected")
