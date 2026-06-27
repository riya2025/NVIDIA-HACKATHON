"""Standalone LangGraph Architect agent, served via NAT's `langgraph_wrapper`.

A minimal `MessagesState` graph with a single `design` node that turns an app
requirement into a concrete JSON tech design. The `langgraph_wrapper` invokes
the compiled graph directly (no NAT builder/context is injected into node
execution), so this agent is fully self-contained: it builds its own NVIDIA NIM
chat model. The model + key are read from the environment, which NAT loads from
the `.env` referenced in `langgraph_config.yml`.

Exports a compiled `graph` (`CompiledStateGraph`), as the wrapper expects.
"""
from __future__ import annotations

# Corporate networks often intercept TLS; trust the OS store so NIM calls
# validate cleanly without disabling verification.
try:  # pragma: no cover - best effort
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

import os

from langchain_core.messages import SystemMessage
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langgraph.graph import END, START, MessagesState, StateGraph

ARCHITECT_SYSTEM = (
    "You are the AI Foundry Architect agent, a senior software architect. "
    "Given an application requirement, respond ONLY with a compact JSON object "
    "with keys: frontend, backend, database, deployment, rationale. "
    "Target an AWS ECS (Fargate) deployment."
)

ARCHITECT_MODEL = os.getenv("ARCHITECT_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")

_llm: ChatNVIDIA | None = None


def _get_llm() -> ChatNVIDIA:
    """Lazily build (and cache) the NIM chat model.

    `NVIDIA_API_KEY` is loaded into the environment by the wrapper before this
    module is imported, so a module-level singleton is safe; we still build it
    lazily to keep import side effects minimal.
    """
    global _llm
    if _llm is None:
        _llm = ChatNVIDIA(
            model=ARCHITECT_MODEL,
            api_key=os.environ.get("NVIDIA_API_KEY"),
            temperature=0.3,
            max_tokens=700,
        )
    return _llm


def design(state: MessagesState) -> dict:
    """Single reasoning step: requirement messages -> JSON design."""
    response = _get_llm().invoke([SystemMessage(content=ARCHITECT_SYSTEM), *state["messages"]])
    return {"messages": [response]}


_builder = StateGraph(MessagesState)
_builder.add_node("design", design)
_builder.add_edge(START, "design")
_builder.add_edge("design", END)

graph = _builder.compile()
