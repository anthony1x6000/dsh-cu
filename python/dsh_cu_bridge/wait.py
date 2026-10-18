"""Bounded polling, so a tool call waits for the desktop instead of the agent.

An agent that polls from its own loop pays a full model turn per attempt: it
sleeps, looks, and decides, three round trips for what is one fact. Waiting
inside the bridge costs nothing but the wall clock it was going to spend anyway,
and it returns the moment the condition holds instead of at the timeout.
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

#: How long to wait between probes. Short enough to feel immediate for a window
#: that appears in 300 ms, long enough that a 30 s wait is 150 KWin round trips,
#: not thousands.
POLL_INTERVAL_S = 0.2


def poll_until(
    probe: Callable[[], T],
    ready: Callable[[T], bool],
    timeout_s: float,
    *,
    interval_s: float = POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[T, float]:
    """Probe until ``ready`` accepts the value, and report how long that took.

    ``timeout_s <= 0`` probes exactly once, which is the "do not wait" case and
    keeps callers free of a branch. The last probe always wins: a caller never
    gets a value older than the timeout it asked for, and it never gets a
    second probe it did not ask for.

    @param probe: reads the current state; called at least once.
    @param ready: decides whether that state is the one being waited for.
    @param timeout_s: total wait budget in seconds.
    @param interval_s: pause between probes.
    @param clock: monotonic seconds; injected for tests.
    @param sleep: pause function; injected for tests.
    @returns: the last probed value and the elapsed seconds.
    """
    started = clock()
    deadline = started + max(0.0, timeout_s)
    value = probe()
    while not ready(value) and clock() < deadline:
        sleep(min(interval_s, max(0.0, deadline - clock())))
        value = probe()
    return value, clock() - started


def millis(seconds: float) -> int:
    """Round an elapsed-seconds value to whole milliseconds for a response."""
    return int(round(max(0.0, seconds) * 1000))


def as_dict(value: Any) -> dict[str, Any]:  # pragma: no cover - typing helper
    """Narrow a JSON object for callers that already know its shape."""
    return value if isinstance(value, dict) else {}
