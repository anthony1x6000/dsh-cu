"""Leases on held keys and buttons.

A model that presses ``D`` to walk right and then spends several seconds
deciding to release it leaves the key down for those seconds: the game keeps
walking, off the ledge. Nothing in the caller's control can fix that, so the
bridge does. Every held key or button is a lease with a deadline, and a
watchdog thread releases it when the deadline passes, whether or not the
caller ever sends the matching release.

The book is pure bookkeeping (injected clock and release callback) so its
rules are testable without a desktop; ``watch`` is the only part that owns a
thread.

Part of dsh-cu, after the rule in vercel-labs/agent-desktop that a held input
must not be accepted unless something can release it.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from .rpc import BridgeError

DEFAULT_LEASE_S = 1.0
MAX_LEASE_S = 30.0

Release = Callable[[str, int], None]
Held = tuple[str, int]

# How often the watchdog re-checks the guard while anything is held.
GUARD_POLL_S = 0.1


class Leases:
    """Which inputs are held, and when each must be given back."""

    def __init__(
        self,
        release: Release,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_s: float = DEFAULT_LEASE_S,
        max_s: float = MAX_LEASE_S,
        guard: Callable[[], object] | None = None,
    ) -> None:
        self._release = release
        self._clock = clock
        self._default_s = default_s
        self._max_s = max_s
        self._deadlines: dict[Held, float] = {}
        self._expired: set[Held] = set()
        # Wayland resets key state on keyboard leave, so a hold must not outlive
        # the window it was made into; the guard names that window.
        self._guard = guard
        self._anchor: object = None
        self._wake = threading.Condition()

    def grant(self, kind: str, code: int, lease_s: float | None = None) -> float:
        """Record a hold, replacing any earlier lease on it; return its deadline."""
        seconds = self._default_s if lease_s is None else lease_s
        if seconds <= 0:
            raise BridgeError("a lease must be longer than 0 ms", "BAD_ARGUMENT")
        deadline = self._clock() + min(seconds, self._max_s)
        anchor = self._read_guard() if self._deadlines == {} else None
        with self._wake:
            if not self._deadlines:
                self._anchor = anchor
            self._deadlines[(kind, code)] = deadline
            self._expired.discard((kind, code))
            self._wake.notify_all()
        return deadline

    def drop(self, kind: str, code: int) -> str:
        """Forget a hold the caller is releasing: ``held``, ``expired`` or ``unheld``."""
        with self._wake:
            if self._deadlines.pop((kind, code), None) is not None:
                return "held"
            if (kind, code) in self._expired:
                self._expired.discard((kind, code))
                return "expired"
        return "unheld"

    def held(self) -> list[Held]:
        with self._wake:
            return list(self._deadlines)

    def next_deadline(self) -> float | None:
        with self._wake:
            return min(self._deadlines.values(), default=None)

    def reap(self) -> list[Held]:
        """Release every hold whose deadline has passed."""
        now = self._clock()
        moved = self._focus_moved()
        with self._wake:
            due = [held for held, deadline in self._deadlines.items() if moved or deadline <= now]
            for held in due:
                del self._deadlines[held]
                self._expired.add(held)
        self._give_back(due)
        return due

    def release_all(self) -> list[Held]:
        """Release everything held now, however long its lease still has."""
        with self._wake:
            everything = list(self._deadlines)
            self._deadlines.clear()
        self._give_back(everything)
        return everything

    def _read_guard(self) -> object:
        if self._guard is None:
            return None
        try:
            return self._guard()
        except Exception:  # noqa: BLE001 - an unreadable guard must never release early
            return None

    def _focus_moved(self) -> bool:
        if self._guard is None or not self._deadlines or self._anchor is None:
            return False
        current = self._read_guard()
        return current is not None and current != self._anchor

    def _give_back(self, holds: list[Held]) -> None:
        for kind, code in holds:
            try:
                self._release(kind, code)
            except Exception as exc:  # noqa: BLE001 - one dead release must not strand the rest
                print(f"dsh-cu: could not release {kind} {code}: {exc}", file=sys.stderr)

    def watch(self) -> threading.Thread:
        """Start the daemon thread that reaps leases as they come due."""

        def run() -> None:
            while True:
                with self._wake:
                    deadline = min(self._deadlines.values(), default=None)
                    if deadline is None:
                        self._wake.wait()
                    else:
                        until = max(0.0, deadline - self._clock())
                        self._wake.wait(min(until, GUARD_POLL_S) if self._guard else until)
                self.reap()

        thread = threading.Thread(target=run, name="dsh-cu-lease-watchdog", daemon=True)
        thread.start()
        return thread
