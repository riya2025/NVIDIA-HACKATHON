"""Build pipeline as a LangGraph StateGraph.

Topology:

    architect -> codegen (supervisor subgraph) -> tester -> deployment -> monitoring

The codegen supervisor is a compiled subgraph that fans out to the frontend,
backend and devops subagents in parallel (START -> all three -> join), embedded
as a single `codegen` node in the parent graph.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import (
    architect_node,
    backend_node,
    codegen_join,
    deployment_node,
    devops_node,
    frontend_node,
    monitoring_node,
    tester_node,
)
from .persistence import checkpointer, store
from .state import FoundryState


def _build_codegen_subgraph():
    """Supervisor subgraph: 3 codegen subagents run concurrently, then join."""
    sub = StateGraph(FoundryState)
    sub.add_node("frontend", frontend_node)
    sub.add_node("backend", backend_node)
    sub.add_node("devops", devops_node)
    sub.add_node("codegen_join", codegen_join)

    # Fan out: parallel branches from START.
    sub.add_edge(START, "frontend")
    sub.add_edge(START, "backend")
    sub.add_edge(START, "devops")
    # Fan in: join waits for all three before continuing.
    sub.add_edge("frontend", "codegen_join")
    sub.add_edge("backend", "codegen_join")
    sub.add_edge("devops", "codegen_join")
    sub.add_edge("codegen_join", END)
    return sub.compile()


def _build_graph():
    codegen = _build_codegen_subgraph()

    g = StateGraph(FoundryState)
    g.add_node("architect", architect_node)
    # A compiled subgraph is itself a runnable node.
    g.add_node("codegen", codegen)
    g.add_node("tester", tester_node)
    g.add_node("deployment", deployment_node)
    g.add_node("monitoring", monitoring_node)

    g.add_edge(START, "architect")
    g.add_edge("architect", "codegen")
    g.add_edge("codegen", "tester")
    g.add_edge("tester", "deployment")
    g.add_edge("deployment", "monitoring")
    g.add_edge("monitoring", END)

    return g.compile(checkpointer=checkpointer, store=store)


build_graph = _build_graph()
