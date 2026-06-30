"""Pluggable LangGraph checkpointer + long-term store.

`checkpointer` persists per-thread graph state; `store` is the cross-project
long-term memory (e.g. past incidents the RCA agent retrieves). Both honour an
env-selected backend (`memory | sqlite | redis`) and fall back to in-memory with
a logged warning when an optional package or server is unavailable, so the demo
always boots with zero infra.
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from ..config import settings
from ..logging_config import log


def make_checkpointer():
    backend = (settings.checkpoint_backend or "memory").lower()
    try:
        if backend == "sqlite":
            from langgraph.checkpoint.sqlite import SqliteSaver

            return SqliteSaver.from_conn_string(settings.cache_path + ".checkpoints")
        if backend == "redis":
            from langgraph.checkpoint.redis import RedisSaver

            saver = RedisSaver.from_conn_string(settings.redis_url)
            # Some versions require an explicit index setup.
            setup = getattr(saver, "setup", None)
            if callable(setup):
                setup()
            return saver
    except Exception as exc:  # noqa: BLE001
        log.warning("Checkpointer backend '{}' unavailable ({}); using memory", backend, exc)
    return MemorySaver()


def make_store():
    backend = (settings.store_backend or "memory").lower()
    try:
        if backend == "redis":
            from langgraph.store.redis import RedisStore

            store = RedisStore.from_conn_string(settings.redis_url)
            setup = getattr(store, "setup", None)
            if callable(setup):
                setup()
            return store
    except Exception as exc:  # noqa: BLE001
        log.warning("Store backend '{}' unavailable ({}); using memory", backend, exc)
    return InMemoryStore()


# Shared singletons across build_graph and heal_graph.
checkpointer = make_checkpointer()
store = make_store()
