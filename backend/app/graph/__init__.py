"""In-process LangGraph orchestration for the AI Foundry agent pipeline.

The custom sequential orchestrator is replaced by two compiled LangGraph
`StateGraph`s that share a typed `FoundryState` and a `MemorySaver` checkpointer:

  - `build_graph`  : architect -> codegen supervisor (frontend/backend/devops in
                     parallel) -> tester -> deployment -> monitoring.
  - `heal_graph`   : rca -> healing (the self-healing loop).

Each node wraps an existing agent so the proven LLM calls, file writes and live
event emission to the WebSocket bus are preserved.
"""
from __future__ import annotations

from .build_graph import build_graph
from .heal_graph import heal_graph, rca_graph
from .state import FoundryState, hydrate

__all__ = ["build_graph", "heal_graph", "rca_graph", "FoundryState", "hydrate"]
