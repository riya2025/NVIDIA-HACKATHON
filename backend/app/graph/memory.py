"""Long-term incident memory over the LangGraph store.

Every resolved incident is recorded so the RCA agent can retrieve similar past
incidents (the demo's GraphRAG-style "have we seen this before?" step). Matching
is a simple keyword-overlap score so it works with the zero-infra InMemoryStore
(no embedding index required); a vector index can replace `_score` later.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from ..logging_config import log

_NS = ("incidents",)
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _score(query_tokens: set[str], item: Dict[str, Any]) -> int:
    hay = _tokens(f"{item.get('title', '')} {item.get('root_cause', '')}")
    return len(query_tokens & hay)


def record_incident(store, *, project_id: str, incident_id: str, title: str,
                    root_cause: str = "", action: str = "") -> None:
    if store is None:
        return
    try:
        store.put(
            _NS,
            incident_id,
            {
                "project_id": project_id,
                "title": title,
                "root_cause": root_cause,
                "action": action,
                "ts": time.time(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("record_incident failed: {}", exc)


def search_incidents(store, query: str, *, limit: int = 3, exclude_id: str = "") -> List[Dict[str, Any]]:
    """Return up to `limit` past incidents most similar to `query`."""
    if store is None:
        return []
    try:
        results = store.search(_NS, limit=100)
    except Exception as exc:  # noqa: BLE001
        log.warning("search_incidents failed: {}", exc)
        return []

    qt = _tokens(query)
    scored = []
    for r in results:
        value = getattr(r, "value", None) or {}
        key = getattr(r, "key", "")
        if key == exclude_id:
            continue
        s = _score(qt, value)
        if s > 0:
            scored.append((s, value))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored[:limit]]
