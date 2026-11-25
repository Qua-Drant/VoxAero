"""Bounded in-memory TTL session storage and shared dispatch step helpers."""

from __future__ import annotations

from collections import OrderedDict
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from typing import Callable, Generic, TypeVar

from backend.schemas import DispatchNotice, DispatchNoticeType, LLMMode, LocationClarification, ThoughtStep

T = TypeVar("T")
DEFAULT_CAPACITY = 128


class SessionStore(Generic[T]):
    """Thread-safe LRU-bounded TTL store for ephemeral dispatch and mission state."""

    def __init__(self, ttl_seconds: float, *, capacity: int = DEFAULT_CAPACITY, prefix: str = "") -> None:
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self.prefix = prefix
        self._entries: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = Lock()

    def _raw_key(self, token: str | None) -> str | None:
        if not token:
            return None
        if self.prefix:
            return token[len(self.prefix):] if token.startswith(self.prefix) else None
        return token

    def issue(self, value: T) -> str:
        key = token_urlsafe(24)
        with self._lock:
            self._entries[key] = (monotonic() + self.ttl_seconds, value)
            if len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
        return f"{self.prefix}{key}"

    def get(self, token: str | None, *, consume: bool = False) -> T | None:
        key = self._raw_key(token)
        if not key:
            return None
        with self._lock:
            entry = self._entries.pop(key, None) if consume else self._entries.get(key)
            if entry is not None and entry[0] > monotonic():
                return entry[1]
            self._entries.pop(key, None)
        return None

    def update(self, token: str | None, transform: Callable[[T], T]) -> T | None:
        key = self._raw_key(token)
        if not key:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] <= monotonic():
                self._entries.pop(key, None)
                return None
            expires_at, current = entry
            updated = transform(current)
            self._entries[key] = (expires_at, updated)
            return updated


def add_step(steps: list[ThoughtStep], role: str, content: str) -> None:
    steps.append(ThoughtStep(step=len(steps) + 1, role=role, content=content))


def build_notice(
    steps: list[ThoughtStep],
    status: DispatchNoticeType,
    detail: str,
    *,
    mode: LLMMode | None = None,
    model_called: bool = False,
    token: str | None = None,
    clarification: LocationClarification | None = None,
) -> DispatchNotice:
    add_step(steps, "observation", detail)
    return DispatchNotice(
        status=status,
        detail=detail,
        thought_steps=steps,
        mode=mode,
        model_called=model_called,
        clarification_token=token,
        clarification=clarification,
    )
