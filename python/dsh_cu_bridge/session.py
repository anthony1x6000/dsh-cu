"""Session state that changes what the other tools mean.

A locked session is the single most important piece of context for computer use:
screenshots show the lock screen, synthetic input lands on the lock screen, and
a launched app may never become visible. Reporting that plainly is better than
handing back a black frame or a click that went somewhere unexpected.

Part of dsh-cu.
"""

from __future__ import annotations

import os
from typing import Any

import dbus
import dbus.bus

# Plasma's locker answers to both spellings depending on the release.
_SCREENSAVER_SERVICES = ("org.kde.screensaver", "org.freedesktop.ScreenSaver")
_SCREENSAVER_PATH = "/ScreenSaver"
_SCREENSAVER_INTERFACE = "org.freedesktop.ScreenSaver"


def locked() -> bool | None:
    """Whether the session is locked, or None when nothing answers.

    None is not False: an unroutable question must not be reported as "unlocked",
    because every tool behaves differently under a lock screen.
    """
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    try:
        bus = dbus.bus.BusConnection(address) if address else dbus.SessionBus()
    except dbus.DBusException:
        return None
    for service in _SCREENSAVER_SERVICES:
        try:
            obj = bus.get_object(service, _SCREENSAVER_PATH)
            active = obj.GetActive(dbus_interface=_SCREENSAVER_INTERFACE)
        except dbus.DBusException:
            continue
        return bool(active)
    return None


def describe() -> dict[str, Any]:
    """Session identity and lock state, for the capability report."""
    return {
        "sessionType": os.environ.get("XDG_SESSION_TYPE"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "waylandDisplay": os.environ.get("WAYLAND_DISPLAY"),
        "locked": locked(),
    }
