"""Flatpak identity, permissions, and launch.

Flatpak matters for computer use in three separate ways, and each one is
handled here rather than being left to the caller to discover:

* **Identity.** A sandboxed app's accessibility tree is published by the
  sandbox's ``xdg-dbus-proxy``, so AT-SPI reports the proxy's pid while KWin
  reports the app's pid. Both live in the instance's own
  ``app-flatpak-<app-id>-<n>.scope`` cgroup, which is what lets us map one to
  the other (see ``atspi.py``).
* **Reachability.** An app only appears on the accessibility bus when the
  sandbox is allowed to talk to ``org.a11y.Bus`` and toolkit accessibility is
  enabled session-wide. ``permissions()`` reads what the app was granted so a
  failure can be explained instead of guessed at.
* **Launching.** ``flatpak run`` is how an app is started, and it is also the
  only place where the app id, branch, and per-launch environment are known.

Part of dsh-cu. Portions are derived from kwin-mcp's ``accessibility.py``
(MIT, Copyright (c) 2026 Byeonghoon Yoo) — see NOTICE.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import dbus

from . import session
from .rpc import BridgeError

PROC = Path("/proc")

# Flatpak runs each app instance in its own systemd scope, named
# ``app-flatpak-<app-id>-<instance>.scope``. The app id itself may contain
# dashes, so the trailing ``-<digits>`` is stripped from the right.
FLATPAK_SCOPE = re.compile(r"app-flatpak-.+-\d+\.scope")
_SCOPE_PREFIX = "app-flatpak-"
_SCOPE_SUFFIX = re.compile(r"-\d+\.scope$")

# Identity of the sandbox's proxy process on the accessibility bus.
DBUS_PROXY_COMM = "xdg-dbus-proxy"

FLATPAK_TIMEOUT_S = 15.0

# ``flatpak ps`` resolves pids through cgroupfs, which a caller in another PID
# namespace can still read even though /proc does not list those pids. That
# makes it the fallback identity source; it is cached briefly because a window
# list enriches many pids at once.
_PS_CACHE_S = 2.0
_ps_cache: tuple[float, dict[int, str]] | None = None
# pid -> (probe time, app id) for the systemd identity fallback.
_pid_unit_cache: dict[int, tuple[float, str | None]] = {}


def _run(argv: list[str], timeout: float = FLATPAK_TIMEOUT_S) -> str:
    """Run a flatpak CLI command and return stdout, mapping failures to errors."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BridgeError(
            "the flatpak CLI is not installed",
            "FLATPAK_UNAVAILABLE",
            {"argv": argv},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            f"`{' '.join(argv)}` did not finish within {timeout:g}s",
            "FLATPAK_TIMEOUT",
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise BridgeError(
            f"`{' '.join(argv)}` failed (exit {completed.returncode}): {detail[:400]}",
            "FLATPAK_COMMAND_FAILED",
        )
    return completed.stdout


def cgroup_scope(pid: int) -> str | None:
    """The cgroup-v2 path of ``pid``, or None when it cannot be read.

    A pid from another process namespace has no ``/proc`` entry here; that is
    indistinguishable from a vanished process and is reported as None.
    """
    try:
        lines = (PROC / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            return line[3:]
    return None


def comm_of(pid: int) -> str | None:
    """The short command name of ``pid``, or None when unreadable."""
    try:
        return (PROC / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def scope_unit(scope: str | None) -> str | None:
    """The last path segment of a cgroup path when it is a flatpak app scope."""
    if scope is None:
        return None
    unit = scope.rsplit("/", 1)[-1]
    return unit if FLATPAK_SCOPE.fullmatch(unit) else None


def app_id_from_scope(scope: str | None) -> str | None:
    """Extract ``org.example.App`` from a flatpak instance scope path."""
    unit = scope_unit(scope)
    if unit is None or not unit.startswith(_SCOPE_PREFIX):
        return None
    body = _SCOPE_SUFFIX.sub("", unit[len(_SCOPE_PREFIX):])
    return body or None


def instance_from_scope(scope: str | None) -> str | None:
    """The instance id (app id plus trailing number) carried by a scope path."""
    unit = scope_unit(scope)
    if unit is None:
        return None
    return unit[len(_SCOPE_PREFIX):-len(".scope")] or None


def app_id_for_pid(pid: int) -> str | None:
    """The flatpak app id owning ``pid``.

    Three sources, in order of directness, because no single one works
    everywhere:

    1. ``/proc/<pid>/cgroup`` — exact, one file read, and it resolves *any*
       process in the sandbox including ``xdg-dbus-proxy``. It cannot see another
       PID namespace's processes.
    2. the user systemd manager's ``GetUnitByPID`` — systemd resolves the number
       on the host side and answers with the unit, which for a sandboxed app is
       its ``app-flatpak-<app-id>-<n>.scope``. This is the source that survives
       being sandboxed: cgroupfs reports a foreign pid as 0 from inside a PID
       namespace, so reading it directly is not an option.
    3. ``flatpak ps`` — the instance table, which covers instance main pids when
       systemd has no unit loaded for them.
    """
    app_id = app_id_from_scope(cgroup_scope(pid))
    if app_id:
        return app_id
    app_id = app_id_from_systemd(pid)
    if app_id:
        return app_id
    return instance_map().get(int(pid))


# systemd escapes a unit name into a path: '-' -> '_2d', '.' -> '_2e', '_' -> '_5f'.
_SYSTEMD_UNIT_PREFIX = "app_2dflatpak_2d"
_SYSTEMD_UNIT_SUFFIX = "_2escope"


def _unescape_unit_name(escaped: str) -> str:
    """Undo systemd's unit-name escaping."""
    return escaped.replace("_2d", "-").replace("_2e", ".").replace("_5f", "_")


def app_id_from_systemd_unit(unit: str) -> str | None:
    """Extract an app id from a systemd unit path or name, or None.

    Accepts either the object path systemd returns
    (``/org/freedesktop/systemd1/unit/app_2dflatpak_2d…_2escope``) or a bare unit
    name, so a caller holding either form can use it.
    """
    name = unit.strip().rsplit("/", 1)[-1]
    if not name.startswith(_SYSTEMD_UNIT_PREFIX) or not name.endswith(_SYSTEMD_UNIT_SUFFIX):
        return None
    body = name[len(_SYSTEMD_UNIT_PREFIX):-len(_SYSTEMD_UNIT_SUFFIX)]
    app_id, _, instance = body.rpartition("_2d")
    if not app_id or not instance.isdigit():
        return None
    return _unescape_unit_name(app_id)


def app_id_from_systemd(pid: int) -> str | None:
    """Resolve a pid to its flatpak app through the user systemd manager.

    This is the identity path that works from inside a PID namespace, where
    ``/proc`` has no entry for the pid and cgroupfs reports it as 0: the lookup
    happens inside systemd, which lives in the host namespace.
    """
    now = time.monotonic()
    cached = _pid_unit_cache.get(int(pid))
    if cached is not None and now - cached[0] < _PS_CACHE_S:
        return cached[1]
    resolved: str | None = None
    try:
        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        bus = dbus.bus.BusConnection(address) if address else dbus.SessionBus()
        manager = bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
        unit = manager.GetUnitByPID(
            dbus.UInt32(int(pid)), dbus_interface="org.freedesktop.systemd1.Manager"
        )
        resolved = app_id_from_systemd_unit(str(unit))
    except dbus.DBusException:
        # "PID does not belong to any loaded unit" is the common case for an
        # ordinary application, not a failure worth surfacing.
        resolved = None
    except Exception:  # noqa: BLE001 - identity lookup must never break a listing
        resolved = None
    _pid_unit_cache[int(pid)] = (now, resolved)
    return resolved


def instance_map() -> dict[int, str]:
    """Host pid to flatpak app id for every running instance, cached briefly."""
    global _ps_cache
    now = time.monotonic()
    if _ps_cache is not None and now - _ps_cache[0] < _PS_CACHE_S:
        return _ps_cache[1]
    mapping: dict[int, str] = {}
    try:
        for instance in running_instances():
            mapping[int(instance["pid"])] = str(instance["appId"])
    except BridgeError:
        mapping = {}
    _ps_cache = (now, mapping)
    return mapping


def pids_for_app(app_id: str, pids: list[int] | None = None) -> list[int]:
    """Every known pid belonging to ``app_id``.

    ``pids`` narrows the answer to candidates the caller already has (for
    example the pids of a KWin window list); without it the flatpak instance
    table is used.
    """
    if pids is not None:
        return [pid for pid in pids if app_id_for_pid(pid) == app_id]
    return [pid for pid, owner in instance_map().items() if owner == app_id]


def running_instances() -> list[dict[str, Any]]:
    """Every running flatpak app instance, with its host pid and app id.

    ``scope`` is the instance's systemd unit name, built from the id flatpak
    reports. Reading it out of ``/proc`` instead would be wrong for an instance
    launched from a terminal: ``flatpak ps`` reports the ``flatpak run`` process,
    which still lives in the caller's scope, while the sandbox's own processes
    are the ones under ``app-flatpak-<app-id>-<n>.scope``.
    """
    output = _run(["flatpak", "ps", "--columns=instance,pid,application"])
    instances: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        instances.append({
            "instance": parts[0],
            "pid": int(parts[1]),
            "appId": parts[2],
            "scope": f"{_SCOPE_PREFIX}{parts[2]}-{parts[0]}.scope",
        })
    return instances


def installed_apps() -> list[dict[str, Any]]:
    """Every installed flatpak app, with version and branch."""
    output = _run(["flatpak", "list", "--app", "--columns=application,name,version,branch"])
    apps: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if not parts or parts[0] == "":
            continue
        apps.append({
            "appId": parts[0],
            "name": parts[1] if len(parts) > 1 else "",
            "version": parts[2] if len(parts) > 2 else "",
            "branch": parts[3] if len(parts) > 3 else "",
        })
    return apps


def permissions(app_id: str) -> dict[str, Any]:
    """Parse ``flatpak info --show-permissions`` into sections.

    The reply is an INI-like document; sections are returned verbatim so an
    unexpected one is visible rather than dropped.
    """
    output = _run(["flatpak", "info", "--show-permissions", app_id])
    sections: dict[str, Any] = {}
    current = "defaults"
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections[current] = {} if current == "Context" else []
            continue
        if stripped == "":
            continue
        target = sections.get(current)
        if isinstance(target, dict):
            if "=" in stripped:
                key, value = stripped.split("=", 1)
                target[key.strip()] = value.strip()
        elif isinstance(target, list):
            target.append(stripped)
    session_bus = sections.get("Session Bus Policy") or []
    talk_names = {
        entry.split("=", 1)[0].strip()
        for entry in session_bus
        if isinstance(entry, str) and "=" in entry
    }
    context = sections.get("Context") or {}
    sockets = str(context.get("sockets", ""))
    return {
        "sections": sections,
        "sessionBusTalk": sorted(talk_names),
        # What the app *declares*. Flatpak grants the accessibility bus by
        # default, so an app with no declaration here still publishes a tree
        # (Brave does), and an app that declares it still may not register.
        # The authoritative answer is whether it shows up in cu_a11y_windows.
        "a11yBusDeclared": "org.a11y.Bus" in talk_names,
        "sockets": sockets,
        "hasWayland": "wayland" in sockets,
        "hasX11": "x11" in sockets or "fallback-x11" in sockets,
    }


def app_id_by_name(name: str) -> str | None:
    """Resolve a human-readable app name to its app id when it is unambiguous.

    Only used for reporting and for improving a failure message: a name is not
    an identity, and two installed apps can share one.
    """
    needle = name.strip().lower()
    if needle == "":
        return None
    matches = [
        app["appId"] for app in safe_apps()
        if needle == str(app["name"]).lower() or needle in str(app["name"]).lower()
    ]
    return matches[0] if len(matches) == 1 else None


def describe(app_id: str) -> dict[str, Any]:
    """Everything about one app that computer use depends on."""
    result: dict[str, Any] = {"appId": app_id}
    try:
        result["permissions"] = permissions(app_id)
    except BridgeError as exc:
        result["permissions"] = {"error": exc.as_payload()}
    try:
        instances = [i for i in running_instances() if i["appId"] == app_id]
    except BridgeError as exc:
        instances = []
        result["instancesError"] = exc.as_payload()
    result["instances"] = instances
    result["installed"] = any(app["appId"] == app_id for app in safe_apps())
    return result


def safe_apps() -> list[dict[str, Any]]:
    """Installed apps, or an empty list when the flatpak CLI is unusable."""
    try:
        return installed_apps()
    except BridgeError:
        return []


def launch(
    app_id: str,
    *,
    command: str | None = None,
    args: list[str] | None = None,
    branch: str | None = None,
    env: dict[str, str] | None = None,
    extra_flatpak_args: list[str] | None = None,
) -> dict[str, Any]:
    """Start a flatpak app detached and return its argv, pid, and log path.

    ``flatpak run`` returns as soon as the sandbox is started, and the app's
    real host pid belongs to the instance scope rather than to that child, so
    callers should resolve the app's windows by app id (see ``atoms()`` in
    ``__main__.py``) instead of trusting the pid returned here.
    """
    argv: list[str] = ["flatpak", "run"]
    if branch:
        argv.append(f"--branch={branch}")
    if command:
        argv.append(f"--command={command}")
    for key, value in (env or {}).items():
        argv.append(f"--env={key}={value}")
    argv.extend(extra_flatpak_args or [])
    argv.append(app_id)
    argv.extend(args or [])

    log_path = _log_path(app_id)
    try:
        log = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        raise BridgeError(f"cannot open {log_path} for the app log: {exc}", "LOG_UNAVAILABLE") from exc
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is built here, never a shell string
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        log.close()
        raise BridgeError("the flatpak CLI is not installed", "FLATPAK_UNAVAILABLE") from exc
    finally:
        log.close()
    return {
        "argv": argv,
        "launcherPid": process.pid,
        "logPath": str(log_path),
    }


def _log_path(app_id: str) -> Path:
    """The append-only log file used for one launched app."""
    directory = session.state_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{app_id}.log"
