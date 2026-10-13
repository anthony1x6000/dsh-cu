"""Session state that changes what the other tools mean.

Two things live here because every other module needs to ask them:

* **Lock state.** A locked session is the most important context for computer
  use: screenshots show the lock screen, synthetic input lands on the lock
  screen, and a launched app may never become visible.
* **Writability of the XDG directories.** A caller inside a read-only sandbox
  (the DSH bash sandbox bind-mounts the whole filesystem read-only) cannot let
  KDE applications write their configuration, and Spectacle refuses to capture
  at all in that state — it shows a modal dialog and exits 0 without writing an
  image. Knowing this up front turns a confusing failure into an explained one.

Part of dsh-cu.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
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


# --- directories -------------------------------------------------------------


def config_home() -> Path:
    """The XDG config directory an application would write to."""
    override = os.environ.get("XDG_CONFIG_HOME")
    return Path(override) if override else Path.home() / ".config"


def cache_home() -> Path:
    """The XDG cache directory an application would write to."""
    override = os.environ.get("XDG_CACHE_HOME")
    return Path(override) if override else Path.home() / ".cache"


def data_home() -> Path:
    """The XDG data directory an application would write to."""
    override = os.environ.get("XDG_DATA_HOME")
    return Path(override) if override else Path.home() / ".local" / "share"


def state_dir() -> Path:
    """Where dsh-cu keeps logs, screenshots, and its own scratch files."""
    override = os.environ.get("DSH_CU_STATE_DIR")
    base = Path(override) if override else Path.home() / ".local" / "state" / "dsh-cu"
    return base


def writable(path: Path) -> bool:
    """Whether a file can actually be created in ``path``, creating it if needed.

    ``os.access`` is not enough: inside a read-only bind mount it reports success
    for a directory that cannot be written, which is exactly the case this
    function exists to catch.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = path / ".dsh-cu-write-probe"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return True


def writability() -> dict[str, Any]:
    """Which of the directories this plugin writes through are usable."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return {
        "config": str(config_home()),
        "configWritable": writable(config_home()),
        "cacheWritable": writable(cache_home()),
        "dataWritable": writable(data_home()),
        "stateDir": str(state_dir()),
        "stateDirWritable": writable(state_dir()),
        "runtimeDirWritable": writable(Path(runtime)) if runtime else None,
        "devShmWritable": writable(Path("/dev/shm")) if Path("/dev/shm").is_dir() else False,
        "tmpDir": tempfile.gettempdir(),
    }


def sandboxed() -> bool:
    """Whether the caller looks sandboxed, judged by a read-only config directory.

    A sandbox whose filesystem is read-only is the one condition that silently
    breaks screenshot capture and (with a private ``/tmp``) KWin scripting, so it
    is the condition worth naming.
    """
    return not writable(config_home())


def describe() -> dict[str, Any]:
    """Session identity, lock state, and directory writability."""
    facts = writability()
    return {
        "sessionType": os.environ.get("XDG_SESSION_TYPE"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "waylandDisplay": os.environ.get("WAYLAND_DISPLAY"),
        "locked": locked(),
        "sandboxed": not facts["configWritable"],
        **{key: value for key, value in facts.items() if key != "config"},
    }

