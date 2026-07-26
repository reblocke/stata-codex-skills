from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_DETACHMENT_DEPTH: ContextVar[int] = ContextVar(
    "stata_codex_detachment_depth",
    default=0,
)


def detachment_is_authorized() -> bool:
    return _DETACHMENT_DEPTH.get() > 0


@contextmanager
def allow_detached_process() -> Iterator[None]:
    """Permit one reviewed, lease-tracked detached-process scope."""

    token = _DETACHMENT_DEPTH.set(_DETACHMENT_DEPTH.get() + 1)
    try:
        yield
    finally:
        _DETACHMENT_DEPTH.reset(token)
