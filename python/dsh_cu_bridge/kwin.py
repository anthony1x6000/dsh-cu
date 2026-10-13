"""Window control and output topology through KWin's scripting engine.

A KWin script runs in a sandboxed QJSEngine with no file, socket, or process
access; the only way out is ``callDBus`` back into the process that loaded it.
So every operation here writes a small JavaScript file, loads it through
``org.kde.kwin.Scripting``, and waits for the script to report JSON to a
DBus service this module exports. Replies are dispatched on the calling thread
through the default GLib main context: a GLib loop on a background thread races
libatspi's use of the same context and was observed to corrupt the heap.

Because the reply is a JSON string, every caller-supplied value reaches the
script as a JSON literal (see ``_render_script``) and can never execute as
script source.

This is also the flatpak-neutral half of the plugin: KWin sees a flatpak
window as an ordinary Wayland client, so listing, activating, moving, and
closing one needs no sandbox privileges at all.

Part of dsh-cu, derived from kwin-mcp's ``geometry.py`` (MIT,
Copyright (c) 2026 Byeonghoon Yoo) — see NOTICE.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import dbus
import dbus.bus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

from .rpc import BridgeError

SINK_NAME = "dev.dshcu.KWinBridge"
SINK_PATH = "/dshcu"
SCRIPT_TIMEOUT_S = 8.0

# Window properties worth reporting. ``maximizeMode`` and the desktop list are
# read defensively: a property that this KWin build does not expose must not
# fail the whole query.
_REPORT_WINDOW = """
function reportWindow(w) {
    var rec = {
        pid: w.pid,
        id: String(w.internalId),
        app: String(w.resourceClass),
        resourceName: String(w.resourceName),
        caption: w.caption,
        normal: w.normalWindow,
        popup: w.popupWindow,
        managed: w.managed,
        deleted: w.deleted,
        desktop: w.desktopWindow,
        dock: w.dock,
        notification: w.notification,
        active: w === workspace.activeWindow,
        minimized: w.minimized,
        fullScreen: w.fullScreen,
        keepAbove: w.keepAbove,
        skipTaskbar: w.skipTaskbar,
        onAllDesktops: w.onAllDesktops,
        closeable: w.closeable,
        moveable: w.moveable,
        resizeable: w.resizeable,
        maximizable: w.maximizable,
        frame: [w.frameGeometry.x, w.frameGeometry.y, w.frameGeometry.width, w.frameGeometry.height],
        client: [w.clientGeometry.x, w.clientGeometry.y, w.clientGeometry.width, w.clientGeometry.height],
        buffer: [w.bufferGeometry.x, w.bufferGeometry.y, w.bufferGeometry.width, w.bufferGeometry.height]
    };
    try { rec.maximizeMode = String(w.maximizeMode); } catch (e) { rec.maximizeMode = null; }
    try {
        if (w.screen) { rec.output = String(w.screen.name); }
        else if (w.output) { rec.output = String(w.output.name); }
        else { rec.output = null; }
    } catch (e) { rec.output = null; }
    try {
        var ds = [];
        for (var d = 0; d < w.desktops.length; d++) { ds.push(String(w.desktops[d].x11DesktopNumber)); }
        rec.desktops = ds;
    } catch (e) { rec.desktops = null; }
    return rec;
}
"""

_WINDOWS_SCRIPT = _REPORT_WINDOW + """
var out = [];
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) { out.push(reportWindow(windows[i])); }
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(out));
"""

_OUTPUTS_SCRIPT = """
var screens = [];
var list = workspace.screens;
for (var i = 0; i < list.length; i++) {
    var s = list[i];
    var g = s.geometry;
    screens.push({ name: String(s.name), scale: s.devicePixelRatio,
                   geometry: [g.x, g.y, g.width, g.height] });
}
var v = workspace.virtualScreenGeometry;
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify({
    screens: screens,
    virtual: v ? [v.x, v.y, v.width, v.height] : null,
    activeOutput: workspace.activeScreen ? String(workspace.activeScreen.name) : null
}));
"""

# One script for every mutation, so the before/after the caller sees comes from
# a single round trip and cannot be interleaved with another change.
_ACTION_SCRIPT = _REPORT_WINDOW + """
var target = {window_id};
var action = {action};
var value = {value};
var rect = {rect};
var result = { found: false, action: action };
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (String(w.internalId) !== target) { continue; }
    result.found = true;
    result.before = reportWindow(w);
    if (action === "activate") {
        workspace.activeWindow = w;
    } else if (action === "close") {
        if (!w.closeable) { result.refused = "not-closeable"; }
        else { w.closeWindow(); }
    } else if (action === "minimize") {
        if (value === null) { value = !w.minimized; }
        if (!w.minimizable) { result.refused = "not-minimizable"; }
        else { w.minimized = value; }
    } else if (action === "maximize") {
        if (!w.maximizable) { result.refused = "not-maximizable"; }
        else { w.setMaximize(value !== false, value !== false); }
    } else if (action === "fullscreen") {
        if (value && !w.fullScreenable) { result.refused = "not-fullscreenable"; }
        else { w.fullScreen = value === true; }
    } else if (action === "keep_above") {
        w.keepAbove = value === true;
    } else if (action === "on_all_desktops") {
        w.onAllDesktops = value === true;
    } else if (action === "geometry") {
        var area = workspace.virtualScreenGeometry;
        if (rect.width !== null && rect.width > area.width) { result.refused = "width"; result.limit = area.width; }
        else if (rect.height !== null && rect.height > area.height) { result.refused = "height"; result.limit = area.height; }
        else {
            var g = Object.assign({}, w.frameGeometry);
            if (rect.x !== null) { g.x = rect.x; }
            if (rect.y !== null) { g.y = rect.y; }
            if (rect.width !== null) { g.width = rect.width; }
            if (rect.height !== null) { g.height = rect.height; }
            w.frameGeometry = g;
        }
    } else {
        result.refused = "unknown-action";
    }
    result.after = reportWindow(w);
    break;
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(result));
"""

# The active window's app class and geometry, for the clipboard paste path.
_ACTIVE_SCRIPT = _REPORT_WINDOW + """
var w = workspace.activeWindow;
callDBus("{sink}", "{path}", "{sink}", "Report",
         JSON.stringify(w ? reportWindow(w) : null));
"""

# The pointer position, in the same global logical space EIS accepts.
_CURSOR_SCRIPT = """
var p = workspace.cursorPos;
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify([p.x, p.y]));
"""


def _render_script(template: str, values: dict[str, Any]) -> str:
    """Substitute ``{name}`` placeholders with ``json.dumps(values[name])``.

    All placeholders are substituted in one pass, so a value that itself
    contains ``{other}`` stays a literal inside its JSON string instead of
    being expanded into script source.
    """
    if not values:
        return template
    pattern = "|".join(re.escape("{" + name + "}") for name in values)
    return re.sub(pattern, lambda match: json.dumps(values[match.group(0)[1:-1]]), template)


def _session_bus() -> Any:
    """Open the session bus, honouring an explicit address when one is set."""
    DBusGMainLoop(set_as_default=True)
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if address:
        return dbus.bus.BusConnection(address)
    return dbus.SessionBus()


def run_kwin_script(js_source: str, timeout: float = SCRIPT_TIMEOUT_S) -> str:
    """Run one KWin script and return the payload it reported over D-Bus.

    ``{sink}``/``{path}`` placeholders are filled with this call's reply
    endpoint. The endpoint name embeds the pid so concurrent queries cannot
    collide, and both the bus-name and the exported object are kept alive until
    the wait finishes — dropping either makes KWin's ``callDBus`` land nowhere.
    """
    from gi.repository import GLib

    bus = _session_bus()
    sink_name = f"{SINK_NAME}.p{os.getpid()}"
    received: list[str] = []

    class _Sink(dbus.service.Object):
        @dbus.service.method(sink_name, in_signature="s", out_signature="")
        def Report(self, payload: str) -> None:  # noqa: N802 - D-Bus method name
            received.append(str(payload))

    bus_name = dbus.service.BusName(sink_name, bus)
    sink = _Sink(bus, SINK_PATH)

    # The script must live somewhere KWin can read. A sandboxed caller whose
    # /tmp is private points DSH_CU_SCRIPT_DIR at a shared directory.
    script_dir = Path(os.environ.get("DSH_CU_SCRIPT_DIR") or tempfile.gettempdir())
    script_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", prefix="dsh-cu-kwin-", suffix=".js", dir=script_dir, delete=False
    )
    script_path = Path(handle.name)
    try:
        handle.write(js_source.replace("{sink}", sink_name).replace("{path}", SINK_PATH))
        handle.close()
        scripting = dbus.Interface(
            bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting"
        )
        try:
            scripting.loadScript(str(script_path))
            scripting.start()
        except dbus.DBusException as exc:
            raise BridgeError(
                f"KWin refused to load a script: {exc}",
                "KWIN_SCRIPTING_UNAVAILABLE",
                {"hint": "org.kde.KWin.Scripting is the interface; is this a KWin session?"},
            ) from exc

        context = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            context.iteration(False) or time.sleep(0.005)
    finally:
        with contextlib.suppress(dbus.DBusException):
            scripting.unloadScript(str(script_path))
        script_path.unlink(missing_ok=True)
        sink.remove_from_connection()
        del bus_name

    if not received:
        raise BridgeError(
            f"KWin did not answer the scripting query within {timeout:g}s",
            "KWIN_TIMEOUT",
            {"hint": "the script file must be readable by KWin; set DSH_CU_SCRIPT_DIR"},
        )
    return received[0]


def _as_int_list(value: Any) -> list[int]:
    """Round one KWin qreal rectangle to four integers."""
    return [round(float(item)) for item in value]


def _parse_windows(payload: str) -> list[dict[str, Any]]:
    """Decode the JSON window list a script reported, skipping malformed rows."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return []
    windows: list[dict[str, Any]] = []
    for record in raw:
        if not isinstance(record, dict):
            continue
        try:
            windows.append({
                "pid": int(record["pid"]),
                "id": str(record["id"]),
                "app": str(record["app"]),
                "resourceName": str(record.get("resourceName", "")),
                "caption": str(record["caption"]),
                "kind": _kind(record),
                "active": bool(record["active"]),
                "minimized": bool(record["minimized"]),
                "fullScreen": bool(record["fullScreen"]),
                "keepAbove": bool(record["keepAbove"]),
                "closeable": bool(record["closeable"]),
                "moveable": bool(record["moveable"]),
                "resizeable": bool(record["resizeable"]),
                "maximizable": bool(record["maximizable"]),
                "maximizeMode": record.get("maximizeMode"),
                "output": record.get("output"),
                "desktops": record.get("desktops"),
                "frame": _as_int_list(record["frame"]),
                "client": _as_int_list(record["client"]),
                "buffer": _as_int_list(record["buffer"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return windows


def _kind(record: dict[str, Any]) -> str:
    """Classify one KWin window into the vocabulary callers filter on."""
    if record.get("desktop"):
        return "desktop"
    if record.get("dock"):
        return "dock"
    if record.get("notification"):
        return "notification"
    if record.get("popup"):
        return "popup"
    if record.get("normal"):
        return "normal"
    return "other"


def windows(timeout: float = SCRIPT_TIMEOUT_S) -> list[dict[str, Any]]:
    """Every window KWin knows about, including popups and docks."""
    return _parse_windows(run_kwin_script(_WINDOWS_SCRIPT, timeout))


def outputs(timeout: float = SCRIPT_TIMEOUT_S) -> dict[str, Any]:
    """Output topology: per-output logical geometry and scale, plus the union."""
    payload = json.loads(run_kwin_script(_OUTPUTS_SCRIPT, timeout))
    screens = []
    for screen in payload.get("screens") or []:
        x, y, width, height = (float(v) for v in screen["geometry"])
        screens.append({
            "name": str(screen["name"]),
            "scale": float(screen["scale"]),
            "geometry": [round(x), round(y), round(width), round(height)],
        })
    virtual = payload.get("virtual")
    if isinstance(virtual, list) and len(virtual) == 4:
        virtual_rect = [round(float(v)) for v in virtual]
    else:
        left = min((s["geometry"][0] for s in screens), default=0)
        top = min((s["geometry"][1] for s in screens), default=0)
        right = max((s["geometry"][0] + s["geometry"][2] for s in screens), default=0)
        bottom = max((s["geometry"][1] + s["geometry"][3] for s in screens), default=0)
        virtual_rect = [left, top, right - left, bottom - top]
    return {"screens": screens, "virtual": virtual_rect, "activeOutput": payload.get("activeOutput")}


def find_window(window_id: str = "", app: str = "") -> dict[str, Any] | None:
    """Resolve one window by exact internal id, else by app/caption substring.

    ``window_id`` wins when both are given. The substring match prefers normal
    windows and an active match, so ``app="brave"`` finds the window a user
    would point at rather than a hidden popup of the same process.
    """
    candidates = windows()
    if window_id:
        for window in candidates:
            if window["id"] == window_id:
                return window
        return None
    needle = app.strip().lower()
    if not needle:
        return None
    matches = [
        window for window in candidates
        if needle in f"{window['app']} {window['caption']} {window['resourceName']}".lower()
    ]
    if not matches:
        return None
    matches.sort(key=lambda window: (
        window["kind"] != "normal",
        not window["active"],
        window["id"],
    ))
    return matches[0]


def require_window(window_id: str = "", app: str = "") -> dict[str, Any]:
    """Like {@link find_window} but raises the routable failure when nothing matches."""
    window = find_window(window_id=window_id, app=app)
    if window is None:
        target = window_id or app
        raise BridgeError(
            f"no window matches {target!r}",
            "NO_SUCH_WINDOW" if window_id else "NO_MATCHING_WINDOW",
        )
    return window


def active_window() -> dict[str, Any] | None:
    """The active window, or None when the desktop has no active client."""
    payload = run_kwin_script(_ACTIVE_SCRIPT)
    if payload in ("null", ""):
        return None
    parsed = _parse_windows(f"[{payload}]")
    return parsed[0] if parsed else None


def cursor_position() -> dict[str, int]:
    """The pointer's global logical position.

    Read from KWin rather than tracked locally: the user moves the pointer too,
    and every coordinate this plugin accepts is in KWin's logical space.
    """
    payload = json.loads(run_kwin_script(_CURSOR_SCRIPT))
    try:
        return {"x": round(float(payload[0])), "y": round(float(payload[1]))}
    except (IndexError, TypeError, ValueError) as exc:
        raise BridgeError(f"KWin returned an unreadable cursor position: {payload!r}", "KWIN_BAD_REPLY") from exc


def window_action(
    action: str,
    *,
    window_id: str = "",
    app: str = "",
    value: bool | None = None,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Mutate one window and return its state before and after.

    Activation goes through KWin (``workspace.activeWindow``) rather than
    AT-SPI: on Wayland AT-SPI's ``grab_focus`` neither raises nor activates a
    window, so it reports success while the previous window keeps focus.
    """
    window = require_window(window_id=window_id, app=app)
    rect = {
        "x": None if x is None else int(x),
        "y": None if y is None else int(y),
        "width": None if width is None else int(width),
        "height": None if height is None else int(height),
    }
    script = _render_script(_ACTION_SCRIPT, {
        "window_id": window["id"],
        "action": action,
        "value": None if value is None else bool(value),
        "rect": rect,
    })
    payload = run_kwin_script(script)
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"KWin returned an unreadable action reply: {payload[:200]}", "KWIN_BAD_REPLY") from exc
    if not result.get("found"):
        raise BridgeError(
            f"window {window['id']} disappeared before the action ran",
            "NO_SUCH_WINDOW",
        )
    before = result.get("before")
    after = result.get("after")
    return {
        "action": action,
        "refused": result.get("refused"),
        "limit": result.get("limit"),
        "window": _parse_windows(json.dumps([after]))[0] if after else None,
        "before": _parse_windows(json.dumps([before]))[0] if before else None,
    }
