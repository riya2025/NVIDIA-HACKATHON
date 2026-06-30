"""Pluggable LLM response caching.

Two surfaces share one backend choice (`settings.cache_backend`):

  - A tiny key->str KV used by `nvidia_client.complete` (OpenAI SDK path), so
    repeated Architect/Developer generations return instantly.
  - LangChain's global LLM cache, so the RCA ReAct agent's `ChatNVIDIA` calls
    are cached too (`install_langchain_llm_cache`).

Backends: `memory` (process-local dict), `sqlite` (zero-infra, persists across
restarts) and `redis` (drop-in when a server is available). Anything that fails
to initialise degrades gracefully to in-memory with a logged warning.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Protocol

from .config import settings
from .logging_config import log


def make_request_key(
    *, model: str, system: str, prompt: str, max_tokens: int, temperature: float, thinking: bool
) -> str:
    raw = f"{model}\x1f{system}\x1f{prompt}\x1f{max_tokens}\x1f{temperature}\x1f{thinking}"
    return "llm:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _KV(Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str) -> None: ...


class _MemoryKV:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._d.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._d[key] = value


class _SqliteKV:
    """Thread-safe SQLite KV (one connection per thread via check_same_thread)."""

    def __init__(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(p)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False, timeout=5.0)

    def get(self, key: str) -> Optional[str]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT v FROM cache WHERE k = ?", (key,)).fetchone()
            return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO cache (k, v) VALUES (?, ?)", (key, value))


class _RedisKV:
    def __init__(self, url: str) -> None:
        import redis  # type: ignore

        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._r.ping()

    def get(self, key: str) -> Optional[str]:
        return self._r.get(key)

    def set(self, key: str, value: str) -> None:
        self._r.set(key, value)


def _build_kv() -> _KV:
    backend = (settings.cache_backend or "memory").lower()
    try:
        if backend == "sqlite":
            return _SqliteKV(settings.cache_path)
        if backend == "redis":
            return _RedisKV(settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM cache backend '{}' unavailable ({}); using memory", backend, exc)
    return _MemoryKV()


_kv: _KV = _build_kv()


def cache_get(key: str) -> Optional[str]:
    if not settings.cache_enabled:
        return None
    try:
        return _kv.get(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_get failed: {}", exc)
        return None


def cache_set(key: str, value: str) -> None:
    if not settings.cache_enabled or not value:
        return
    try:
        _kv.set(key, value)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_set failed: {}", exc)


def install_langchain_llm_cache() -> None:
    """Point LangChain's global LLM cache at the configured backend.

    Caches `ChatNVIDIA` calls (RCA ReAct agent). Best-effort: never fatal.
    """
    if not settings.cache_enabled:
        return
    backend = (settings.cache_backend or "memory").lower()
    try:
        from langchain_core.caches import InMemoryCache
        from langchain_core.globals import set_llm_cache

        cache = None
        if backend == "sqlite":
            try:
                from langchain_community.cache import SQLiteCache

                Path(settings.cache_path).parent.mkdir(parents=True, exist_ok=True)
                cache = SQLiteCache(database_path=settings.cache_path)
            except Exception:  # noqa: BLE001
                # langchain_community not installed -> use the in-memory cache
                # (only needs langchain_core, always present). No noisy warning.
                cache, backend = InMemoryCache(), "memory (sqlite backend unavailable)"
        elif backend == "redis":
            try:
                import redis  # type: ignore
                from langchain_community.cache import RedisCache

                cache = RedisCache(redis.Redis.from_url(settings.redis_url))
            except Exception:  # noqa: BLE001
                cache, backend = InMemoryCache(), "memory (redis backend unavailable)"
        else:
            cache = InMemoryCache()
        set_llm_cache(cache)
        log.info("LangChain LLM cache installed (backend={})", backend)
    except Exception as exc:  # noqa: BLE001
        # Truly couldn't set any cache (e.g. langchain_core missing) — not fatal.
        log.debug("LangChain LLM cache not installed ({}); continuing without it", exc)
