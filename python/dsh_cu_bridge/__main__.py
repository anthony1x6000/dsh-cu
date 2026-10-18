"""dsh-cu bridge: one JSON-RPC process serving every desktop operation.

The TypeScript plugin spawns this module once and speaks newline-delimited JSON
over stdio. Keeping it resident matters for two reasons: the EIS input
connection is a handshake plus a socket that would otherwise be rebuilt for
every click, and PyGObject binds its AT-SPI D-Bus connection once per process.

Protocol (one request object per line on stdin, one response object per line on
stdout)::

    {"id": 3, "op": "window.list", "args": {...}}
    {"id": 3, "ok": true, "result": {...}}
    {"id": 3, "ok": false, "error": {"code": "NO_SUCH_WINDOW", "message": "..."}}

stdout carries frames only; diagnostics go to stderr.

Part of dsh-cu, derived from kwin-mcp (MIT, Copyright (c) 2026 Byeonghoon Yoo)
— see NOTICE.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import atspi, clipboard, eis, flatpak, keys, kwin, rpc, shot, status, wait
from .rpc import BridgeError

OPS: dict[str, Callable[[dict[str, Any]], Any]] = {}

#: Wait budget used when a caller asks to wait for a window but names none: long
#: enough for a cold application start, short enough that a typo in the match
#: string fails the call instead of hanging it.
DEFAULT_WAIT_MS = 15_000


def op(name: str) -> Callable[[Callable[[dict[str, Any]], Any]], Callable[[dict[str, Any]], Any]]:
    """Register one operation under its wire name."""

    def register(function: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        OPS[name] = function
        return function

    return register


def _int(args: dict[str, Any], key: str, default: int | None = None) -> int:
    """Read a required-or-defaulted integer argument."""
    value = args.get(key, default)
    if value is None:
        raise BridgeError(f"missing required integer argument {key!r}", "BAD_ARGUMENT")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError(f"{key} must be an integer, got {value!r}", "BAD_ARGUMENT") from exc


def _str(args: dict[str, Any], key: str, default: str = "") -> str:
    """Read a string argument with a default."""
    value = args.get(key, default)
    return "" if value is None else str(value)


def _bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
    """Read a boolean argument with a default."""
    value = args.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no")
    return bool(value)


# --- status and windows ------------------------------------------------------


@op("status")
def op_status(args: dict[str, Any]) -> dict[str, Any]:
    """Report host capabilities; see ``status.probe``."""
    return status.probe(refresh_screenshot_authorization=_bool(args, "refreshScreenshotAuthorization"))


def _window_matches(
    window: dict[str, Any], *, app: str = "", caption: str = "", text: str = ""
) -> bool:
    """Whether one window satisfies every substring filter it was given."""
    haystack = f"{window['app']} {window['caption']} {window['resourceName']}".lower()
    if app and app.lower() not in haystack:
        return False
    if caption and caption.lower() not in window["caption"].lower():
        return False
    if text and text.lower() not in haystack:
        return False
    return True


def _await_window(wait_for: str, app: str, timeout_ms: int) -> float:
    """Block until a window matching the filters exists; return the wait in seconds.

    Waiting here is the point: a caller that polls for itself spends a round trip
    per attempt to learn one fact, while this returns the moment the window
    appears.
    """
    budget_ms = timeout_ms if timeout_ms > 0 else DEFAULT_WAIT_MS
    windows, waited_s = wait.poll_until(
        kwin.windows,
        lambda found: any(_window_matches(w, app=app, text=wait_for) for w in found),
        budget_ms / 1000.0,
    )
    if not any(_window_matches(w, app=app, text=wait_for) for w in windows):
        raise BridgeError(
            f"no window matching {wait_for!r} appeared within {budget_ms} ms", "WINDOW_TIMEOUT"
        )
    return waited_s


@op("window.list")
def op_window_list(args: dict[str, Any]) -> dict[str, Any]:
    """List windows, optionally filtered, each enriched with its flatpak app id.

    ``waitFor`` holds the call open until a window matches the filters, so
    waiting for an application to start is one call instead of a poll loop.
    """
    app = _str(args, "app")
    caption = _str(args, "caption")
    wait_for = _str(args, "waitFor")
    timeout_ms = _int(args, "timeoutMs", 0)
    include_popups = _bool(args, "includePopups", True)
    include_special = _bool(args, "includeSpecial", False)

    def wanted(window: dict[str, Any]) -> bool:
        if not include_popups and window["kind"] == "popup":
            return False
        if not include_special and window["kind"] in ("desktop", "dock", "notification"):
            return False
        return _window_matches(window, app=app, caption=caption, text=wait_for)

    waited_s = 0.0
    if wait_for:
        waited_s = _await_window(wait_for, app, timeout_ms)
    windows = kwin.windows()
    selected: list[dict[str, Any]] = []
    for window in windows:
        if not wanted(window):
            continue
        enriched = dict(window)
        enriched["flatpakAppId"] = flatpak.app_id_for_pid(int(window["pid"]))
        selected.append(enriched)
    try:
        topology = kwin.outputs()
    except BridgeError:
        topology = None
    result = {
        "windows": selected,
        "count": len(selected),
        "totalWindows": len(windows),
        "workspace": topology["virtual"] if topology else None,
        "outputs": topology["screens"] if topology else None,
        "activeWindowId": next((w["id"] for w in windows if w["active"]), None),
    }
    if wait_for:
        result["waitedMs"] = wait.millis(waited_s)
    return result


@op("window.control")
def op_window_control(args: dict[str, Any]) -> dict[str, Any]:
    """Activate, close, minimize, maximize, fullscreen, or move one window."""
    action = _str(args, "action")
    supported = {
        "activate", "close", "minimize", "maximize", "fullscreen",
        "keep_above", "on_all_desktops", "geometry", "info",
    }
    if action not in supported:
        raise BridgeError(
            f"unknown window action {action!r}; expected one of {', '.join(sorted(supported))}",
            "BAD_ARGUMENT",
        )
    window_id = _str(args, "windowId")
    app = _str(args, "app")
    if action == "info":
        window = kwin.require_window(window_id=window_id, app=app)
        enriched = dict(window)
        enriched["flatpakAppId"] = flatpak.app_id_for_pid(int(window["pid"]))
        return {"window": enriched, "action": "info"}
    value: bool | None = None
    if action in ("minimize", "maximize", "fullscreen", "keep_above", "on_all_desktops") and "value" in args:
        value = _bool(args, "value", True)
    return kwin.window_action(
        action,
        window_id=window_id,
        app=app,
        value=value,
        x=args.get("x"),
        y=args.get("y"),
        width=args.get("width"),
        height=args.get("height"),
    )


@op("pointer.position")
def op_pointer_position(args: dict[str, Any]) -> dict[str, Any]:
    """The pointer's global logical position."""
    return {"position": kwin.cursor_position()}


# --- screenshots -------------------------------------------------------------


@op("screenshot")
def op_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    """Capture the workspace, one window, or one area.

    ``waitFor`` waits for a matching window before capturing anything, which is
    the "screenshot the app once it is up" call: it cannot capture the wrong
    frame, and it costs one call rather than a poll loop.
    """
    target = _str(args, "target", "workspace")
    wait_for = _str(args, "waitFor")
    if wait_for:
        _await_window(wait_for, _str(args, "app"), _int(args, "timeoutMs", 0))
    common = {
        "save_to": args.get("saveTo"),
        "max_edge": _int(args, "maxEdge", 0),
        "include_cursor": _bool(args, "includeCursor"),
        "backend": _str(args, "backend", "auto"),
        "refresh_authorization": _bool(args, "refreshAuthorization"),
    }
    if target == "workspace":
        return shot.capture_workspace(**common)
    if target == "window":
        return shot.capture_window(
            window_id=_str(args, "windowId"), app=_str(args, "app"), **common
        )
    if target == "area":
        return shot.capture_area(
            x=_int(args, "x"),
            y=_int(args, "y"),
            width=_int(args, "width"),
            height=_int(args, "height"),
            **common,
        )
    raise BridgeError(f"unknown screenshot target {target!r}", "BAD_ARGUMENT")


# --- input -------------------------------------------------------------------


def _correct_pointer(session: eis.Input, x: int, y: int, tolerance: int = 0) -> dict[str, Any]:
    """Re-issue an absolute move when the pointer did not land where it was asked.

    A wake from display power saving, or the very first motion after an EIS
    handshake, can swallow a motion. Reading the position back and repeating the
    move is cheap and makes every coordinate the model is given trustworthy.
    """
    actual = kwin.cursor_position()
    if abs(actual["x"] - x) > tolerance or abs(actual["y"] - y) > tolerance:
        session.move(x, y)
        actual = kwin.cursor_position()
    return actual


@op("mouse")
def op_mouse(args: dict[str, Any]) -> dict[str, Any]:
    """Move, click, drag, or scroll the pointer."""
    action = _str(args, "action", "click")
    session = eis.input_session()
    before = kwin.cursor_position()
    if action == "position":
        return {"position": before, "action": action}
    if action == "move":
        session.move(_int(args, "x"), _int(args, "y"))
        # The compositor can coalesce the first motion after the display wakes or
        # after a fresh handshake, and every caller depends on landing exactly
        # where it asked. The position is read anyway for the reply, so a
        # correction costs one extra event when the landing was already right.
        _correct_pointer(session, _int(args, "x"), _int(args, "y"))
    elif action in ("click", "double_click", "triple_click"):
        clicks = {"click": 1, "double_click": 2, "triple_click": 3}[action]
        session.click(
            _int(args, "x"),
            _int(args, "y"),
            button=_str(args, "button", "left"),
            clicks=_int(args, "clicks", clicks),
            hold_ms=_int(args, "holdMs", 0),
        )
    elif action == "down":
        session.button_down(
            _int(args, "x"), _int(args, "y"), button=_str(args, "button", "left")
        )
    elif action == "up":
        session.button_up(
            _int(args, "x"), _int(args, "y"), button=_str(args, "button", "left")
        )
    elif action == "drag":
        session.drag(
            _int(args, "x"),
            _int(args, "y"),
            to_x=_int(args, "toX"),
            to_y=_int(args, "toY"),
            button=_str(args, "button", "left"),
            steps=_int(args, "steps", 20),
        )
    elif action == "scroll":
        session.scroll(
            _int(args, "x"),
            _int(args, "y"),
            amount=_int(args, "amount"),
            horizontal=_bool(args, "horizontal"),
            discrete=_bool(args, "discrete", True),
        )
    else:
        raise BridgeError(
            f"unknown mouse action {action!r}; expected move, click, double_click, "
            "triple_click, down, up, drag, scroll, or position",
            "BAD_ARGUMENT",
        )
    return {"action": action, "positionBefore": before, "position": kwin.cursor_position()}


@op("keyboard")
def op_keyboard(args: dict[str, Any]) -> dict[str, Any]:
    """Type text or press key chords."""
    action = _str(args, "action", "type")
    session = eis.input_session()
    if action == "type":
        text = _str(args, "text")
        if text == "":
            raise BridgeError("keyboard type needs text", "BAD_ARGUMENT")
        unicode_requested = _bool(args, "unicode")
        if not unicode_requested:
            try:
                session.type_plan(keys.text_plan(text))
                return {"action": action, "method": "keys", "characters": len(text)}
            except BridgeError as exc:
                if exc.code != "NOT_TYPEABLE":
                    raise
        return _type_via_clipboard(session, text)
    chord = _str(args, "keys")
    modifiers, main = keys.parse_chord(chord)
    if action == "press":
        session.press_chord(modifiers, main)
    elif action == "down":
        session.key_down(modifiers, main)
    elif action == "up":
        session.key_up(modifiers, main)
    else:
        raise BridgeError(
            f"unknown keyboard action {action!r}; expected type, press, down, or up",
            "BAD_ARGUMENT",
        )
    return {"action": action, "method": "keys", "keys": chord}


def _type_via_clipboard(session: eis.Input, text: str) -> dict[str, Any]:
    """Type text by placing it on the clipboard and pressing the paste chord.

    Unicode cannot be injected through a US-QWERTY evdev keymap, so this is the
    only general path. The previous clipboard is restored afterwards, and a
    target whose paste chord is unknown is refused rather than risk injecting a
    literal ``^V`` into a shell.
    """
    window = kwin.active_window()
    resource = window["app"] if window else None
    chord = keys.paste_chord(resource)
    if chord is None:
        raise BridgeError(
            f"cannot type unicode into {resource!r}: no known paste chord for it "
            "(a terminal that does not bind Ctrl+V would receive a literal ^V)",
            "NOT_TYPEABLE",
            {"target": resource},
        )
    previous: str | None = None
    try:
        previous = clipboard.read()
    except BridgeError:
        previous = None
    clipboard.write(text)
    time.sleep(0.15)
    modifiers, main = keys.parse_chord(chord)
    session.press_chord(modifiers, main)
    time.sleep(0.45)
    restored = False
    if previous is not None:
        clipboard.write(previous)
        restored = True
    return {
        "action": "type",
        "method": "clipboard",
        "chord": chord,
        "target": resource,
        "clipboardRestored": restored,
        "characters": len(text),
    }


@op("clipboard")
def op_clipboard(args: dict[str, Any]) -> dict[str, Any]:
    """Read or write the clipboard."""
    action = _str(args, "action", "read")
    if action == "read":
        return {"action": action, "text": clipboard.read()}
    if action == "write":
        text = _str(args, "text")
        clipboard.write(text)
        return {"action": action, "characters": len(text)}
    raise BridgeError(f"unknown clipboard action {action!r}", "BAD_ARGUMENT")


# --- accessibility -----------------------------------------------------------


@op("a11y.tree")
def op_a11y_tree(args: dict[str, Any]) -> dict[str, Any]:
    """Walk the accessibility tree, with global coordinates where provable."""
    return atspi.tree(
        app_name=_str(args, "app"),
        window_id=_str(args, "windowId"),
        max_depth=_int(args, "maxDepth", 15),
        role=_str(args, "role"),
        max_elements=_int(args, "maxElements", 400),
        ensure_enabled=_bool(args, "ensureEnabled", True),
    )


@op("a11y.find")
def op_a11y_find(args: dict[str, Any]) -> dict[str, Any]:
    """Find accessibility elements matching a query, optionally waiting for one."""
    states = args.get("states")
    if states is not None and not isinstance(states, list):
        raise BridgeError("states must be a list of state names", "BAD_ARGUMENT")
    return atspi.find(
        query=_str(args, "query"),
        app_name=_str(args, "app"),
        window_id=_str(args, "windowId"),
        states=[str(state) for state in states] if states else None,
        wait_ms=_int(args, "waitMs", 0),
        poll_ms=_int(args, "pollMs", 200),
        max_results=_int(args, "maxResults", 50),
        ensure_enabled=_bool(args, "ensureEnabled", True),
    )


@op("a11y.invoke")
def op_a11y_invoke(args: dict[str, Any]) -> dict[str, Any]:
    """Run one accessibility action on exactly one matching element."""
    index = args.get("index")
    return atspi.invoke(
        query=_str(args, "query"),
        app_name=_str(args, "app"),
        window_id=_str(args, "windowId"),
        action_name=_str(args, "action"),
        index=None if index is None else int(index),
        ensure_enabled=_bool(args, "ensureEnabled", True),
    )


@op("a11y.windows")
def op_a11y_windows(args: dict[str, Any]) -> dict[str, Any]:
    """List the applications and top-level windows AT-SPI knows about."""
    return atspi.list_windows()


@op("a11y.enable")
def op_a11y_enable(args: dict[str, Any]) -> dict[str, Any]:
    """Turn toolkit accessibility on or off."""
    return atspi.set_enabled(_bool(args, "enabled", True))


# --- launching ---------------------------------------------------------------


def _descendants(pid: int) -> set[int]:
    """Every transitive child of ``pid``, from ``/proc``.

    A launched app rarely owns its window itself — a wrapper script, ``flatpak
    run``, or a launcher process does — so window matching has to consider the
    whole process subtree, not just the pid that was spawned.
    """
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
        except OSError:
            continue
        # The comm field may contain spaces and parentheses; everything after
        # the final ')' is positional.
        try:
            fields = stat.rsplit(")", 1)[1].split()
        except IndexError:
            continue
        if len(fields) < 2:
            continue
        parents[int(entry)] = int(fields[1])
    found: set[int] = set()
    frontier = {pid}
    while frontier:
        children = {
            child for child, parent in parents.items()
            if parent in frontier and child not in found
        }
        found |= children
        frontier = children
    return found


def _app_window(app_id: str = "", pids: set[int] | None = None, match: str = "") -> dict[str, Any] | None:
    """The window that already belongs to an app, or None.

    Used before and after a launch: ``flatpak run`` on an app that is already
    running brings the existing instance forward instead of opening a second
    window, so waiting only for a *new* window would time out on a healthy app.
    """
    needle = match.lower()
    for window in kwin.windows():
        if window["kind"] not in ("normal", "other"):
            continue
        if app_id and flatpak.app_id_for_pid(int(window["pid"])) == app_id:
            return window
        if pids and int(window["pid"]) in pids:
            return window
        if needle and needle in f"{window['app']} {window['caption']}".lower():
            return window
    return None


def _wait_for_window(
    before_ids: set[str],
    *,
    app_id: str = "",
    pids: set[int] | None = None,
    match: str = "",
    timeout_ms: int = 15000,
) -> dict[str, Any] | None:
    """Poll KWin for the window a freshly launched app owns."""
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    while True:
        for window in kwin.windows():
            if window["id"] in before_ids:
                continue
            if app_id and flatpak.app_id_for_pid(int(window["pid"])) == app_id:
                return window
            if pids and int(window["pid"]) in pids:
                return window
            if match and match.lower() in f"{window['app']} {window['caption']}".lower():
                return window
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


@op("app.launch")
def op_app_launch(args: dict[str, Any]) -> dict[str, Any]:
    """Launch a flatpak app or a host command, then wait for its window.

    Waiting is part of the operation on purpose: an agent that has to poll for
    the window itself will guess, and the guess is usually an activation of
    whatever was already focused. For a flatpak app the window is matched by the
    instance's cgroup scope, which is exact even when several instances of the
    same app are running.
    """
    app_id = _str(args, "appId")
    command = _str(args, "command")
    extra_args = [str(item) for item in args.get("args") or []]
    env = {str(key): str(value) for key, value in (args.get("env") or {}).items()}
    wait_ms = _int(args, "waitMs", 15000)
    match = _str(args, "windowMatch")
    if not app_id and not command:
        raise BridgeError("app.launch needs appId (flatpak) or command (host)", "BAD_ARGUMENT")

    before_ids = {window["id"] for window in kwin.windows()}
    pids: set[int] = set()
    launch: dict[str, Any]
    if app_id:
        launch = flatpak.launch(
            app_id,
            command=_str(args, "flatpakCommand") or None,
            args=extra_args,
            branch=_str(args, "branch") or None,
            env=env,
        )
        # The sandbox's own environment owns accessibility; a Qt app inside it
        # only publishes a tree when this is set.
        pids = set()
    else:
        argv = [command, *extra_args]
        log_path = flatpak._log_path(command.replace("/", "_"))  # noqa: SLF001 - shared log location
        try:
            log = log_path.open("a", encoding="utf-8")
        except OSError as exc:
            raise BridgeError(f"cannot open {log_path} for the app log: {exc}", "LOG_UNAVAILABLE") from exc
        try:
            process = subprocess.Popen(  # noqa: S603 - argv comes from the caller, never a shell
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, **env},
            )
        except FileNotFoundError as exc:
            log.close()
            raise BridgeError(f"command not found: {command}", "COMMAND_NOT_FOUND") from exc
        finally:
            log.close()
        launch = {"argv": argv, "launcherPid": process.pid, "logPath": str(log_path)}
        pids = {process.pid} | _descendants(process.pid)

    window = _wait_for_window(
        before_ids,
        app_id=app_id,
        pids=pids if not app_id else None,
        match=match,
        timeout_ms=wait_ms,
    )
    already_running = False
    if window is None:
        # A second `flatpak run` of a running app activates it rather than
        # opening a window, and a host program may have a window already.
        window = _app_window(app_id, pids if not app_id else None, match)
        already_running = window is not None
    result = dict(launch)
    if window is not None:
        # Enrich the window the same way cu_windows does, so a caller that only
        # has the launch result still knows which sandbox it landed in.
        window = dict(window)
        window["flatpakAppId"] = flatpak.app_id_for_pid(int(window["pid"]))
    result["window"] = window
    result["flatpakAppId"] = app_id or (window["flatpakAppId"] if window else None)
    result["alreadyRunning"] = already_running
    if window is None:
        result["note"] = (
            f"no window appeared within {wait_ms}ms; the app may still be starting "
            f"(log: {launch.get('logPath')})"
        )
    elif already_running:
        result["note"] = (
            "the app was already running, so its existing window is returned "
            "(a second launch brings that instance forward instead of opening a new window)"
        )
    return result


@op("flatpak.info")
def op_flatpak_info(args: dict[str, Any]) -> dict[str, Any]:
    """Installed apps, running instances, and one app's permissions when asked."""
    app_id = _str(args, "appId")
    if app_id:
        detail = flatpak.describe(app_id)
        accessibility = atspi.bus_state()
        # Whether the app actually publishes a tree is the answer that matters:
        # flatpak allows the accessibility bus by default, so a missing
        # declaration in the app's own permissions proves nothing.
        accessibility["appearsOnA11yBus"] = False
        accessibility["treePublishers"] = []
        try:
            applications = atspi.list_windows()["applications"]
            accessibility["appearsOnA11yBus"] = any(a["flatpakAppId"] == app_id for a in applications)
            accessibility["treePublishers"] = [
                a["name"] for a in applications if a["flatpakAppId"] == app_id
            ]
        except BridgeError as exc:
            accessibility["probeError"] = exc.as_payload()
        detail["accessibility"] = accessibility
        return detail
    return {
        "apps": flatpak.installed_apps(),
        "instances": flatpak.running_instances(),
        "accessibility": atspi.bus_state(),
    }


@op("shutdown")
def op_shutdown(args: dict[str, Any]) -> dict[str, Any]:
    """Close the EIS connection and let the serve loop exit."""
    eis.shutdown()
    return {"stopping": True}


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    """Run one request object and return its response frame.

    The frame carries its own cost as ``elapsedMs``: a caller that wonders where
    a session's wall clock went can read it per operation, and one that does not
    ignores it.
    """
    request_id = request.get("id")
    name = request.get("op")
    handler = OPS.get(str(name))
    if handler is None:
        return rpc.failure(request_id, BridgeError(f"unknown op {name!r}", "UNKNOWN_OP"))
    args = request.get("args") or {}
    if not isinstance(args, dict):
        return rpc.failure(request_id, BridgeError("args must be an object", "BAD_ARGUMENT"))
    started = time.monotonic()
    try:
        frame = rpc.ok(request_id, handler(args))
    except BaseException as exc:  # noqa: BLE001 - every failure is a frame, never a crash
        frame = rpc.failure(request_id, exc)
    frame["elapsedMs"] = wait.millis(time.monotonic() - started)
    return frame


def serve() -> int:
    """Answer newline-delimited requests until stdin closes or shutdown arrives."""
    import warnings

    warnings.filterwarnings("ignore", message=r"Atspi\.Action\.get_action_name is deprecated")
    # Keep the response stream private: anything a library prints to stdout
    # would otherwise be interleaved into the frame stream.
    responses = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        stripped = line.strip()
        if stripped == "":
            continue
        try:
            request = json.loads(stripped)
        except json.JSONDecodeError as exc:
            rpc.emit(rpc.failure(None, BridgeError(f"invalid JSON request: {exc}", "BAD_REQUEST")), responses)
            continue
        if not isinstance(request, dict):
            rpc.emit(rpc.failure(None, BridgeError("a request must be a JSON object", "BAD_REQUEST")), responses)
            continue
        frame = dispatch(request)
        rpc.emit(frame, responses)
        if frame.get("ok") and isinstance(frame.get("result"), dict) and frame["result"].get("stopping"):
            return 0
    return 0


def main(argv: list[str]) -> int:
    """Run the serve loop, one operation, or a status dump."""
    if not argv or argv[0] == "--serve":
        return serve()
    if argv[0] == "--detect":
        rpc.emit(rpc.ok(None, status.probe()))
        return 0
    if argv[0] == "--ops":
        rpc.emit(rpc.ok(None, sorted(OPS)))
        return 0
    name = argv[0]
    payload = argv[1] if len(argv) > 1 else "{}"
    try:
        args = json.loads(payload)
    except json.JSONDecodeError as exc:
        rpc.emit(rpc.failure(None, BridgeError(f"arguments are not JSON: {exc}", "BAD_ARGUMENT")))
        return 2
    rpc.emit(dispatch({"id": 1, "op": name, "args": args}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
