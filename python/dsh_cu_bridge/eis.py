"""Input injection through KWin's EIS endpoint and libei.

KWin exposes a private ``org.kde.KWin.EIS.RemoteDesktop`` object that hands out
an EIS (Emulated Input Server) socket plus a cookie. libei speaks that protocol,
so a process can inject pointer and keyboard events into the compositor with no
XDG portal prompt and no privilege beyond the session bus.

Because injection lands in the *compositor*, it reaches every client the same
way — native Wayland apps, XWayland apps, Wine apps under Vinegar, and flatpak
sandboxes alike. That is why this is the input backend rather than something
per-app: a sandbox cannot block a synthetic compositor event aimed at its
surface.

Layout notes that matter:

* ``ei_dispatch`` returns void. Declaring it ``c_int`` reads a garbage register
  and can abort the handshake, so its restype stays None.
* A device may only emulate after the server resumed it. Starting early is
  rejected and the events are then dropped silently, so emulation is started
  from the RESUMED event with an unconditional fallback after the handshake.
* ``ei_unref`` must run *before* the D-Bus ``disconnect`` call, or libei's final
  message lands on a closed socket.

Part of dsh-cu, derived from kwin-mcp's ``input.py`` (MIT,
Copyright (c) 2026 Byeonghoon Ho Yoo) — see NOTICE.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import time
from typing import Any

import dbus
import dbus.bus
from dbus.mainloop.glib import DBusGMainLoop

from .rpc import BridgeError

# --- libei surface -----------------------------------------------------------

EI_CAP_POINTER = 1 << 0
EI_CAP_POINTER_ABSOLUTE = 1 << 1
EI_CAP_KEYBOARD = 1 << 2
EI_CAP_TOUCH = 1 << 3
EI_CAP_SCROLL = 1 << 4
EI_CAP_BUTTON = 1 << 5

EI_EVENT_CONNECT = 1
EI_EVENT_DISCONNECT = 2
EI_EVENT_SEAT_ADDED = 3
EI_EVENT_DEVICE_ADDED = 5
EI_EVENT_DEVICE_REMOVED = 6
EI_EVENT_DEVICE_PAUSED = 7
EI_EVENT_DEVICE_RESUMED = 8

PRESSED = 1
RELEASED = 0

# Linux evdev button codes.
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BUTTON_CODES = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

# libei expresses a wheel detent in fractions of 120.
SCROLL_DISCRETE_UNIT = 120
SCROLL_STEP_PIXELS = 15.0

HANDSHAKE_TIMEOUT_S = 5.0
DEVICE_REPLACEMENT_TIMEOUT_S = 1.5
MAX_DISPATCH_ROUNDS = 64
CLICK_PRESS_S = 0.025
MULTI_CLICK_GAP_S = 0.06
STEP_SLEEP_S = 0.01
MOVE_SETTLE_S = 0.02

_libei: Any = None


def _load_libei() -> Any:
    """Load libei.so.1 and declare every prototype this module calls."""
    library = ctypes.CDLL("libei.so.1")

    library.ei_new_sender.restype = ctypes.c_void_p
    library.ei_new_sender.argtypes = [ctypes.c_void_p]
    library.ei_configure_name.restype = None
    library.ei_configure_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    library.ei_setup_backend_fd.restype = ctypes.c_int
    library.ei_setup_backend_fd.argtypes = [ctypes.c_void_p, ctypes.c_int]
    # void, deliberately: a c_int restype reads a garbage register.
    library.ei_dispatch.restype = None
    library.ei_dispatch.argtypes = [ctypes.c_void_p]
    library.ei_get_event.restype = ctypes.c_void_p
    library.ei_get_event.argtypes = [ctypes.c_void_p]
    library.ei_event_get_type.restype = ctypes.c_int
    library.ei_event_get_type.argtypes = [ctypes.c_void_p]
    library.ei_event_unref.restype = ctypes.c_void_p
    library.ei_event_unref.argtypes = [ctypes.c_void_p]
    library.ei_unref.restype = ctypes.c_void_p
    library.ei_unref.argtypes = [ctypes.c_void_p]
    library.ei_get_fd.restype = ctypes.c_int
    library.ei_get_fd.argtypes = [ctypes.c_void_p]

    library.ei_event_get_seat.restype = ctypes.c_void_p
    library.ei_event_get_seat.argtypes = [ctypes.c_void_p]
    library.ei_seat_has_capability.restype = ctypes.c_int
    library.ei_seat_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    library.ei_seat_bind_capabilities.restype = None
    library.ei_seat_bind_capabilities.argtypes = [ctypes.c_void_p]  # variadic + sentinel

    library.ei_event_get_device.restype = ctypes.c_void_p
    library.ei_event_get_device.argtypes = [ctypes.c_void_p]
    library.ei_device_has_capability.restype = ctypes.c_int
    library.ei_device_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    library.ei_device_ref.restype = ctypes.c_void_p
    library.ei_device_ref.argtypes = [ctypes.c_void_p]
    library.ei_device_unref.restype = ctypes.c_void_p
    library.ei_device_unref.argtypes = [ctypes.c_void_p]

    library.ei_device_pointer_motion_absolute.restype = None
    library.ei_device_pointer_motion_absolute.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    library.ei_device_button_button.restype = None
    library.ei_device_button_button.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    library.ei_device_scroll_discrete.restype = None
    library.ei_device_scroll_discrete.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]
    library.ei_device_scroll_delta.restype = None
    library.ei_device_scroll_delta.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    library.ei_device_scroll_stop.restype = None
    library.ei_device_scroll_stop.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    library.ei_device_keyboard_key.restype = None
    library.ei_device_keyboard_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    library.ei_device_frame.restype = None
    library.ei_device_frame.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    library.ei_device_start_emulating.restype = None
    library.ei_device_start_emulating.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    library.ei_device_stop_emulating.restype = None
    library.ei_device_stop_emulating.argtypes = [ctypes.c_void_p]
    return library


def get_libei() -> Any:
    """The process-wide libei handle, loaded on first use.

    Loading lazily keeps the bridge importable (and every non-input operation
    usable) on a host without libei installed.
    """
    global _libei
    if _libei is None:
        try:
            _libei = _load_libei()
        except OSError as exc:
            raise BridgeError(
                f"libei could not be loaded ({exc}); install libei "
                "(for example: sudo apt install libei1)",
                "LIBEI_UNAVAILABLE",
            ) from exc
    return _libei


def libei_available() -> bool:
    """Whether libei can be loaded, without raising."""
    try:
        get_libei()
    except BridgeError:
        return False
    return True


class EISClient:
    """One EIS connection to KWin, with resume-driven device management."""

    _DEVICE_SLOTS: tuple[tuple[str, str], ...] = (
        ("pointer", "_pointer"),
        ("keyboard", "_keyboard"),
    )

    def __init__(self, dbus_address: str | None = None) -> None:
        DBusGMainLoop(set_as_default=True)
        address = dbus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        self._bus = dbus.bus.BusConnection(address) if address else dbus.SessionBus()
        self._ei: int = 0
        self._cookie: int = 0
        self._pointer: int = 0
        self._keyboard: int = 0
        self._eis_iface: Any = None
        self._emulating: set[int] = set()
        self._lost_at: dict[str, float] = {}
        self._disconnected = False
        self._sequence = 0
        self._setup()

    # -- connection ----------------------------------------------------------

    def _setup(self) -> None:
        library = get_libei()
        try:
            eis_object = self._bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop")
            self._eis_iface = dbus.Interface(eis_object, "org.kde.KWin.EIS.RemoteDesktop")
            caps = (
                EI_CAP_POINTER
                | EI_CAP_POINTER_ABSOLUTE
                | EI_CAP_KEYBOARD
                | EI_CAP_BUTTON
                | EI_CAP_SCROLL
            )
            result = self._eis_iface.connectToEIS(dbus.Int32(caps))
        except dbus.DBusException as exc:
            raise BridgeError(
                f"KWin EIS interface unavailable: {exc}",
                "EIS_UNAVAILABLE",
                {"hint": "input injection needs a KWin Wayland session"},
            ) from exc

        fd: int | None = result[0].take()
        self._cookie = int(result[1])
        try:
            self._ei = library.ei_new_sender(None)
            if not self._ei:
                raise BridgeError("libei refused to create an EI context", "EIS_SETUP_FAILED")
            library.ei_configure_name(self._ei, b"dsh-cu")
            backend_fd, fd = fd, None  # libei owns the fd from here, even on failure
            ret = library.ei_setup_backend_fd(self._ei, backend_fd)
            if ret != 0:
                raise BridgeError(f"ei_setup_backend_fd failed: {ret}", "EIS_SETUP_FAILED")
            self._negotiate_devices()
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.close()
            raise

    def _negotiate_devices(self, timeout: float = HANDSHAKE_TIMEOUT_S) -> None:
        """Pump events until pointer and keyboard are usable, or fail."""
        library = get_libei()
        ei_fd = library.ei_get_fd(self._ei)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            readable, _, _ = select.select([ei_fd], [], [], 0.3)
            if readable:
                library.ei_dispatch(self._ei)
            self._process_queued_events()
            if self._disconnected:
                raise BridgeError("the EIS server disconnected during the handshake", "EIS_DISCONNECTED")
            if self._pointer in self._emulating and self._keyboard in self._emulating:
                break
        if not self._pointer:
            raise BridgeError("KWin offered no absolute pointer device over EIS", "EIS_NO_POINTER")
        if not self._keyboard:
            raise BridgeError("KWin offered no keyboard device over EIS", "EIS_NO_KEYBOARD")
        # A device that never announced a resume is started anyway: emulating a
        # paused device is no worse than losing a slow-to-resume one.
        for device in {self._pointer, self._keyboard}:
            if device and device not in self._emulating:
                library.ei_device_start_emulating(device, self._next_sequence())
                self._emulating.add(device)

    def _next_sequence(self) -> int:
        """A monotonically increasing sequence number for ``start_emulating``."""
        self._sequence += 1
        return self._sequence

    # -- event pump ----------------------------------------------------------

    def _process_queued_events(self) -> None:
        library = get_libei()
        while True:
            event = library.ei_get_event(self._ei)
            if not event:
                return
            self._handle_event(event)

    def _handle_event(self, event: int) -> None:
        library = get_libei()
        try:
            event_type = library.ei_event_get_type(event)
            if event_type == EI_EVENT_DISCONNECT:
                self._disconnected = True
            elif event_type == EI_EVENT_SEAT_ADDED:
                self._bind_seat_capabilities(event)
            elif event_type == EI_EVENT_DEVICE_ADDED:
                self._register_device(event)
            elif event_type in (EI_EVENT_DEVICE_REMOVED, EI_EVENT_DEVICE_PAUSED, EI_EVENT_DEVICE_RESUMED):
                device = int(library.ei_event_get_device(event) or 0)
                if device:
                    self._update_device_state(event_type, device)
        finally:
            library.ei_event_unref(event)

    def _update_device_state(self, event_type: int, device: int) -> None:
        library = get_libei()
        roles = [role for role, attr in self._DEVICE_SLOTS if getattr(self, attr) == device]
        if not roles:
            return
        if event_type == EI_EVENT_DEVICE_RESUMED:
            if device not in self._emulating:
                library.ei_device_start_emulating(device, self._next_sequence())
                self._emulating.add(device)
            for role in roles:
                self._lost_at.pop(role, None)
            return
        now = time.monotonic()
        for role in roles:
            self._lost_at.setdefault(role, now)
        self._emulating.discard(device)
        if event_type == EI_EVENT_DEVICE_PAUSED:
            return
        for role, attr in self._DEVICE_SLOTS:
            if role in roles:
                setattr(self, attr, 0)
                library.ei_device_unref(device)

    def _drain_events(self, timeout: float) -> None:
        library = get_libei()
        self._process_queued_events()
        ei_fd = library.ei_get_fd(self._ei)
        wait = timeout
        for _ in range(MAX_DISPATCH_ROUNDS):
            readable, _, _ = select.select([ei_fd], [], [], wait)
            if not readable:
                return
            library.ei_dispatch(self._ei)
            self._process_queued_events()
            if self._disconnected:
                return
            wait = 0.0

    def _live_device(self, role: str) -> int:
        library = get_libei()
        self._drain_events(0.0)
        deadline = time.monotonic() + DEVICE_REPLACEMENT_TIMEOUT_S
        while True:
            if self._disconnected:
                raise BridgeError("KWin closed the EIS connection; input is unavailable", "EIS_DISCONNECTED")
            attribute = dict(self._DEVICE_SLOTS)[role]
            device = getattr(self, attribute)
            lost_at = self._lost_at.get(role)
            if lost_at is None:
                if device:
                    return device
                raise BridgeError(f"no {role} device available from EIS", "EIS_NO_DEVICE")
            remaining = min(lost_at + DEVICE_REPLACEMENT_TIMEOUT_S, deadline) - time.monotonic()
            if remaining <= 0:
                raise BridgeError(
                    f"KWin removed the EIS {role} device and no replacement arrived within "
                    f"{DEVICE_REPLACEMENT_TIMEOUT_S:g}s",
                    "EIS_DEVICE_LOST",
                )
            self._drain_events(remaining)

    def _bind_seat_capabilities(self, event: int) -> None:
        library = get_libei()
        seat = library.ei_event_get_seat(event)
        wanted = [
            EI_CAP_POINTER,
            EI_CAP_POINTER_ABSOLUTE,
            EI_CAP_KEYBOARD,
            EI_CAP_BUTTON,
            EI_CAP_SCROLL,
        ]
        bind = [cap for cap in wanted if library.ei_seat_has_capability(seat, cap)]
        if not bind:
            return
        args = [ctypes.c_uint(cap) for cap in bind]
        args.append(ctypes.c_void_p(None))  # NULL sentinel; the C call is variadic
        library.ei_seat_bind_capabilities(seat, *args)

    def _register_device(self, event: int) -> None:
        library = get_libei()
        device = library.ei_event_get_device(event)
        if library.ei_device_has_capability(device, EI_CAP_POINTER_ABSOLUTE) and not self._pointer:
            self._pointer = library.ei_device_ref(device)
        if library.ei_device_has_capability(device, EI_CAP_KEYBOARD) and not self._keyboard:
            self._keyboard = library.ei_device_ref(device)

    # -- senders -------------------------------------------------------------

    @staticmethod
    def _now_us() -> int:
        """CLOCK_MONOTONIC microseconds, the timeline libei frame times use."""
        return int(time.monotonic() * 1_000_000)

    def _flush(self) -> None:
        get_libei().ei_dispatch(self._ei)

    def pointer_move_absolute(self, x: float, y: float) -> None:
        """Move the pointer to one global logical point."""
        library = get_libei()
        device = self._live_device("pointer")
        library.ei_device_pointer_motion_absolute(device, float(x), float(y))
        library.ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_button(self, button: int, state: int) -> None:
        """Press or release one mouse button by evdev code."""
        library = get_libei()
        device = self._live_device("pointer")
        library.ei_device_button_button(device, int(button), int(state))
        library.ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_scroll_discrete(self, dx: int, dy: int) -> None:
        """Scroll by whole wheel detents (120ths of a detent are rejected)."""
        library = get_libei()
        device = self._live_device("pointer")
        library.ei_device_scroll_discrete(device, int(dx), int(dy))
        library.ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_scroll_delta(self, dx: float, dy: float) -> None:
        """Scroll by a smooth pixel delta."""
        library = get_libei()
        device = self._live_device("pointer")
        library.ei_device_scroll_delta(device, float(dx), float(dy))
        library.ei_device_frame(device, self._now_us())
        self._flush()

    def pointer_scroll_stop(self) -> None:
        """End a scroll gesture on both axes."""
        library = get_libei()
        device = self._live_device("pointer")
        library.ei_device_scroll_stop(device, 1, 1)
        library.ei_device_frame(device, self._now_us())
        self._flush()

    def keyboard_key(self, keycode: int, state: int) -> None:
        """Press or release one evdev keycode."""
        library = get_libei()
        device = self._live_device("keyboard")
        library.ei_device_keyboard_key(device, int(keycode), int(state))
        library.ei_device_frame(device, self._now_us())
        self._flush()

    def close(self) -> None:
        """Tear the connection down in the order libei requires."""
        library = get_libei()
        if self._ei:
            for device in {self._pointer, self._keyboard}:
                if device and device in self._emulating and not self._disconnected:
                    library.ei_device_stop_emulating(device)
            for _, attr in self._DEVICE_SLOTS:
                device = getattr(self, attr)
                if device:
                    library.ei_device_unref(device)
                    setattr(self, attr, 0)
            # libei first: disconnecting the D-Bus side first makes its final
            # message hit a closed socket.
            library.ei_unref(self._ei)
            self._ei = 0
        self._emulating.clear()
        if self._eis_iface is not None and self._cookie:
            try:
                self._eis_iface.disconnect(dbus.Int32(self._cookie))
            except dbus.DBusException:
                pass
            self._eis_iface = None
        self._cookie = 0


class Input:
    """High-level pointer and keyboard gestures over one {@link EISClient}."""

    def __init__(self, client: EISClient) -> None:
        self._client = client
        self._held: set[str] = set()
        self._warmed = False

    def move(self, x: int, y: int) -> None:
        """Move the pointer, defeating KWin's same-position short circuit.

        KWin returns early from pointer motion when the global cursor position
        did not change, which leaves a client whose window moved under a parked
        cursor with a stale surface-local position. Visiting an adjacent pixel
        first makes the final position a real change. While a button is held the
        detour is skipped so it never becomes part of a drag path.

        The first motion after the handshake is repeated: it can be coalesced
        with the device-resume frame and settle on the approach pixel, and an
        absolute motion is idempotent, so repeating it costs one event.
        """
        if not self._held:
            approach_x = x - 1 if x > 0 else x + 1
            self._client.pointer_move_absolute(approach_x, y)
            time.sleep(STEP_SLEEP_S)
        self._client.pointer_move_absolute(x, y)
        if not self._warmed:
            self._warmed = True
            time.sleep(STEP_SLEEP_S)
            self._client.pointer_move_absolute(x, y)

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1, hold_ms: int = 0) -> None:
        """Move, then click one button ``clicks`` times."""
        code = BUTTON_CODES.get(button)
        if code is None:
            raise BridgeError(f"unknown mouse button {button!r}", "BAD_ARGUMENT")
        if clicks < 1:
            raise BridgeError("clicks must be at least 1", "BAD_ARGUMENT")
        self.move(x, y)
        time.sleep(MOVE_SETTLE_S)
        for index in range(clicks):
            if index > 0:
                time.sleep(MULTI_CLICK_GAP_S)
            self._client.pointer_button(code, PRESSED)
            if hold_ms > 0 and index == clicks - 1:
                deadline = time.monotonic() + hold_ms / 1000.0
                while time.monotonic() < deadline:
                    time.sleep(min(0.5, deadline - time.monotonic()))
            else:
                time.sleep(CLICK_PRESS_S)
            self._client.pointer_button(code, RELEASED)

    def button_down(self, x: int, y: int, *, button: str = "left") -> None:
        """Press and hold one button, entering drag mode."""
        code = BUTTON_CODES.get(button)
        if code is None:
            raise BridgeError(f"unknown mouse button {button!r}", "BAD_ARGUMENT")
        self.move(x, y)
        time.sleep(MOVE_SETTLE_S)
        self._client.pointer_button(code, PRESSED)
        self._held.add(button)

    def button_up(self, x: int, y: int, *, button: str = "left") -> None:
        """Release one held button; releasing never leaves drag mode stuck."""
        code = BUTTON_CODES.get(button)
        if code is None:
            raise BridgeError(f"unknown mouse button {button!r}", "BAD_ARGUMENT")
        self.move(x, y)
        time.sleep(MOVE_SETTLE_S)
        try:
            self._client.pointer_button(code, RELEASED)
        finally:
            self._held.discard(button)

    def drag(self, x: int, y: int, *, to_x: int, to_y: int, button: str = "left", steps: int = 20) -> None:
        """Press at one point, move along a straight path, release at the end."""
        if steps < 2:
            raise BridgeError("drag needs at least 2 steps", "BAD_ARGUMENT")
        self.button_down(x, y, button=button)
        try:
            for index in range(1, steps + 1):
                ratio = index / steps
                self._client.pointer_move_absolute(
                    x + (to_x - x) * ratio,
                    y + (to_y - y) * ratio,
                )
                time.sleep(STEP_SLEEP_S)
        finally:
            self.button_up(to_x, to_y, button=button)

    def scroll(self, x: int, y: int, *, amount: int, horizontal: bool = False, discrete: bool = True) -> None:
        """Scroll ``amount`` clicks (discrete detents) or pixels at one point."""
        if amount == 0:
            raise BridgeError("scroll amount must not be 0", "BAD_ARGUMENT")
        self.move(x, y)
        time.sleep(MOVE_SETTLE_S)
        sign = 1 if amount > 0 else -1
        magnitude = abs(amount)
        steps = min(10, magnitude)
        try:
            for index in range(steps):
                share = magnitude // steps + (1 if index < magnitude % steps else 0)
                if share == 0:
                    continue
                if discrete:
                    ticks = sign * share * SCROLL_DISCRETE_UNIT
                    self._client.pointer_scroll_discrete(ticks if horizontal else 0, 0 if horizontal else ticks)
                else:
                    delta = sign * share * SCROLL_STEP_PIXELS
                    self._client.pointer_scroll_delta(delta if horizontal else 0.0, 0.0 if horizontal else delta)
                time.sleep(STEP_SLEEP_S)
        finally:
            self._client.pointer_scroll_stop()

    def press_chord(self, modifiers: list[int], keycode: int) -> None:
        """Press modifiers, tap the main key, then release modifiers in reverse."""
        for code in modifiers:
            self._client.keyboard_key(code, PRESSED)
            time.sleep(STEP_SLEEP_S)
        try:
            self._client.keyboard_key(keycode, PRESSED)
            time.sleep(STEP_SLEEP_S)
            self._client.keyboard_key(keycode, RELEASED)
        finally:
            for code in reversed(modifiers):
                time.sleep(STEP_SLEEP_S)
                self._client.keyboard_key(code, RELEASED)

    def key_down(self, modifiers: list[int], keycode: int) -> None:
        """Hold modifiers and one main key."""
        for code in modifiers:
            self._client.keyboard_key(code, PRESSED)
            time.sleep(STEP_SLEEP_S)
        self._client.keyboard_key(keycode, PRESSED)

    def key_up(self, modifiers: list[int], keycode: int) -> None:
        """Release the main key first, then modifiers in reverse."""
        self._client.keyboard_key(keycode, RELEASED)
        for code in reversed(modifiers):
            time.sleep(STEP_SLEEP_S)
            self._client.keyboard_key(code, RELEASED)

    def type_plan(self, plan: list[tuple[int, bool]]) -> None:
        """Type a pre-computed US-QWERTY keystroke plan."""
        for keycode, needs_shift in plan:
            if needs_shift:
                self._client.keyboard_key(42, PRESSED)
                time.sleep(STEP_SLEEP_S)
            self._client.keyboard_key(keycode, PRESSED)
            time.sleep(STEP_SLEEP_S)
            self._client.keyboard_key(keycode, RELEASED)
            if needs_shift:
                time.sleep(STEP_SLEEP_S)
                self._client.keyboard_key(42, RELEASED)
            time.sleep(MOVE_SETTLE_S)


# Process-wide client, replaced when the compositor closes the connection.
_client: EISClient | None = None


def client() -> EISClient:
    """The live EIS client, connecting on first use and reconnecting when lost."""
    global _client
    if _client is not None and _client._disconnected:  # noqa: SLF001 - same module owns the flag
        with_closing(_client)
        _client = None
    if _client is None:
        _client = EISClient()
    return _client


def with_closing(dead: EISClient) -> None:
    """Close a dead client without letting teardown errors surface."""
    try:
        dead.close()
    except Exception:  # noqa: BLE001 - teardown of a dead connection
        pass


def input_session() -> Input:
    """A gesture helper bound to the live client."""
    return Input(client())


def shutdown() -> None:
    """Close the process-wide client, if one is open."""
    global _client
    if _client is not None:
        with_closing(_client)
        _client = None


def probe() -> dict[str, Any]:
    """Report whether injection is possible without leaving a connection open."""
    result: dict[str, Any] = {
        "libei": libei_available(),
        "libeiPath": ctypes.util.find_library("ei"),
    }
    try:
        connection = EISClient()
    except BridgeError as exc:
        result["kwinEis"] = False
        result["error"] = exc.as_payload()
        return result
    result["kwinEis"] = True
    result["pointer"] = bool(connection._pointer)  # noqa: SLF001 - probe reads the negotiated slots
    result["keyboard"] = bool(connection._keyboard)  # noqa: SLF001
    with_closing(connection)
    return result
