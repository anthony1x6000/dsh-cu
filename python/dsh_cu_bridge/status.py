"""Capability probe: what this host can actually do, before anything fails.

Computer use on Plasma depends on several independent things — a KWin session, a
Wayland socket, libei plus KWin's EIS endpoint for input, an authorized or
fallback screenshot path, an enabled accessibility bus, and flatpak for sandboxed
apps. Every one of them can be missing while the rest works, so the plugin
answers with a capability report and an actionable hint per degraded part rather
than failing one operation at a time.

Part of dsh-cu.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import atspi, clipboard, eis, flatpak, kwin, session, shot


def _version(argv: list[str], timeout: float = 5.0) -> str | None:
    """First non-empty output line of a command that is known to exit quickly.

    Only ``--version`` implementations that terminate are probed here:
    ``plasmashell --version`` blocks trying to open a QApplication, so Plasma's
    version comes from the package manager instead.
    """
    binary = shutil.which(argv[0])
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    candidates = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not candidates and completed.returncode == 0:
        # Some tools report their version on stderr; a *failed* command only
        # reports its own error there, which is never a version.
        candidates = [
            line.strip() for line in completed.stderr.splitlines()
            if line.strip() and "libEGL" not in line and "MESA" not in line
        ]
    for line in candidates:
        if not line.startswith(f"{Path(binary).name}:"):
            return line
    return None


def _package_version(*names: str) -> str | None:
    """Installed version of a package, via the distro's package manager."""
    for name in names:
        if shutil.which("dpkg-query"):
            value = _version(["dpkg-query", "-W", "-f=${Version}", name])
            if value:
                return value
        if shutil.which("rpm"):
            value = _version(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", name])
            if value:
                return value
    return None


def _distro() -> str | None:
    """The distro's pretty name, from os-release."""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def _self_report() -> dict[str, Any]:
    """Identity of this bridge process, including the cgroup that names its scope."""
    return {
        "pid": os.getpid(),
        "executable": os.path.realpath("/proc/self/exe") if Path("/proc/self/exe").exists() else None,
        "cgroup": flatpak.cgroup_scope(os.getpid()),
        "dbusSessionBus": os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
        "waylandDisplay": os.environ.get("WAYLAND_DISPLAY"),
        "x11Display": os.environ.get("DISPLAY"),
        "xdgSessionType": os.environ.get("XDG_SESSION_TYPE"),
        "xdgCurrentDesktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "scriptDir": os.environ.get("DSH_CU_SCRIPT_DIR"),
        "screenshotDir": str(shot._screenshot_dir()),  # noqa: SLF001 - the report states the real default
    }


def probe(refresh_screenshot_authorization: bool = False) -> dict[str, Any]:
    """Report every capability computer use depends on, with hints when degraded."""
    hints: list[str] = []

    session_report = session.describe()
    if session_report.get("locked"):
        hints.append(
            "The session is locked: screenshots show the lock screen and synthetic input lands on "
            "it, not on the user's windows. Unlock the session before driving applications."
        )

    kwin_report: dict[str, Any] = {"available": False}
    try:
        topology = kwin.outputs()
        kwin_report = {
            "available": True,
            "distro": _distro(),
            "plasmaVersion": _package_version("plasma-desktop", "plasma-workspace"),
            "kwinVersion": _version(["kwin_wayland", "--version"]) or _package_version("kwin-wayland", "kwin"),
            "outputs": topology["screens"],
            "workspace": topology["virtual"],
        }
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not raise
        kwin_report["error"] = str(exc)
        hints.append(
            "KWin is not answering org.kde.KWin over the session bus: window control, screenshots, "
            "and input all need a KDE Plasma Wayland session."
        )

    input_report = eis.probe()
    if not input_report.get("kwinEis"):
        hints.append(
            "Input injection is unavailable: KWin's EIS endpoint did not answer. "
            "Mouse and keyboard tools will fail; everything else still works."
        )
    elif not input_report.get("libei"):
        hints.append("libei is missing: install it (Debian/Ubuntu: libei1) for input injection.")

    screenshot_report: dict[str, Any] = {
        "spectacle": bool(shutil.which("spectacle")),
        "spectacleVersion": _package_version("spectacle"),
    }
    try:
        available, detail = shot.screenshot2_status(refresh=refresh_screenshot_authorization)
        screenshot_report["screenShot2"] = available
        if not available:
            screenshot_report["screenShot2Detail"] = detail
            if not screenshot_report["spectacle"]:
                hints.append("No screenshot backend: neither ScreenShot2 authorization nor Spectacle.")
    except Exception as exc:  # noqa: BLE001
        screenshot_report["screenShot2"] = False
        screenshot_report["screenShot2Detail"] = str(exc)

    accessibility_report: dict[str, Any] = {"available": False}
    try:
        state = atspi.bus_state()
        accessibility_report = {
            "available": bool(state.get("available")),
            "enabled": bool(state.get("enabled")),
            "address": state.get("address"),
        }
        if not accessibility_report["enabled"]:
            hints.append(
                "Toolkit accessibility is off (org.a11y.Status.IsEnabled=false): applications publish "
                "no widget tree until it is on. The accessibility tools enable it on use."
            )
    except Exception as exc:  # noqa: BLE001
        accessibility_report["error"] = str(exc)
        hints.append("AT-SPI2 is unavailable: install python3-gi and gir1.2-atspi-2.0 for the a11y tools.")

    try:
        flatpak_report: dict[str, Any] = {
            "available": True,
            "version": _version(["flatpak", "--version"]),
            "apps": flatpak.installed_apps(),
            "instances": flatpak.running_instances(),
        }
    except Exception as exc:  # noqa: BLE001
        flatpak_report = {"available": False, "error": str(exc)}

    return {
        "self": _self_report(),
        "session": session_report,
        "kwin": kwin_report,
        "input": input_report,
        "screenshot": screenshot_report,
        "accessibility": accessibility_report,
        "clipboard": {"available": clipboard.available()},
        "flatpak": flatpak_report,
        "tools": {
            "windowControl": kwin_report["available"],
            "screenshots": bool(screenshot_report.get("screenShot2") or screenshot_report["spectacle"]),
            "input": bool(input_report.get("kwinEis") and input_report.get("libei")),
            "accessibility": bool(accessibility_report.get("available")),
            "flatpakApps": bool(flatpak_report.get("available")),
        },
        "hints": hints,
    }
