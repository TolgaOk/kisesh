"""Whole-operation cancellation for isolated Unix CLI workers, never Kitty itself."""

from __future__ import annotations

import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType


class OperationDeadlineExceeded(BaseException):
    """Bypass recoverable per-pane Exception handlers so a deadline cannot be swallowed."""


def _expired(signum: int, frame: FrameType | None) -> None:
    """Interrupt lock waits and subprocess calls in the isolated worker's main thread."""
    del signum, frame
    raise OperationDeadlineExceeded("background operation exceeded its deadline")


@contextmanager
def operation_deadline(seconds: float) -> Iterator[None]:
    """Bound an isolated CLI operation while restoring any enclosing alarm on exit."""
    if seconds <= 0:
        raise ValueError("operation deadline must be positive")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    started = time.monotonic()
    duration = min(seconds, previous_timer[0]) if previous_timer[0] else seconds
    signal.signal(signal.SIGALRM, _expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, duration)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            remaining = max(0.000001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])
