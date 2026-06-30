"""RCA Agent: a LangGraph tool-calling ReAct agent.

The agent reasons over an incident using two tools:

  - `fetch_logs`             -> the raw logs / stack trace of the failing service
  - `query_incident_history` -> similar past incidents from long-term memory
                                (the GraphRAG "have we seen this before?" step)

It runs as a real `create_react_agent` graph (LangGraph + ChatNVIDIA). Each step
is streamed to the UI as an event. If the ReAct path fails for any reason (model
without tool-calling, TLS, etc.) it transparently falls back to a single
reasoning completion via the existing NIM client, so the demo never breaks.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..config import settings
from ..logging_config import log

# TLS handling for the ReAct path's ChatNVIDIA client. Unlike our httpx-based
# NIM client, ChatNVIDIA talks over aiohttp/urllib and ignores our insecure-SSL
# flag, so on SSL-intercepting networks (corporate proxy / antivirus) its calls
# fail cert verification. When tls_verify_off is set we disable verification for
# the default SSL context the aiohttp/urllib clients build (DEV ONLY); otherwise
# we trust the OS cert store via truststore so calls validate cleanly.
if settings.tls_verify_off:  # pragma: no cover - dev-only network shim
    import ssl

    try:
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        _orig_ctx = ssl.create_default_context

        def _unverified_ctx(*args, **kwargs):  # noqa: ANN001, ANN202
            ctx = _orig_ctx(*args, **kwargs)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        ssl.create_default_context = _unverified_ctx  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass
else:
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001
        pass
from ..models import Incident, Stage
from ..nvidia_client import nvidia
from .base import BaseAgent

SYSTEM = (
    "You are a Site Reliability RCA agent. Investigate the incident using your "
    "tools, then give a concise root cause and a single recommended remediation "
    "action. Always call fetch_logs first, and query_incident_history to check "
    "for similar past incidents before concluding. Keep the final answer to 2-3 "
    "sentences: 'Root cause: ... Recommended action: ...'."
)


class RCAAgent(BaseAgent):
    stage = Stage.rca
    title = "RCA Agent"

    def __init__(self, project, incident: Incident, logs: str = "") -> None:
        super().__init__(project)
        self.incident = incident
        self.logs = logs or "Render web service exit 137 (OOM), /health unhealthy, 503s."

    async def execute(self) -> Dict[str, Any]:
        await self.step(f"Investigating incident: {self.incident.title}")

        analysis = ""
        if settings.rca_use_react:
            try:
                analysis = await self._run_react()
            except Exception as exc:  # noqa: BLE001
                log.warning("RCA ReAct agent failed ({}); falling back to direct reasoning", exc)
                await self.step("ReAct agent unavailable; falling back to direct reasoning")

        if not analysis:
            analysis = await self._run_fallback()

        self.incident.root_cause = analysis
        await self.step(f"Root cause: {analysis}")
        self._remember()
        return {"root_cause": analysis}

    # ------------------------------------------------------------------ ReAct
    async def _run_react(self) -> str:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from langchain_core.tools import tool
        from langchain_nvidia_ai_endpoints import ChatNVIDIA
        from langgraph.prebuilt import create_react_agent

        # Runtime import avoids an import cycle (graph package -> nodes -> rca).
        from ..graph.memory import search_incidents
        from ..graph.persistence import store

        logs_text = self.logs
        incident_id = self.incident.id

        @tool
        def fetch_logs() -> str:
            """Return the raw incident logs / stack trace from the failing service."""
            return logs_text or "no logs captured"

        @tool
        def query_incident_history(query: str) -> str:
            """Search past incidents for similar root causes. Pass a short
            description of the current symptom (e.g. '503 OOM container exit')."""
            matches = search_incidents(store, query, exclude_id=incident_id)
            if not matches:
                return "No similar past incidents found."
            return "\n".join(
                f"- {m.get('title')} -> {str(m.get('root_cause', ''))[:160]} "
                f"(fix: {str(m.get('action', ''))[:80]})"
                for m in matches
            )

        await self.step("Spawning LangGraph ReAct agent (tools: fetch_logs, query_incident_history)...")

        system = SYSTEM
        if "nemotron" in settings.rca_model.lower():
            system = f"detailed thinking off\n\n{SYSTEM}"

        model = ChatNVIDIA(
            model=settings.rca_model,
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            temperature=0.2,
            max_tokens=900,
        )
        agent = create_react_agent(model, [fetch_logs, query_incident_history], prompt=system)

        human = (
            f"Incident: {self.incident.title}\n"
            f"Stack: {self._arch_line()}\n"
            f"Initial logs (also available via fetch_logs):\n{logs_text[:600]}"
        )
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=human)]},
            config={"recursion_limit": 2 * settings.rca_max_iterations + 1},
        )

        messages: List[Any] = result.get("messages", [])
        final = ""
        for msg in messages:
            if isinstance(msg, AIMessage):
                for call in (getattr(msg, "tool_calls", None) or []):
                    await self.step(f"-> tool call: {call.get('name')}({_short_args(call.get('args'))})")
                if isinstance(msg.content, str) and msg.content.strip():
                    final = msg.content.strip()
            elif isinstance(msg, ToolMessage):
                await self.step(f"<- {msg.name}: {str(msg.content)[:120]}")
        return final

    # --------------------------------------------------------------- Fallback
    async def _run_fallback(self) -> str:
        import asyncio

        await self.step(f"Reasoning with {settings.rca_model} (direct)...")
        return await asyncio.to_thread(
            nvidia.complete,
            f"Incident: {self.incident.title}\nStack: {self._arch_line()}\nLogs: {self.logs}",
            system=SYSTEM,
            model=settings.rca_model,
            max_tokens=600,
            temperature=0.2,
            thinking=False,
        )

    # ----------------------------------------------------------------- Helpers
    def _arch_line(self) -> str:
        arch = self.project.architecture or {}
        return ", ".join(f"{k}={v}" for k, v in arch.items() if k != "rationale") or "unknown"

    def _remember(self) -> None:
        try:
            from ..graph.memory import record_incident
            from ..graph.persistence import store

            record_incident(
                store,
                project_id=self.project.id,
                incident_id=self.incident.id,
                title=self.incident.title,
                root_cause=self.incident.root_cause or "",
                action=self.incident.action or "",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record incident to memory: {}", exc)


def _short_args(args: Any) -> str:
    if isinstance(args, dict):
        return ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
    return str(args)[:60]
