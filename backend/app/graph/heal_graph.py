"""Self-healing pipeline as LangGraph StateGraphs.

`heal_graph` runs the full incident response: rca -> healing. `rca_graph` is the
analysis-only path used for browser/client errors, where Monitoring + RCA catch
the bug but no automatic rollback is performed.

Both share the build graph's `MemorySaver` so a project's heal runs are
checkpointed under the same thread_id.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import healing_node, rca_node
from .persistence import checkpointer, store
from .state import FoundryState


def _build_heal_graph():
    g = StateGraph(FoundryState)
    g.add_node("rca", rca_node)
    g.add_node("healing", healing_node)
    g.add_edge(START, "rca")
    g.add_edge("rca", "healing")
    g.add_edge("healing", END)
    return g.compile(checkpointer=checkpointer, store=store)


def _build_rca_graph():
    g = StateGraph(FoundryState)
    g.add_node("rca", rca_node)
    g.add_edge(START, "rca")
    g.add_edge("rca", END)
    return g.compile(checkpointer=checkpointer, store=store)


heal_graph = _build_heal_graph()
rca_graph = _build_rca_graph()
