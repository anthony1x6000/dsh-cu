"""The accessibility tree, with global coordinates and flatpak identity.

AT-SPI2 reports a widget's rectangle in *window-local* coordinates: a Wayland
client cannot know where the compositor put it. KWin does know, so each AT-SPI
top-level is matched to a KWin window and its origin is added to every element
rectangle inside it. When the match cannot be made with certainty the element is
reported as unavailable instead of with a plausible-but-wrong point, because a
wrong coordinate clicks something else.

Flatpak is the reason this module is more than a tree walk. A sandboxed app
publishes its accessibility tree through the sandbox's ``xdg-dbus-proxy``, so
AT-SPI reports the *proxy's* pid while KWin reports the app's. Both share the
instance's ``app-flatpak-<app-id>-<n>.scope`` cgroup, and ``flatpak.py`` turns
that scope into the app id and into the set of pids that own windows.

Part of dsh-cu, derived from kwin-mcp's ``accessibility.py`` (MIT,
Copyright (c) 2026 Byeonghoon Yoo) — see NOTICE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import dbus
import dbus.bus

from . import flatpak, kwin
from .rpc import BridgeError

# Widgets such as editors expose a whole document through the Text interface.
MAX_TEXT_CHARS = 200
# Acting on an element found by an incomplete discovery could trigger the wrong
# control, so a read error during an action search fails the request closed.
ACTION_SEARCH_MAX_DEPTH = 64
# How long a match whose window mapping failed because KWin's list changed is
# re-polled before the caller gets it as-is.
WINDOWS_CHANGED_SETTLE_S = 1.0

_atspi: Any = None


def atspi_module() -> Any:
    """Import PyGObject's Atspi binding on first use.

    Imported lazily so a host without PyGObject can still list windows, launch
    apps, and take screenshots; only the accessibility operations fail.
    """
    global _atspi
    if _atspi is None:
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi  # noqa: PLC0415 - deliberate lazy import
        except (ImportError, ValueError) as exc:
            raise BridgeError(
                f"AT-SPI2 support is unavailable ({exc}); install python3-gi and gir1.2-atspi-2.0",
                "ATSPI_UNAVAILABLE",
            ) from exc
        _atspi = Atspi
    return _atspi


# --- accessibility bus state -------------------------------------------------


def _session_bus() -> Any:
    """A session-bus connection honouring an explicit address."""
    import os

    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    return dbus.bus.BusConnection(address) if address else dbus.SessionBus()


def bus_state() -> dict[str, Any]:
    """Whether toolkit accessibility is enabled and what the a11y bus is.

    Toolkits only register with AT-SPI when the accessibility bus reports
    ``IsEnabled``; with it off, most applications publish nothing at all.
    """
    state: dict[str, Any] = {"available": False, "enabled": False, "address": None}
    try:
        bus = _session_bus()
        obj = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
        state["address"] = str(obj.GetAddress(dbus_interface="org.a11y.Bus"))
        props = obj.GetAll("org.a11y.Status", dbus_interface="org.freedesktop.DBus.Properties")
        state["enabled"] = bool(props.get("IsEnabled", False))
        state["screenReaderEnabled"] = bool(props.get("ScreenReaderEnabled", False))
        state["available"] = True
    except dbus.DBusException as exc:
        state["error"] = str(exc)
    return state


def set_enabled(enabled: bool = True) -> dict[str, Any]:
    """Turn toolkit accessibility on or off for this session.

    KDE ships accessibility off by default, and an application that started
    while it was off may not register until it restarts — so this is a
    necessary step for computer use, reported rather than hidden.
    """
    try:
        bus = _session_bus()
        obj = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
        obj.Set(
            "org.a11y.Status",
            "IsEnabled",
            dbus.Boolean(enabled),
            dbus_interface="org.freedesktop.DBus.Properties",
        )
    except dbus.DBusException as exc:
        raise BridgeError(
            f"cannot set org.a11y.Status.IsEnabled={enabled}: {exc}",
            "ATSPI_UNAVAILABLE",
        ) from exc
    return bus_state()


# --- element records ---------------------------------------------------------


@dataclass
class _Mapping:
    """How one AT-SPI top-level maps onto a KWin window.

    ``rect`` is the top-level's window-local rectangle, ``anchor`` the global
    origin of the matched KWin rectangle, and ``offset`` the translation added
    to every element inside. ``reason`` is set whenever the mapping failed.
    """

    window: dict[str, Any] | None
    reason: str
    rect: tuple[int, int, int, int] | None = None
    offset: tuple[int, int] | None = None
    anchor: tuple[int, int] | None = None


UNMAPPED = "not-a-window"


@dataclass
class Element:
    """One accessibility element, with its screen rectangle when proven."""

    role: str
    name: str
    description: str
    states: list[str]
    actions: list[str]
    text: str
    value: float | None
    value_max: float | None
    children_count: int
    depth: int
    has_extents: bool
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    app: str = ""
    app_id: str | None = None
    window_id: str | None = None
    mapped: bool = False
    unavailable: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Project to the JSON shape the tool layer returns."""
        return {
            "app": self.app,
            "flatpakAppId": self.app_id,
            "windowId": self.window_id,
            "role": self.role,
            "name": self.name,
            "description": self.description,
            "states": self.states,
            "actions": self.actions,
            "text": self.text,
            "value": self.value,
            "valueMax": self.value_max,
            "depth": self.depth,
            "children": self.children_count,
            "rect": (
                {"x": self.x, "y": self.y, "width": self.width, "height": self.height}
                if self.mapped else None
            ),
            "unavailable": None if self.mapped else (self.unavailable or "unmapped"),
        }


@dataclass
class _TopLevel:
    """One AT-SPI top-level window together with its resolved KWin mapping."""

    element: Any
    mapping: _Mapping


@dataclass
class _AppScan:
    """The result of preparing one AT-SPI application for a walk."""

    name: str
    pid: int | None
    pids: frozenset[int]
    children: list[Any]
    mappings: list[_Mapping]
    flatpak_app_id: str | None = None
    notes: list[str] = field(default_factory=list)


def _flatpak_identity(app_pid: int | None, pids: frozenset[int], windows: list[dict[str, Any]]) -> str | None:
    """The flatpak app id behind an AT-SPI application, when there is one.

    The sandbox proxy pid and the app's own pids are all tried; the scope is the
    only identity that is stable across the sandbox boundary.
    """
    for candidate in [app_pid, *sorted(pids)]:
        if candidate is None:
            continue
        app_id = flatpak.app_id_for_pid(int(candidate))
        if app_id:
            return app_id
    for window in windows:
        app_id = flatpak.app_id_for_pid(int(window["pid"]))
        if app_id:
            return app_id
    return None


# --- window mapping ----------------------------------------------------------


def _eligible(window: dict[str, Any]) -> bool:
    """Whether a KWin window can own an AT-SPI top-level.

    Popups stay eligible: a GTK3 menu is a separate AT-SPI top-level backed by
    a KWin popup window, and excluding popups would leave a wrong singleton
    match. They still have to pass the caption and size checks.
    """
    return bool(
        window["kind"] not in ("desktop", "dock", "notification")
        and window.get("deleted") is not True
    )


def _caption_consistent(caption: str, name: str) -> bool:
    """Whether a KWin caption is consistent with an AT-SPI top-level name.

    Both sides collapse whitespace runs, because KWin simplifies a client title
    into its caption. Exact equality and KDE's `` — App`` suffix are accepted; a
    plain prefix never is, so ``Document`` cannot claim ``Document 2 — KWrite``.
    """
    collapsed_name, collapsed_caption = " ".join(name.split()), " ".join(caption.split())
    if not collapsed_name or not collapsed_caption:
        return True
    return collapsed_caption == collapsed_name or collapsed_caption.startswith(collapsed_name + " \u2014 ")


def _window_pids(pid: int, windows: list[dict[str, Any]]) -> frozenset[int]:
    """KWin window pids that belong to the AT-SPI application ``pid``.

    Normally the same pid. A flatpak app reaches the accessibility bus through
    its sandbox's ``xdg-dbus-proxy``, so AT-SPI reports the proxy's pid while
    KWin reports the app's. Two resolutions, in order of precision:

    1. when ``/proc`` is readable, both processes share the instance's
       ``app-flatpak-<app-id>-<n>.scope`` cgroup path, which identifies exactly
       the windows of that instance and no other;
    2. when it is not (the caller runs in its own PID namespace), the flatpak
       instance table resolves the pid to an app id, which aliases every window
       of that app rather than of that one instance — less precise, but the only
       honest answer available.
    """
    if any(window["pid"] == pid for window in windows):
        return frozenset({pid})
    scope = flatpak.cgroup_scope(pid)
    if flatpak.comm_of(pid) == flatpak.DBUS_PROXY_COMM and flatpak.scope_unit(scope) is not None:
        owners = {
            int(window["pid"]) for window in windows
            if flatpak.cgroup_scope(int(window["pid"])) == scope
        }
        return frozenset({pid} | owners)
    app_id = flatpak.app_id_for_pid(pid)
    if app_id:
        owners = {
            int(window["pid"]) for window in windows
            if flatpak.app_id_for_pid(int(window["pid"])) == app_id
        }
        if owners:
            return frozenset({pid} | owners)
    return frozenset({pid})


def _match_toplevel(
    pids: frozenset[int],
    name: str,
    rect: tuple[int, int, int, int],
    windows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, tuple[int, int] | None]:
    """Match one AT-SPI top-level to exactly one KWin window.

    There is no singleton shortcut: even the only window of a process must
    survive the caption and size checks. The size is compared against both the
    client and the buffer rectangle, because a client-side-decorated window
    draws its shadow inside its own surface; when one window matches through
    both, the client origin wins because client rectangles are tried first.
    """
    candidates = [
        window for window in windows
        if window["pid"] in pids and _eligible(window) and _caption_consistent(window["caption"], name)
    ]
    size = (rect[2], rect[3])
    matches = [(window, window["client"]) for window in candidates]
    matches += [(window, window["buffer"]) for window in candidates]
    matches = [(window, rect_) for window, rect_ in matches if (rect_[2], rect_[3]) == size]
    if not matches:
        return None, "no-kwin-window", None
    if len({window["id"] for window, _ in matches}) > 1:
        return None, "ambiguous", None
    window, matched = matches[0]
    return window, "", (matched[0], matched[1])


def _window_rect(element: Any) -> tuple[int, int, int, int] | None:
    """One top-level's window-local rectangle, or None when it has none."""
    atspi = atspi_module()
    try:
        component = element.get_component_iface()
        if component is None:
            return None
        extents = atspi.Component.get_extents(component, atspi.CoordType.WINDOW)
    except Exception:  # noqa: BLE001 - any read failure means "no rectangle"
        return None
    return (int(extents.x), int(extents.y), int(extents.width), int(extents.height))


def _scan_app(app: Any, windows: list[dict[str, Any]], kwin_error: str) -> _AppScan:
    """Resolve one AT-SPI application's top-levels against the KWin snapshot."""
    atspi = atspi_module()
    try:
        name = app.get_name() or ""
    except Exception:  # noqa: BLE001
        name = ""
    try:
        pid: int | None = int(atspi.Accessible.get_process_id(app))
    except Exception:  # noqa: BLE001
        pid = None
    try:
        children = [app.get_child_at_index(index) for index in range(app.get_child_count())]
    except Exception:  # noqa: BLE001
        children = []

    def unmapped(reason: str) -> list[_Mapping]:
        return [_Mapping(None, reason) for _ in children]

    if pid is None or pid <= 0:
        return _AppScan(name, pid, frozenset(), children, unmapped("pid-unavailable"))
    if kwin_error:
        return _AppScan(name, pid, frozenset({pid}), children, unmapped("kwin-query-failed"))
    if any(child is None for child in children):
        return _AppScan(name, pid, frozenset({pid}), children, unmapped("window-set-mismatch"))

    pids = _window_pids(pid, windows)
    app_id = _flatpak_identity(pid, pids, windows)
    mappings: list[_Mapping] = []
    poisoned = False
    for child in children:
        if child is None:
            continue
        child_name = child.get_name() or ""
        rect = _window_rect(child)
        if rect is None:
            mappings.append(_Mapping(None, "no-extents"))
            continue
        window, reason, anchor = _match_toplevel(pids, child_name, rect, windows)
        # A caption-only miss does not prove the top-level owns no window: an
        # unnamed sibling of the same size could still claim that window.
        if reason == "ambiguous" or (
            reason == "no-kwin-window" and _match_toplevel(pids, "", rect, windows)[1] != "no-kwin-window"
        ):
            poisoned = True
        mappings.append(_Mapping(window, reason, rect, anchor=anchor))

    claimed: dict[str, int] = {}
    for mapping in mappings:
        if mapping.window is not None:
            claimed[mapping.window["id"]] = claimed.get(mapping.window["id"], 0) + 1
    if poisoned or any(count > 1 for count in claimed.values()):
        for mapping in mappings:
            if mapping.window is not None:
                mapping.window, mapping.reason = None, "ambiguous-window-match"
        return _AppScan(name, pid, pids, children, mappings, app_id)

    for mapping in mappings:
        if mapping.window is not None and mapping.rect is not None and mapping.anchor:
            mapping.offset = (mapping.anchor[0] - mapping.rect[0], mapping.anchor[1] - mapping.rect[1])
    return _AppScan(name, pid, pids, children, mappings, app_id)


def _window_key(window: dict[str, Any]) -> tuple[Any, ...]:
    """Identity plus geometry of a KWin window, for before/after comparison."""
    return (window["id"], window["pid"], window["caption"], *window["client"], *window["buffer"])


def _apply_stability(
    scans: list[tuple[_AppScan, list[_Mapping]]],
    windows_before: list[dict[str, Any]],
    kwin_error: str,
) -> None:
    """Unmap every top-level whose application's KWin window set changed.

    The traversal takes a couple of hundred milliseconds; a window opening,
    closing, or moving during it would otherwise have elements translated
    against stale geometry. The window pids are resolved again against the
    second snapshot, because a flatpak instance can gain a window owner while
    the walk is in flight.
    """
    if kwin_error:
        return
    windows_after = kwin.windows()
    for scan, mappings in scans:
        owners = set(scan.pids)
        for pid in scan.pids:
            owners |= set(_window_pids(int(pid), windows_after))
        before = {_window_key(window) for window in windows_before if window["pid"] in owners}
        after = {_window_key(window) for window in windows_after if window["pid"] in owners}
        if before != after:
            for mapping in mappings:
                if mapping.offset is not None:
                    mapping.window, mapping.offset, mapping.reason = None, None, "windows-changed"


# --- extraction --------------------------------------------------------------


def _extract(element: Any, depth: int) -> Element:
    """Read one AT-SPI element into an {@link Element} record."""
    atspi = atspi_module()
    try:
        role = element.get_role_name() or "unknown"
    except Exception:  # noqa: BLE001
        role = "unknown"
    try:
        name = element.get_name() or ""
    except Exception:  # noqa: BLE001
        name = ""
    try:
        description = element.get_description() or ""
    except Exception:  # noqa: BLE001
        description = ""
    states: list[str] = []
    try:
        states = [state.value_nick for state in element.get_state_set().get_states() if state.value_nick]
    except Exception:  # noqa: BLE001
        pass
    actions: list[str] = []
    try:
        action_iface = element.get_action_iface()
        if action_iface is not None:
            for index in range(atspi.Action.get_n_actions(action_iface)):
                action_name = atspi.Action.get_action_name(action_iface, index)
                if action_name:
                    actions.append(str(action_name))
    except Exception:  # noqa: BLE001
        pass
    text = ""
    try:
        text_iface = element.get_text_iface()
        if text_iface is not None:
            # Called through the interface class: the bound method returns an
            # empty string on this PyGObject version.
            count = atspi.Text.get_character_count(text_iface)
            if count:
                text = atspi.Text.get_text(text_iface, 0, min(count, MAX_TEXT_CHARS))
                if count > MAX_TEXT_CHARS:
                    text += "\u2026"
    except Exception:  # noqa: BLE001
        pass
    value: float | None = None
    value_max: float | None = None
    try:
        value_iface = element.get_value_iface()
        if value_iface is not None:
            value = float(atspi.Value.get_current_value(value_iface))
            value_max = float(atspi.Value.get_maximum_value(value_iface))
    except Exception:  # noqa: BLE001
        pass
    has_extents = False
    x = y = width = height = 0
    try:
        component = element.get_component_iface()
        if component is not None:
            extents = atspi.Component.get_extents(component, atspi.CoordType.WINDOW)
            x, y, width, height = int(extents.x), int(extents.y), int(extents.width), int(extents.height)
            has_extents = True
    except Exception:  # noqa: BLE001
        has_extents = False
    try:
        children_count = int(element.get_child_count())
    except Exception:  # noqa: BLE001
        children_count = 0
    return Element(
        role=role, name=name, description=description, states=states, actions=actions, text=text,
        value=value, value_max=value_max, children_count=children_count, depth=depth,
        has_extents=has_extents, x=x, y=y, width=width, height=height,
    )


def _finalize(element: Element, mapping: _Mapping | None) -> Element:
    """Translate window-local extents into screen coordinates, or mark them unavailable."""
    if not element.has_extents:
        element.unavailable = "no-extents"
        return element
    if element.width <= 0 or element.height <= 0:
        element.unavailable = "empty-extents"
        return element
    if mapping is not None and mapping.offset is not None:
        element.x += mapping.offset[0]
        element.y += mapping.offset[1]
        element.mapped = True
        element.unavailable = ""
        element.window_id = mapping.window["id"] if mapping.window else None
        return element
    element.mapped = False
    element.unavailable = (mapping.reason if mapping is not None else UNMAPPED) or "unmapped"
    return element


def _walk(element: Any, mapping: _Mapping, out: list[tuple[Element, _Mapping]], depth: int, max_depth: int) -> None:
    """Collect every descendant in pre-order, carrying the top-level mapping."""
    if depth > max_depth:
        return
    info = _extract(element, depth)
    out.append((info, mapping))
    for index in range(info.children_count):
        try:
            child = element.get_child_at_index(index)
        except Exception:  # noqa: BLE001 - a vanished child is skipped
            continue
        if child is not None:
            _walk(child, mapping, out, depth + 1, max_depth)


def _matches(element: Element, query: str, states: list[str] | None) -> bool:
    """Whether an element matches a case-insensitive query and all required states."""
    haystack = f"{element.name}\n{element.role}\n{element.description}".lower()
    if query and query not in haystack:
        return False
    return states is None or all(state in element.states for state in states)


def _prepare(
    app_name: str = "",
    window_id: str = "",
    ensure_enabled: bool = True,
) -> tuple[list[tuple[_AppScan, list[_Mapping]]], list[dict[str, Any]], list[Element], str, bool]:
    """Snapshot KWin, resolve every matching AT-SPI application, and scan it.

    Returns the per-application scans (for the stability pass), the KWin
    snapshot, the application roots, any KWin error, and whether accessibility
    had to be switched on for this call.
    """
    atspi = atspi_module()
    enabled_here = False
    state = bus_state()
    if ensure_enabled and state.get("available") and not state.get("enabled"):
        set_enabled(True)
        enabled_here = True

    kwin_error = ""
    try:
        windows_before = kwin.windows()
    except BridgeError as exc:
        windows_before, kwin_error = [], str(exc)

    desktop = atspi.get_desktop(0)
    wanted_pid: int | None = None
    if window_id:
        target = kwin.find_window(window_id=window_id)
        if target is None:
            raise BridgeError(f"no window with id {window_id}", "NO_SUCH_WINDOW")
        wanted_pid = int(target["pid"])

    scans: list[tuple[_AppScan, list[_Mapping]]] = []
    roots: list[Element] = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        app_label = ""
        try:
            app_label = app.get_name() or ""
        except Exception:  # noqa: BLE001
            app_label = ""
        if app_name and app_name.lower() not in app_label.lower():
            continue
        scan = _scan_app(app, windows_before, kwin_error)
        if wanted_pid is not None:
            owners = set(scan.pids)
            if wanted_pid not in owners and not any(window["pid"] == wanted_pid for window in windows_before):
                continue
            if scan.pid != wanted_pid and not (owners & {wanted_pid}):
                continue
        root = _extract(app, 0)
        root.app = app_label
        root.app_id = scan.flatpak_app_id
        roots.append(root)
        scans.append((scan, scan.mappings))
    return scans, windows_before, roots, kwin_error, enabled_here


def tree(
    *,
    app_name: str = "",
    window_id: str = "",
    max_depth: int = 15,
    role: str = "",
    max_elements: int = 400,
    ensure_enabled: bool = True,
) -> dict[str, Any]:
    """Walk the accessibility tree of one or every application.

    The role filter is applied after the walk, exactly like AT-SPI semantics
    require: a non-matching element is hidden but its children are still
    traversed, so depth never shifts under the caller.
    """
    if max_depth < 0:
        raise BridgeError("maxDepth must be 0 or positive", "BAD_ARGUMENT")
    scans, windows_before, roots, kwin_error, enabled_here = _prepare(
        app_name=app_name, window_id=window_id, ensure_enabled=ensure_enabled
    )
    collected: list[tuple[Element, _Mapping]] = []
    for scan, mappings in scans:
        elements_before = len(collected)
        for child, mapping in zip(scan.children, mappings, strict=False):
            if child is None:
                continue
            _walk(child, mapping, collected, depth=1, max_depth=max_depth)
        _annotate(scan, [element for element, _ in collected[elements_before:]])
    _apply_stability(scans, windows_before, kwin_error)

    role_filter = role.strip().lower()
    elements: list[Element] = []
    truncated = False
    for element, mapping in collected:
        if role_filter and role_filter != element.role.lower():
            continue
        if len(elements) >= max_elements:
            truncated = True
            break
        elements.append(_finalize(element, mapping))

    root_projection = [_finalize(root, None) for root in roots]
    return {
        "elements": [element.as_dict() for element in elements],
        "applications": [root.as_dict() for root in root_projection],
        "count": len(elements),
        "truncated": truncated,
        "accessibilityEnabled": bus_state().get("enabled", False),
        "enabledByThisCall": enabled_here,
        "kwinError": kwin_error or None,
    }


def _annotate(scan: _AppScan, elements: list[Element]) -> None:
    """Attach the app name and flatpak id to every element of one scan."""
    for element in elements:
        if element.app == "":
            element.app = scan.name
            element.app_id = scan.flatpak_app_id


def find(
    *,
    query: str = "",
    app_name: str = "",
    window_id: str = "",
    states: list[str] | None = None,
    wait_ms: int = 0,
    poll_ms: int = 200,
    max_results: int = 50,
    ensure_enabled: bool = True,
) -> dict[str, Any]:
    """Find elements whose name, role, or description contains ``query``.

    With ``wait_ms`` the search re-polls until a match appears, and re-polls
    briefly when the only reason a match lacks coordinates is that KWin's window
    list changed during the walk.
    """
    deadline = time.monotonic() + max(0, wait_ms) / 1000.0
    settle_deadline: float | None = None
    enabled_here = False
    while True:
        scans, windows_before, _roots, kwin_error, enabled_here = _prepare(
            app_name=app_name, window_id=window_id, ensure_enabled=ensure_enabled
        )
        collected: list[tuple[Element, _Mapping]] = []
        for scan, mappings in scans:
            for child, mapping in zip(scan.children, mappings, strict=False):
                if child is None:
                    continue
                before = len(collected)
                _search(child, mapping, query.lower(), states, collected, depth=1, max_depth=15)
                _annotate(scan, [element for element, _ in collected[before:]])
        _apply_stability(scans, windows_before, kwin_error)
        results = [_finalize(element, mapping) for element, mapping in collected]
        matches = [element for element in results if _matches(element, query.lower(), states)]
        now = time.monotonic()
        if matches:
            if not any(element.unavailable == "windows-changed" for element in matches):
                break
            if settle_deadline is None:
                settle_deadline = min(deadline, now + WINDOWS_CHANGED_SETTLE_S)
            if now >= settle_deadline:
                break
            time.sleep(max(0.0, min(poll_ms / 1000.0, settle_deadline - now)))
            continue
        if now >= deadline:
            break
        time.sleep(poll_ms / 1000.0)

    truncated = len(matches) > max_results
    return {
        "matches": [element.as_dict() for element in matches[:max_results]],
        "count": min(len(matches), max_results),
        "totalMatches": len(matches),
        "truncated": truncated,
        "accessibilityEnabled": bus_state().get("enabled", False),
        "enabledByThisCall": enabled_here,
        "kwinError": kwin_error or None,
    }


def _search(
    element: Any,
    mapping: _Mapping,
    query: str,
    states: list[str] | None,
    out: list[tuple[Element, _Mapping]],
    depth: int,
    max_depth: int,
) -> None:
    """Collect matching descendants; the caller re-checks the query itself."""
    if depth > max_depth:
        return
    info = _extract(element, depth)
    if _matches(info, query, states):
        out.append((info, mapping))
    for index in range(info.children_count):
        try:
            child = element.get_child_at_index(index)
        except Exception:  # noqa: BLE001
            continue
        if child is not None:
            _search(child, mapping, query, states, out, depth + 1, max_depth)


def invoke(
    *,
    query: str,
    app_name: str = "",
    window_id: str = "",
    action_name: str = "",
    index: int | None = None,
    ensure_enabled: bool = True,
) -> dict[str, Any]:
    """Run one action on exactly one matching element.

    Two matches are a refusal unless ``index`` picks one: acting on the wrong
    match triggers the wrong control, and the caller cannot undo it. Any read
    error inside the searched scope also fails closed, because an incomplete
    discovery cannot prove the match is unique.
    """
    atspi = atspi_module()
    if query.strip() == "":
        raise BridgeError("invoke needs a query", "BAD_ARGUMENT")
    state = bus_state()
    if ensure_enabled and state.get("available") and not state.get("enabled"):
        set_enabled(True)
    try:
        windows = kwin.windows()
        kwin_error = ""
    except BridgeError as exc:
        windows, kwin_error = [], str(exc)
    if kwin_error:
        raise BridgeError(f"cannot resolve screen coordinates: {kwin_error}", "KWIN_UNAVAILABLE")

    desktop = atspi.get_desktop(0)
    candidates: list[tuple[Any, int, str, str, str]] = []
    wanted_pid: int | None = None
    if window_id:
        target = kwin.find_window(window_id=window_id)
        if target is None:
            raise BridgeError(f"no window with id {window_id}", "NO_SUCH_WINDOW")
        wanted_pid = int(target["pid"])
    query_lower = query.lower()
    for app_index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(app_index)
        if app is None:
            continue
        label = ""
        try:
            label = app.get_name() or ""
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"could not read application name: {exc}", "ATSPI_READ_FAILED") from exc
        if app_name and app_name.lower() not in label.lower():
            continue
        scan = _scan_app(app, windows, kwin_error)
        if wanted_pid is not None and wanted_pid not in scan.pids:
            continue
        for child, mapping in zip(scan.children, scan.mappings, strict=False):
            if child is None:
                continue
            _collect_actions(child, mapping, query_lower, action_name.lower(), candidates, depth=1)
    if not candidates:
        return {"invoked": False, "matches": 0, "reason": f"no element matches {query!r}"}
    if index is None and len(candidates) != 1:
        return {
            "invoked": False,
            "matches": len(candidates),
            "reason": "more than one element matches; pass index to choose one",
            "candidates": [_candidate_summary(candidate) for candidate in candidates[:20]],
        }
    element, action_index, label, role, action_label = candidates[index or 0]
    try:
        invoked = bool(atspi.Action.do_action(element.get_action_iface(), action_index))
    except Exception as exc:  # noqa: BLE001 - the action may still have run
        return {
            "invoked": None,
            "matches": len(candidates),
            "error": str(exc),
            "app": label,
            "role": role,
            "action": action_label,
        }
    return {
        "invoked": invoked,
        "matches": len(candidates),
        "app": label,
        "role": role,
        "action": action_label,
    }


def _candidate_summary(candidate: tuple[Any, int, str, str, str]) -> dict[str, Any]:
    """A short projection of one invoke candidate for the refusal payload."""
    _element, _index, label, role, action_label = candidate
    return {"app": label, "role": role, "action": action_label}


def _collect_actions(
    element: Any,
    mapping: _Mapping,
    query: str,
    wanted_action: str,
    out: list[tuple[Any, int, str, str, str]],
    depth: int,
) -> None:
    """Collect (element, action index) pairs matching the query and action name."""
    atspi = atspi_module()
    if depth > ACTION_SEARCH_MAX_DEPTH:
        raise BridgeError(
            f"the accessibility subtree is deeper than {ACTION_SEARCH_MAX_DEPTH} levels; "
            "acting on it cannot be proven safe",
            "ATSPI_TOO_DEEP",
        )
    try:
        name = element.get_name() or ""
        role = element.get_role_name() or ""
        description = element.get_description() or ""
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"could not read element identity: {exc}", "ATSPI_READ_FAILED") from exc
    haystack = f"{name}\n{role}\n{description}".lower()
    if query in haystack:
        try:
            action_iface = element.get_action_iface()
            count = atspi.Action.get_n_actions(action_iface) if action_iface is not None else 0
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"could not read the action interface: {exc}", "ATSPI_READ_FAILED") from exc
        for action_index in range(count):
            try:
                action_name = atspi.Action.get_action_name(action_iface, action_index) or ""
            except Exception as exc:  # noqa: BLE001
                raise BridgeError(f"could not read an action name: {exc}", "ATSPI_READ_FAILED") from exc
            if wanted_action in ("", "click", "activate", "default") and action_name.lower() in ("click", "activate", "press", "toggle"):
                out.append((element, action_index, name, role, action_name))
            elif wanted_action != "" and action_name.lower() == wanted_action:
                out.append((element, action_index, name, role, action_name))
    try:
        children_count = int(element.get_child_count())
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"could not read the child count: {exc}", "ATSPI_READ_FAILED") from exc
    for child_index in range(children_count):
        try:
            child = element.get_child_at_index(child_index)
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"could not read a child: {exc}", "ATSPI_READ_FAILED") from exc
        if child is not None:
            _collect_actions(child, mapping, query, wanted_action, out, depth + 1)


def list_windows() -> dict[str, Any]:
    """Every application AT-SPI knows, with its top-level windows.

    This is the accessibility view, not the compositor view: it only lists apps
    that publish a tree, which is exactly the set that can be driven by element
    actions rather than by synthetic input.
    """
    atspi = atspi_module()
    state = bus_state()
    if state.get("available") and not state.get("enabled"):
        set_enabled(True)
    desktop = atspi.get_desktop(0)
    apps: list[dict[str, Any]] = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        try:
            child_count = app.get_child_count()
        except Exception:  # noqa: BLE001
            continue
        if child_count == 0:
            continue
        pid: int | None = None
        try:
            pid = int(atspi.Accessible.get_process_id(app))
        except Exception:  # noqa: BLE001
            pid = None
        windows: list[dict[str, Any]] = []
        for child_index in range(child_count):
            window = app.get_child_at_index(child_index)
            if window is None:
                continue
            try:
                state_set = window.get_state_set()
                windows.append({
                    "title": window.get_name() or "(untitled)",
                    "active": bool(state_set.contains(atspi.StateType.ACTIVE)),
                    "focused": bool(state_set.contains(atspi.StateType.FOCUSED)),
                })
            except Exception:  # noqa: BLE001
                continue
        apps.append({
            "name": app.get_name() or "(unnamed)",
            "pid": pid,
            "flatpakAppId": flatpak.app_id_for_pid(pid) if pid else None,
            "proxyPid": pid if pid and flatpak.comm_of(pid) == flatpak.DBUS_PROXY_COMM else None,
            "windowCount": child_count,
            "windows": windows,
        })
    return {"applications": apps, "count": len(apps), "accessibilityEnabled": bool(state.get("enabled"))}
