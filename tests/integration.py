"""Live integration checks for the dsh-cu bridge, against the real desktop.

Unlike the unit tests, this drives the actual session: KWin windows, the
accessibility bus, screenshot backends, flatpak identity, and EIS input. It is
deliberately conservative about what it changes:

* the pointer is moved and then put back where it was found;
* keyboard input is skipped while the session is locked, because typing would go
  into the lock screen;
* the only window mutation is a no-op that writes a window's current state back.

Run: ``python3 tests/integration.py`` (or ``pnpm test:integration``).
Exit status is non-zero if any check fails; skipped checks do not fail the run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "dsh-cu-bridge"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    """Remember one check's outcome and print it immediately."""
    results.append((name, status, detail))
    marker = {PASS: "\033[32mPASS\033[0m", FAIL: "\033[31mFAIL\033[0m", SKIP: "\033[33mSKIP\033[0m"}[status]
    print(f"  [{marker}] {name}{f' — {detail}' if detail else ''}")


class Bridge:
    """A resident bridge process, the way the plugin runs it."""

    def __init__(self) -> None:
        self.child = subprocess.Popen(
            [str(LAUNCHER), "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.next_id = 1

    def call(self, op: str, args: dict | None = None, timeout: float = 180.0) -> dict:
        """Send one request and return the parsed response frame."""
        assert self.child.stdin is not None and self.child.stdout is not None
        request_id = self.next_id
        self.next_id += 1
        self.child.stdin.write(json.dumps({"id": request_id, "op": op, "args": args or {}}) + "\n")
        self.child.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.child.stdout.readline()
            if line == "":
                raise RuntimeError(f"bridge closed the connection during {op}")
            frame = json.loads(line)
            if frame.get("id") == request_id:
                return frame
        raise TimeoutError(f"{op} did not answer within {timeout:g}s")

    def result(self, op: str, args: dict | None = None, timeout: float = 180.0) -> dict:
        """Call an operation and raise when the bridge reported a failure."""
        frame = self.call(op, args, timeout)
        if not frame.get("ok"):
            error = frame.get("error") or {}
            raise RuntimeError(f"{op} failed: {error.get('code')}: {error.get('message')}")
        return frame["result"]

    def close(self) -> None:
        """Ask the bridge to stop and reap it."""
        try:
            self.call("shutdown", {}, timeout=10.0)
        except Exception:  # noqa: BLE001 - teardown best effort
            pass
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()


def check(name: str, function) -> None:  # noqa: ANN001 - test callable
    """Run one check, recording PASS, FAIL, or SKIP(raised SkipCheck)."""
    try:
        detail = function()
    except SkipCheck as exc:
        record(name, SKIP, str(exc))
    except Exception as exc:  # noqa: BLE001 - a failed check is data, not a crash
        record(name, FAIL, f"{type(exc).__name__}: {exc}")
    else:
        record(name, PASS, detail or "")


class SkipCheck(Exception):
    """Raised when the environment cannot exercise a check."""


def main() -> int:
    """Run every check and report the summary."""
    if not LAUNCHER.exists():
        print(f"launcher missing: {LAUNCHER}", file=sys.stderr)
        return 2
    print("dsh-cu integration checks")
    bridge = Bridge()
    state: dict = {}
    try:
        # --- capabilities -----------------------------------------------------

        def check_status() -> str:
            status = bridge.result("status")
            state["status"] = status
            if not status["kwin"]["available"]:
                raise SkipCheck(f"no KWin session: {status['kwin'].get('error')}")
            state["locked"] = status["session"]["locked"]
            return (
                f"kwin {status['kwin']['kwinVersion']}, workspace {status['kwin']['workspace']}, "
                f"input={status['tools']['input']}, accessibility={status['tools']['accessibility']}, "
                f"screenshots={status['tools']['screenshots']}"
                + (f", screenShot2={status['screenshot']['screenShot2']}" if True else "")
                + (", LOCKED" if state["locked"] else "")
            )

        check("status: capabilities and geometry", check_status)

        def check_input_backend() -> str:
            status = state.get("status") or bridge.result("status")
            if not status["input"].get("kwinEis"):
                raise SkipCheck(f"EIS unavailable: {status['input'].get('error')}")
            return "KWin EIS + libei, pointer and keyboard negotiated"

        check("input: KWin EIS and libei available", check_input_backend)

        # --- windows and flatpak identity ------------------------------------

        def check_windows() -> str:
            listing = bridge.result("window.list", {"includeSpecial": True, "includePopups": True})
            state["windows"] = listing["windows"]
            if not listing["windows"]:
                raise SkipCheck("no windows on this desktop")
            sandboxed = [w for w in listing["windows"] if w["flatpakAppId"]]
            for window in listing["windows"]:
                frame = window["frame"]
                if len(frame) != 4 or frame[2] <= 0:
                    raise AssertionError(f"window {window['id']} has an unusable frame {frame}")
            return f"{listing['count']} windows, {len(sandboxed)} in flatpak sandboxes"

        check("windows: listing with geometry", check_windows)

        def check_flatpak_identity() -> str:
            windows = [w for w in state.get("windows", []) if w["flatpakAppId"]]
            if not windows:
                raise SkipCheck("no flatpak window is open right now")
            from_proc = subprocess.run(
                ["flatpak", "ps", "--columns=pid,application"], capture_output=True, text=True, check=False
            ).stdout
            known = {
                int(line.split("\t")[0]): line.split("\t")[1]
                for line in from_proc.splitlines()
                if "\t" in line and line.split("\t")[0].isdigit()
            }
            for window in windows:
                expected = known.get(int(window["pid"]))
                # A window pid may be a child of the sandbox's main pid, so the
                # cgroup scope (not the pid table) is the authority.
                if expected is not None and expected != window["flatpakAppId"]:
                    raise AssertionError(f"{window['app']} reported {window['flatpakAppId']}, flatpak says {expected}")
            return ", ".join(sorted({f"{w['app']}→{w['flatpakAppId']}" for w in windows}))

        check("flatpak: window pid resolves to its app id", check_flatpak_identity)

        def check_flatpak_info() -> str:
            info = bridge.result("flatpak.info")
            if not info["apps"]:
                raise SkipCheck("no flatpak apps installed")
            detail = bridge.result("flatpak.info", {"appId": info["apps"][0]["appId"]})
            permissions = detail["permissions"]
            if "a11yBusAllowed" not in permissions:
                raise AssertionError("permissions payload is missing the accessibility check")
            return (
                f"{len(info['apps'])} apps, {len(info['instances'])} instances; "
                f"{detail['appId']} a11yBusAllowed={permissions['a11yBusAllowed']} "
                f"sockets={permissions['sockets'] or '(none)'}"
            )

        check("flatpak: apps, instances, and permissions", check_flatpak_info)

        # --- accessibility ----------------------------------------------------

        def check_a11y_windows() -> str:
            listing = bridge.result("a11y.windows")
            state["a11y_apps"] = listing["applications"]
            if not listing["applications"]:
                raise SkipCheck(
                    "no application publishes an accessibility tree "
                    "(toolkit accessibility may need an app restart)"
                )
            sandboxed = [a for a in listing["applications"] if a["flatpakAppId"]]
            proxies = [a for a in listing["applications"] if a["proxyPid"]]
            return (
                f"{listing['count']} applications, {len(sandboxed)} flatpak, "
                f"{len(proxies)} published through a sandbox proxy"
                + (f" ({proxies[0]['name']} pid {proxies[0]['pid']})" if proxies else "")
            )

        check("accessibility: applications and flatpak proxies", check_a11y_windows)

        def check_a11y_tree() -> str:
            apps = state.get("a11y_apps") or bridge.result("a11y.windows")["applications"]
            if not apps:
                raise SkipCheck("no accessibility tree available")
            target = apps[0]
            tree = bridge.result("a11y.tree", {"app": target["name"], "maxDepth": 4, "maxElements": 40})
            mapped = [element for element in tree["elements"] if element["rect"] is not None]
            unmapped = [element for element in tree["elements"] if element["rect"] is None]
            if tree["count"] and not mapped:
                reasons = sorted({element["unavailable"] for element in unmapped if element["unavailable"]})
                raise AssertionError(f"no element got coordinates; reasons: {reasons}")
            state["a11y_target"] = target["name"]
            return f"{tree['count']} elements for {target['name']!r}, {len(mapped)} with global coordinates"

        check("accessibility: tree with translated coordinates", check_a11y_tree)

        def check_a11y_find() -> str:
            app = state.get("a11y_target")
            if app is None:
                raise SkipCheck("no accessibility application found")
            found = bridge.result("a11y.find", {"app": app, "query": "", "maxResults": 5})
            if found["count"] == 0:
                raise SkipCheck(f"{app!r} exposes no elements to search")
            element = found["matches"][0]
            return f"{found['count']} matches; first is {element['role']!r} named {element['name']!r}"

        check("accessibility: element search", check_a11y_find)

        # --- screenshots ------------------------------------------------------

        def check_screenshot_workspace() -> str:
            out_dir = Path(os.environ.get("DSH_CU_SCREENSHOT_DIR") or "/tmp/dsh-cu-integration")
            out_dir.mkdir(parents=True, exist_ok=True)
            shot = bridge.result("screenshot", {"target": "workspace", "saveTo": str(out_dir / "workspace.png")})
            state["screenshot"] = shot
            path = Path(shot["path"])
            if not path.exists() or path.stat().st_size == 0:
                raise AssertionError(f"{path} was not written")
            if shot["reason"]:
                raise SkipCheck(f"captured, but {shot['reason']}")
            return (
                f"{shot['backend']} {shot['pixels'][0]}x{shot['pixels'][1]} at origin "
                f"{tuple(shot['logical']['origin'])}"
            )

        check("screenshot: workspace capture and mapping", check_screenshot_workspace)

        def check_screenshot_window() -> str:
            windows = [
                w for w in state.get("windows", [])
                if w["kind"] == "normal" and w["frame"][2] > 100 and w["app"] != "kwin_wayland"
            ]
            if not windows:
                raise SkipCheck("no capturable normal window")
            window = windows[0]
            shot = bridge.result(
                "screenshot",
                {"target": "window", "windowId": window["id"], "maxEdge": 800,
                 "saveTo": str(Path(state["screenshot"]["path"]).with_name("window.png"))},
            )
            if shot["reason"]:
                raise SkipCheck(f"captured, but {shot['reason']}")
            return f"{window['app']!r} via {shot['backend']}, {shot['pixels'][0]}x{shot['pixels'][1]}"

        check("screenshot: single window capture", check_screenshot_window)

        def check_screenshot_area() -> str:
            workspace = (state.get("status") or bridge.result("status"))["kwin"]["workspace"]
            shot = bridge.result(
                "screenshot",
                {"target": "area", "x": workspace[0], "y": workspace[1], "width": 320, "height": 200,
                 "saveTo": str(Path(state["screenshot"]["path"]).with_name("area.png"))},
            )
            if shot["logical"]["size"] != [320, 200]:
                raise AssertionError(f"area mapping says {shot['logical']['size']}, expected [320, 200]")
            return f"{shot['backend']} area 320x200 at {tuple(shot['logical']['origin'])}"

        check("screenshot: area capture", check_screenshot_area)

        # --- input ------------------------------------------------------------

        def check_pointer() -> str:
            status = state.get("status") or bridge.result("status")
            if not status["tools"]["input"]:
                raise SkipCheck("input injection is unavailable")
            before = bridge.result("pointer.position")["position"]
            target = {"x": 640, "y": 360}
            bridge.result("mouse", {"action": "move", "x": target["x"], "y": target["y"]})
            after = bridge.result("pointer.position")["position"]
            bridge.result("mouse", {"action": "move", "x": before["x"], "y": before["y"]})
            restored = bridge.result("pointer.position")["position"]
            if abs(after["x"] - target["x"]) > 2 or abs(after["y"] - target["y"]) > 2:
                raise AssertionError(f"pointer landed at {after}, asked for {target}")
            if abs(restored["x"] - before["x"]) > 2 or abs(restored["y"] - before["y"]) > 2:
                raise AssertionError(f"pointer was not restored to {before}, it is at {restored}")
            return f"moved {before} → {after} → restored {restored}"

        check("input: pointer move through EIS", check_pointer)

        def check_clipboard() -> str:
            before = bridge.result("clipboard", {"action": "read"})["text"]
            marker = "dsh-cu integration check"
            bridge.result("clipboard", {"action": "write", "text": marker})
            read_back = bridge.result("clipboard", {"action": "read"})["text"]
            if read_back != marker:
                raise AssertionError(f"clipboard read back {read_back!r}")
            bridge.result("clipboard", {"action": "write", "text": before})
            restored = bridge.result("clipboard", {"action": "read"})["text"]
            if restored != before:
                raise AssertionError("the previous clipboard contents were not restored")
            return f"wrote and restored {len(before)} characters of previous clipboard"

        check("input: clipboard read/write round trip", check_clipboard)

        def check_keyboard() -> str:
            if state.get("locked"):
                raise SkipCheck("the session is locked; typing would go into the lock screen")
            status = state.get("status") or bridge.result("status")
            if not status["tools"]["input"]:
                raise SkipCheck("input injection is unavailable")
            # A chord with an unresolvable key must be refused before any input.
            frame = bridge.call("keyboard", {"action": "press", "keys": "ctrl+notakey"})
            if frame.get("ok"):
                raise AssertionError("an unknown key was accepted")
            if frame["error"]["code"] != "UNKNOWN_KEY":
                raise AssertionError(f"unexpected error code {frame['error']['code']}")
            return "unknown chords are refused before injecting input"

        check("input: keyboard chord validation", check_keyboard)

        def check_window_control() -> str:
            candidates = [
                w for w in state.get("windows", [])
                if w["kind"] == "normal" and w["app"] not in ("kwin_wayland",) and w["frame"][2] > 100
            ]
            if not candidates:
                raise SkipCheck("no normal window to control")
            window = candidates[0]
            # Write the state back unchanged: exercises the mutation path with no visible effect.
            result = bridge.result(
                "window.control",
                {"action": "keep_above", "windowId": window["id"], "value": bool(window["keepAbove"])},
            )
            if result["window"] is None:
                raise AssertionError("the action did not report the window state")
            if "refused" in result and result["refused"]:
                raise SkipCheck(f"the window refused the action: {result['refused']}")
            return f"{window['app']!r} keep_above={result['window']['keepAbove']} (unchanged)"

        check("windows: control round trip", check_window_control)
    finally:
        bridge.close()

    failures = [entry for entry in results if entry[1] == FAIL]
    skipped = [entry for entry in results if entry[1] == SKIP]
    print(
        f"\n{len(results)} checks: {len(results) - len(failures) - len(skipped)} passed, "
        f"{len(skipped)} skipped, {len(failures)} failed"
    )
    for name_, _status, detail in failures:
        print(f"  FAILED: {name_} — {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
