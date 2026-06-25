"""In-memory async pub/sub event bus to stream pipeline events to WebSocket clients."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class Event:
    project_id: str
    agent: str
    type: str  # agent_started | agent_log | agent_completed | agent_failed | incident | healed | pipeline
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []
        self._history: Dict[str, List[Event]] = {}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event: Event) -> None:
        self._history.setdefault(event.project_id, []).append(event)
        for q in list(self._subscribers):
            await q.put(event)

    def history(self, project_id: str) -> List[Event]:
        return self._history.get(project_id, [])


bus = EventBus()
