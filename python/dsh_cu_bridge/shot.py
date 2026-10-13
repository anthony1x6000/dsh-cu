"""Screen captures with a verifiable logical coordinate mapping.

Two backends, tried in order:

1. **KWin ScreenShot2** (``org.kde.KWin.ScreenShot2``) — fast, per-window and
   per-area, no consent prompt. It is a *restricted* D-Bus interface: KWin only
   authorizes a caller whose executable is named by a ``.desktop`` file that
   declares ``X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2``. With
   no grant the call fails with ``Error.NoAuthorized``; ``authorize_hint()``
   reports that, and ``tools/authorize-screenshot.sh`` installs the grant.
2. **Spectacle** (``spectacle -b -n -o FILE``) — a KDE application, so it is
   already authorized. It captures the whole desktop, the active window, or the
   current monitor; a region is a crop of the full-desktop frame.

Every capture reports the mapping it can prove: the logical origin the returned
pixels start at, the logical size, and the backend. When the mapping cannot be
proven, ``reason`` says why and no coordinate should be derived from the image.

Part of dsh-cu, derived from kwin-mcp's ``screenshot.py`` (MIT,
Copyright (c) 2026 Byeonghoon Yoo) — see NOTICE.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import dbus
import dbus.bus

from . import kwin, session
from .rpc import BridgeError

# QImage::Format values KWin sends, mapped to Pillow raw modes. The *32 formats
# are little-endian integers (memory order B,G,R,A); the *8888 formats are
# byte-ordered (memory order R,G,B,X). 4=RGB32, 5=ARGB32, 6=ARGB32_Premultiplied,
# 16=RGBX8888. A missing format key is treated as 6.
RAW_FORMATS: dict[int, tuple[str, str]] = {
    4: ("RGB", "BGRX"),
    5: ("RGBA", "BGRA"),
    6: ("RGBA", "BGRA"),
    16: ("RGB", "RGBX"),
}

CAPTURE_TIMEOUT_S = 8.0
READER_POLL_S = 0.1
READER_JOIN_S = 1.0
SPECTACLE_TIMEOUT_S = 25.0

# ScreenShot2 restricted-interface error names this module can explain.
NO_AUTHORIZED = "org.kde.KWin.ScreenShot2.Error.NoAuthorized"

_authorization: tuple[bool, str] | None = None


def _bus() -> Any:
    """A session-bus connection honouring an explicit address."""
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    return dbus.bus.BusConnection(address) if address else dbus.SessionBus()


def _iface() -> Any:
    """The ScreenShot2 interface."""
    bus = _bus()
    return dbus.Interface(
        bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2"),
        "org.kde.KWin.ScreenShot2",
    )


def _is_no_authorized(error: BaseException) -> bool:
    """Whether a D-Bus failure is KWin's restricted-interface refusal."""
    name = getattr(error, "get_dbus_name", lambda: "")()
    return name == NO_AUTHORIZED or "NoAuthorized" in str(error)


def _capture_raw(method: str, args: list[Any], options: dict[str, Any]) -> dict[str, Any]:
    """Call one ScreenShot2 method and return the raw frame it piped back.

    KWin answers the D-Bus call *before* a worker thread finishes streaming the
    pixels, so the pipe is drained concurrently; otherwise a frame far larger
    than the pipe buffer deadlocks the compositor.
    """
    interface = _iface()
    read_fd, write_fd = os.pipe()
    chunks: list[bytes] = []
    reader_errors: list[OSError] = []
    stop_reader = threading.Event()

    def drain() -> None:
        try:
            while not stop_reader.is_set():
                try:
                    readable, _, _ = select.select([read_fd], [], [], READER_POLL_S)
                except InterruptedError:
                    continue
                if not readable:
                    continue
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as exc:
            reader_errors.append(exc)
        finally:
            os.close(read_fd)

    reader = threading.Thread(target=drain, name="dsh-cu-screenshot-reader", daemon=True)
    reader.start()
    captured = False
    try:
        results = getattr(interface, method)(
            *args,
            options,
            dbus.types.UnixFd(write_fd),
            timeout=CAPTURE_TIMEOUT_S,
        )
        captured = True
    finally:
        os.close(write_fd)
        if not captured:
            stop_reader.set()
        reader.join(timeout=READER_JOIN_S)
        if reader.is_alive():
            stop_reader.set()
            reader.join(timeout=READER_JOIN_S)
        if reader.is_alive():
            raise BridgeError("KWin ScreenShot2 pipe reader did not terminate", "SCREENSHOT_PIPE_STUCK")

    if reader_errors:
        raise BridgeError(f"KWin ScreenShot2 pipe read failed: {reader_errors[0]}", "SCREENSHOT_PIPE_FAILED")
    data = b"".join(chunks)
    if not data:
        raise BridgeError("KWin ScreenShot2 returned no data", "SCREENSHOT_EMPTY")
    try:
        return {
            "data": data,
            "width": int(results["width"]),
            "height": int(results["height"]),
            "stride": int(results["stride"]),
            "format": int(results["format"]) if "format" in results else None,
            "scale": float(results.get("scale", 1.0)),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError(f"KWin ScreenShot2 returned malformed metadata: {exc}", "SCREENSHOT_BAD_METADATA") from exc


def _image_from_raw(frame: dict[str, Any]) -> Any:
    """Decode a raw KWin frame into a Pillow image."""
    from PIL import Image

    image_format = frame["format"] if frame["format"] is not None else 6
    mode, raw_mode = RAW_FORMATS.get(image_format, ("", ""))
    if not mode:
        raise BridgeError(
            f"KWin returned unsupported image format {image_format}",
            "SCREENSHOT_BAD_FORMAT",
        )
    try:
        return Image.frombytes(
            mode, (frame["width"], frame["height"]), frame["data"], "raw", raw_mode, frame["stride"]
        )
    except (OSError, ValueError) as exc:
        raise BridgeError(f"KWin returned malformed image data: {exc}", "SCREENSHOT_BAD_IMAGE") from exc


def screenshot2_status(refresh: bool = False) -> tuple[bool, str]:
    """Whether ScreenShot2 answers for this process, probing once per process.

    The probe captures a 1x1 area, which is cheap and, unlike a full capture,
    cannot disturb anything visible.
    """
    global _authorization
    if _authorization is not None and not refresh:
        return _authorization
    try:
        _capture_raw(
            "CaptureArea",
            [0, 0, 1, 1],
            {"native-resolution": dbus.Boolean(False), "hide-caller-windows": dbus.Boolean(False)},
        )
    except BridgeError as exc:
        _authorization = (False, str(exc))
        return _authorization
    except dbus.DBusException as exc:
        if _is_no_authorized(exc):
            _authorization = (False, authorize_hint())
        else:
            _authorization = (False, f"ScreenShot2 probe failed: {exc}")
        return _authorization
    _authorization = (True, "")
    return _authorization


def authorize_hint() -> str:
    """The actionable explanation for a refused ScreenShot2 call."""
    return (
        "KWin refused the screenshot (org.kde.KWin.ScreenShot2.Error.NoAuthorized). "
        "ScreenShot2 is a restricted interface: the calling executable must be named by a "
        ".desktop file declaring X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2. "
        "Run tools/authorize-screenshot.sh (shipped in this plugin) to install that grant, "
        "or keep using the Spectacle backend."
    )


def _spectacle_binary() -> str:
    """The Spectacle executable, or a routable error."""
    binary = shutil.which("spectacle")
    if not binary:
        raise BridgeError(
            "neither ScreenShot2 nor Spectacle is available for screenshots",
            "SCREENSHOT_UNAVAILABLE",
        )
    return binary


def _capture_spectacle(mode: str, output: Path, include_cursor: bool = False, extra: tuple[str, ...] = ()) -> Path:
    """Capture with Spectacle in background mode; ``mode`` is an argv fragment."""
    binary = _spectacle_binary()
    argv = [binary, "-b", "-n", *mode.split(), *extra, "-o", str(output)]
    if include_cursor:
        argv.insert(1, "-p")
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=SPECTACLE_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            f"Spectacle did not finish within {SPECTACLE_TIMEOUT_S:g}s",
            "SCREENSHOT_TIMEOUT",
        ) from exc
    if not output.is_file() or output.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-300:]
        raise BridgeError(
            f"Spectacle wrote no image (exit {completed.returncode}): {detail}",
            "SCREENSHOT_EMPTY",
        )
    return output


def _screenshot_dir() -> Path:
    """The default directory screenshots are written to."""
    override = os.environ.get("DSH_CU_SCREENSHOT_DIR")
    if override:
        return Path(override)
    state = os.environ.get("DSH_CU_STATE_DIR")
    base = Path(state) if state else Path.home() / ".local" / "state" / "dsh-cu"
    return base / "screenshots"


def _target_path(save_to: str | None, kind: str) -> Path:
    """Resolve the output path for one capture, creating its directory."""
    if save_to:
        path = Path(save_to).expanduser()
        if path.is_dir():
            path = path / f"dsh-cu-{kind}-{int(time.time())}.png"
    else:
        path = _screenshot_dir() / f"dsh-cu-{kind}-{int(time.time() * 1000)}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _uniform_colour(image: Any) -> tuple[int, int, int] | None:
    """The single colour of a one-colour frame, or None when it has content.

    A blank frame is the signature of a display that is powered off, a stuck
    compositor scene, or a full-screen surface covering the desktop; an agent
    that cannot tell "black screen" from "black wallpaper" will act on the wrong
    assumption, so this is reported rather than rendered.
    """
    try:
        colours = image.convert("RGB").getcolors(maxcolors=2)
    except (OSError, ValueError):
        return None
    if colours is not None and len(colours) == 1:
        return colours[0][1]
    return None


def _finalize(
    image: Any,
    path: Path,
    *,
    backend: str,
    origin: list[int],
    size: list[int],
    scale: float,
    max_edge: int,
    reason: str = "",
) -> dict[str, Any]:
    """Save one image, downscale when asked, and describe its mapping."""
    image_size = [image.width, image.height]
    uniform = _uniform_colour(image)
    if uniform is not None:
        blank = (
            f"the captured frame is a single uniform colour {uniform}; the display may be "
            "powered off, the compositor may not be rendering, or a full-screen surface "
            "may be covering the desktop"
        )
        reason = f"{reason}; {blank}" if reason else blank
    if max_edge > 0 and max(image.width, image.height) > max_edge:
        from PIL import Image as PilImage

        ratio = max_edge / max(image.width, image.height)
        target = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        image = image.resize(target, PilImage.LANCZOS)
    try:
        image.save(path, "PNG")
    except OSError as exc:
        raise BridgeError(f"cannot write {path}: {exc}", "SCREENSHOT_WRITE_FAILED") from exc
    result: dict[str, Any] = {
        "path": str(path),
        "backend": backend,
        "pixels": image_size,
        "logical": {"origin": origin, "size": size},
        "scale": scale,
        "coverage": "full" if not reason else "unknown",
        "reason": reason,
        "uniformColour": list(uniform) if uniform is not None else None,
        "sessionLocked": session.locked(),
        "image": [image.width, image.height] if [image.width, image.height] != image_size else None,
    }
    if result["image"] is None:
        result["coordinateNote"] = (
            f"pixel (px, py) is logical ({origin[0]} + px, {origin[1]} + py)"
            if not reason
            else f"coordinates unavailable: {reason}"
        )
    else:
        result["coordinateNote"] = (
            "downscaled: logical x = "
            f"{origin[0]} + (px + 0.5) * {size[0]} / {image.width} - 0.5, "
            f"logical y = {origin[1]} + (py + 0.5) * {size[1]} / {image.height} - 0.5"
        )
    return result


def _workspace_geometry() -> dict[str, Any]:
    """KWin's output topology, needed to prove where a frame starts."""
    return kwin.outputs()


def capture_workspace(
    *,
    save_to: str | None = None,
    max_edge: int = 0,
    include_cursor: bool = False,
    backend: str = "auto",
    refresh_authorization: bool = False,
) -> dict[str, Any]:
    """Capture the whole logical workspace."""
    path = _target_path(save_to, "workspace")
    topology = _workspace_geometry()
    virtual = topology["virtual"]
    if backend in ("auto", "screenshot2"):
        available, detail = screenshot2_status(refresh=refresh_authorization)
        if available:
            frame = _capture_raw(
                "CaptureWorkspace",
                [],
                {
                    "include-cursor": dbus.Boolean(include_cursor),
                    "hide-caller-windows": dbus.Boolean(False),
                },
            )
            image = _image_from_raw(frame)
            reason = ""
            if abs(frame["scale"] - 1.0) > 1e-6 or abs(image.width - virtual[2]) > 2 or abs(image.height - virtual[3]) > 2:
                reason = (
                    f"ScreenShot2 returned {image.width}x{image.height} at scale {frame['scale']:g}, "
                    f"not KWin's logical workspace {virtual[2]}x{virtual[3]}"
                )
            return _finalize(
                image, path, backend="screenshot2", origin=[virtual[0], virtual[1]],
                size=[virtual[2], virtual[3]], scale=frame["scale"], max_edge=max_edge, reason=reason,
            )
        if backend == "screenshot2":
            raise BridgeError(detail, "SCREENSHOT_NOT_AUTHORIZED")
    _capture_spectacle("-f", path, include_cursor=include_cursor)
    from PIL import Image

    with Image.open(path) as image:
        image.load()
        reason = ""
        if image.width != virtual[2] or image.height != virtual[3]:
            reason = (
                f"Spectacle returned {image.width}x{image.height}, not KWin's logical "
                f"workspace {virtual[2]}x{virtual[3]}"
            )
        return _finalize(
            image, path, backend="spectacle", origin=[virtual[0], virtual[1]],
            size=[virtual[2], virtual[3]], scale=1.0, max_edge=max_edge, reason=reason,
        )


def capture_window(
    *,
    window_id: str = "",
    app: str = "",
    save_to: str | None = None,
    max_edge: int = 0,
    include_cursor: bool = False,
    backend: str = "auto",
    refresh_authorization: bool = False,
) -> dict[str, Any]:
    """Capture one window, including its decoration.

    The window is activated first: both the ScreenShot2 window call and
    Spectacle's active-window mode need the window to be the one on top, and a
    minimized or covered window is captured as its current (possibly obscured)
    state either way.
    """
    window = kwin.require_window(window_id=window_id, app=app)
    path = _target_path(save_to, "window")
    if backend in ("auto", "screenshot2"):
        available, detail = screenshot2_status(refresh=refresh_authorization)
        if available:
            frame = _capture_raw(
                "CaptureWindow",
                [window["id"]],
                {
                    "include-cursor": dbus.Boolean(include_cursor),
                    "include-decoration": dbus.Boolean(True),
                    "include-shadow": dbus.Boolean(False),
                    "hide-caller-windows": dbus.Boolean(False),
                },
            )
            image = _image_from_raw(frame)
            origin, reason = _window_origin(window, image.width, image.height, frame["scale"])
            return _finalize(
                image, path, backend="screenshot2", origin=origin, size=[image.width, image.height],
                scale=frame["scale"], max_edge=max_edge, reason=reason,
            )
        if backend == "screenshot2":
            raise BridgeError(detail, "SCREENSHOT_NOT_AUTHORIZED")

    kwin.window_action("activate", window_id=window["id"])
    time.sleep(0.35)
    before = kwin.find_window(window_id=window["id"]) or window
    # ``-S`` drops the window shadow: with it, Spectacle returns an image larger
    # than every KWin rectangle for the window (a 130px margin was measured), so
    # no pixel could be mapped to a coordinate.
    _capture_spectacle("-a", path, include_cursor=include_cursor, extra=("-S",))
    after = kwin.find_window(window_id=window["id"]) or before
    from PIL import Image

    with Image.open(path) as image:
        image.load()
        origin, reason = _window_origin(before, image.width, image.height, 1.0)
        if not reason and before["frame"] != after["frame"]:
            reason = "the window moved while the capture was taken"
        return _finalize(
            image, path, backend="spectacle", origin=origin, size=[image.width, image.height],
            scale=1.0, max_edge=max_edge, reason=reason,
        )


def _window_origin(window: dict[str, Any], width: int, height: int, scale: float) -> tuple[list[int], str]:
    """Decide where a captured window image starts in logical coordinates.

    A capture either includes the decoration (frame-sized, frame origin) or not
    (client-sized, client origin). Which one it is, is proven by matching the
    image size against KWin's rectangles rather than assumed per toolkit.
    """
    frame = window["frame"]
    client = window["client"]
    buffer = window["buffer"]
    if abs(frame[2] - width) <= 2 and abs(frame[3] - height) <= 2:
        return [frame[0], frame[1]], ""
    if abs(client[2] - width) <= 2 and abs(client[3] - height) <= 2:
        return [client[0], client[1]], ""
    if abs(buffer[2] - width) <= 2 and abs(buffer[3] - height) <= 2:
        return [buffer[0], buffer[1]], ""
    return [frame[0], frame[1]], (
        f"the captured image {width}x{height} matches neither the window frame "
        f"{frame[2]}x{frame[3]}, its client area {client[2]}x{client[3]}, nor its buffer "
        f"{buffer[2]}x{buffer[3]}; coordinates cannot be derived"
    )


def capture_area(
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    save_to: str | None = None,
    max_edge: int = 0,
    include_cursor: bool = False,
    backend: str = "auto",
    refresh_authorization: bool = False,
) -> dict[str, Any]:
    """Capture one logical rectangle of the workspace."""
    if width <= 0 or height <= 0:
        raise BridgeError("area width and height must be positive", "BAD_ARGUMENT")
    path = _target_path(save_to, "area")
    topology = _workspace_geometry()
    virtual = topology["virtual"]
    if not (
        virtual[0] <= x and virtual[1] <= y
        and x + width <= virtual[0] + virtual[2] and y + height <= virtual[1] + virtual[3]
    ):
        raise BridgeError(
            f"area ({x}, {y}, {width}x{height}) is outside the workspace "
            f"({virtual[0]}, {virtual[1]}, {virtual[2]}x{virtual[3]})",
            "AREA_OUTSIDE_WORKSPACE",
        )
    if backend in ("auto", "screenshot2"):
        available, detail = screenshot2_status(refresh=refresh_authorization)
        if available:
            frame = _capture_raw(
                "CaptureArea",
                [x, y, width, height],
                {
                    "include-cursor": dbus.Boolean(include_cursor),
                    "hide-caller-windows": dbus.Boolean(False),
                },
            )
            image = _image_from_raw(frame)
            reason = ""
            if abs(image.width - width) > 2 or abs(image.height - height) > 2:
                reason = f"ScreenShot2 returned {image.width}x{image.height} for a {width}x{height} area"
            return _finalize(
                image, path, backend="screenshot2", origin=[x, y], size=[width, height],
                scale=frame["scale"], max_edge=max_edge, reason=reason,
            )
        if backend == "screenshot2":
            raise BridgeError(detail, "SCREENSHOT_NOT_AUTHORIZED")

    full_path = path.with_name(path.stem + "-full.png")
    _capture_spectacle("-f", full_path, include_cursor=include_cursor)
    from PIL import Image

    try:
        with Image.open(full_path) as full:
            full.load()
            left = x - virtual[0]
            top = y - virtual[1]
            reason = ""
            if full.width != virtual[2] or full.height != virtual[3]:
                reason = (
                    f"the full-desktop frame is {full.width}x{full.height}, not the workspace "
                    f"{virtual[2]}x{virtual[3]}; the crop may be offset"
                )
            crop = full.crop((left, top, left + width, top + height))
            result = _finalize(
                crop, path, backend="spectacle", origin=[x, y], size=[width, height],
                scale=1.0, max_edge=max_edge, reason=reason,
            )
    finally:
        full_path.unlink(missing_ok=True)
    return result
