"""Clipboard access through Plasma's Klipper.

Computer use needs the clipboard for two things: reading what a user copied, and
the unicode typing fallback, which puts text on the clipboard and pastes it
because injecting arbitrary Unicode through evdev keycodes is not possible on a
US-QWERTY keymap.

Klipper is Plasma's clipboard manager and owns the selection, so reading and
writing through it works without this process becoming a selection owner. When
Klipper is not running, both operations fail with a routable error instead of
silently doing nothing.

Part of dsh-cu.
"""

from __future__ import annotations

import os

import dbus
import dbus.bus

from .rpc import BridgeError

KLIPPER_BUS = "org.kde.klipper"
KLIPPER_PATH = "/klipper"
KLIPPER_INTERFACE = "org.kde.klipper.klipper"


def _iface() -> object:
    """The Klipper interface, or a routable failure when Klipper is absent."""
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    bus = dbus.bus.BusConnection(address) if address else dbus.SessionBus()
    try:
        obj = bus.get_object(KLIPPER_BUS, KLIPPER_PATH)
    except dbus.DBusException as exc:
        raise BridgeError(
            f"Klipper is not reachable on the session bus ({exc}); "
            "the clipboard operations need Plasma's clipboard manager",
            "CLIPBOARD_UNAVAILABLE",
        ) from exc
    return dbus.Interface(obj, KLIPPER_INTERFACE)


def read() -> str:
    """The current clipboard text."""
    try:
        return str(_iface().getClipboardContents())
    except dbus.DBusException as exc:
        raise BridgeError(f"cannot read the clipboard: {exc}", "CLIPBOARD_READ_FAILED") from exc


def write(text: str) -> None:
    """Replace the clipboard text."""
    try:
        _iface().setClipboardContents(dbus.String(text))
    except dbus.DBusException as exc:
        raise BridgeError(f"cannot write the clipboard: {exc}", "CLIPBOARD_WRITE_FAILED") from exc


def available() -> bool:
    """Whether Klipper answers, without raising."""
    try:
        read()
    except BridgeError:
        return False
    return True
