"""End-to-end: launch Minecraft through Prism, get into a world, break a tree.

The plugin is only as good as what it can do to a real, latency-sensitive game,
so this drives one: a flatpak Prism Launcher starting Minecraft Java 1.21.1.
It exercises launch-and-wait, window targeting, timed key holds (the lease
feature: `hold` never depends on how fast this script is), a mouse hold with a
lease, and menu clicks. The pass condition is not a screenshot, it is the game's
own statistics file: ``minecraft:mined`` -> ``minecraft:oak_log`` must rise.

Default run, about a minute: restore the pristine world, quick-launch straight
into it (``-w``), walk to the tree, mine it, save and quit, read the stats.

    python3 tests/e2e_minecraft.py             # quick path
    python3 tests/e2e_minecraft.py --snapshot  # also exercises Create New World, then
                                               # stores the pristine copy the quick path restores

Needs: org.prismlauncher.PrismLauncher (flatpak) with a Microsoft account that
owns Minecraft Java, and an instance named ``e2e`` on 1.21.1 (see README). The
account is read from Prism's accounts.json; nothing here handles credentials.
Overrides: E2E_INSTANCE, E2E_PROFILE, E2E_SEED.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from integration import Bridge  # noqa: E402

APP = "org.prismlauncher.PrismLauncher"
DATA = Path.home() / ".var/app" / APP / "data/PrismLauncher"
INSTANCE = os.environ.get("E2E_INSTANCE", "e2e")
WORLD = "e2e"
# Seed 2 on 1.21.1 spawns beside a tree: the route below is deterministic from it.
SEED = os.environ.get("E2E_SEED", "2")
SAVES = DATA / "instances" / INSTANCE / "minecraft" / "saves"
LATEST_LOG = DATA / "instances" / INSTANCE / "minecraft" / "logs" / "latest.log"
# Printed once resources are loaded, a moment before the title screen is clickable.
REF_LEFT, REF_TOP = 533, 300
TITLE_READY = "Sound engine started"

# From the pristine start to a trunk: ("look", dx, dy) turns the camera in relative mouse
# counts (about one screen pixel each); ("hold", key, ms) is timed inside the bridge.
ROUTE = [
    ("look", 0, 120),
    ("look", 300, 0),
    ("look", -72, 18),
    ("hold", "w", 1100),
    ("look", 300, 0),
    ("hold", "w", 800),
    ("look", -20, -90),
]
BREAK_MS = int(os.environ.get("E2E_BREAK_MS", "4500"))  # bare hands take ~3 s; 3.4 s failed 2 of 6 runs
SETTLE_MS = 150  # between route steps: lets the game apply the last input before the next
WORLD_SETTLE_S = float(os.environ.get("E2E_WORLD_SETTLE_S", "1.0"))
TRACE = bool(os.environ.get("E2E_TRACE"))
LAUNCH_TIMEOUT_S = 180


class E2EFailure(Exception):
    pass


def profile_name() -> str:
    """The Prism account to launch with: env override, else the first one in accounts.json."""
    if os.environ.get("E2E_PROFILE"):
        return os.environ["E2E_PROFILE"]
    accounts = json.loads((DATA / "accounts.json").read_text(encoding="utf-8"))["accounts"]
    for account in accounts:
        name = account.get("profile", {}).get("name")
        if name:
            return name
    raise E2EFailure("no signed-in account in Prism's accounts.json; sign in through Prism first")


class Game:
    """One Minecraft window, addressed in fractions of its client area."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.window: dict = {}
        self.steps: list[tuple[str, str]] = []

    def op(self, name: str, args: dict | None = None) -> dict:
        return self.bridge.result(name, args or {})

    def windows(self) -> list[dict]:
        return self.op("window.list")["windows"]

    def find_window(self) -> dict | None:
        for window in self.windows():
            if window["caption"].startswith("Minecraft") and "prism" not in window["app"].lower():
                self.window = window
                return window
        return None

    def wait_window(self, predicate, what: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            window = self.find_window()
            if window and predicate(window["caption"]):
                return window
            time.sleep(1.0)
        raise E2EFailure(f"timed out after {timeout:g}s waiting for {what}")

    def focus(self) -> None:
        self.op("window.control", {"action": "activate", "windowId": self.window["id"]})
        time.sleep(0.4)

    def wait_title_screen(self, timeout: float) -> None:
        """The window exists long before the menu does; the game's own log says when it is up."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if TITLE_READY in LATEST_LOG.read_text(encoding="utf-8", errors="replace"):
                    time.sleep(2.0)
                    return
            except FileNotFoundError:
                pass
            time.sleep(1.0)
        raise E2EFailure(f"the game did not finish loading within {timeout:g}s")

    def type_text(self, text: str) -> None:
        """Type only into the game: a stray keystroke on the desktop opens KRunner."""
        active = next((w for w in self.windows() if w["active"]), None)
        if not active or active["id"] != self.window["id"]:
            raise E2EFailure(f"refusing to type {text!r}: the game does not have focus")
        self.op("keyboard", {"action": "type", "text": text})

    def click(self, x: float, y: float) -> None:
        """Click at a client-area fraction, so the layout survives a window of any size."""
        left, top, width, height = self.window["client"]
        self.op("mouse", {"action": "click", "x": int(left + x * width), "y": int(top + y * height)})

    def click_px(self, x: int, y: int) -> None:
        """Click a screen pixel measured with the game's 854x480 client area at (533, 300)."""
        self.click((x - REF_LEFT) / 854, (y - REF_TOP) / 480)

    def batch(self, steps: list[dict]) -> list[dict]:
        """Several bridge operations in one request; raises on the first failed step."""
        result = self.op("batch", {"steps": steps})
        if result["failedAt"] is not None:
            failed = result["results"][result["failedAt"]]
            raise E2EFailure(f"batch step {result['failedAt']} ({failed['op']}) failed: {failed['error']}")
        return result["results"]

    def click_step(self, x: int, y: int) -> dict:
        """A batchable click at a screen pixel measured on the reference window."""
        left, top, width, height = self.window["client"]
        return {"op": "mouse", "args": {
            "action": "click",
            "x": int(left + (x - REF_LEFT) / 854 * width),
            "y": int(top + (y - REF_TOP) / 480 * height),
        }}

    def hold(self, keys: str, ms: int) -> None:
        self.op("keyboard", {"action": "hold", "keys": keys, "holdMs": ms})

    def screenshot(self) -> str:
        return self.op("screenshot")["path"]

    def trace(self, step: str) -> None:
        """Keep a screenshot per step; they are listed when a run fails."""
        self.steps.append((step, self.screenshot()))


def stats_mined(name: str = "minecraft:oak_log") -> int:
    for path in (SAVES / WORLD / "stats").glob("*.json"):
        stats = json.loads(path.read_text(encoding="utf-8"))["stats"]
        return int(stats.get("minecraft:mined", {}).get(name, 0))
    return 0


def launch(world: bool) -> None:
    LATEST_LOG.unlink(missing_ok=True)
    command = ["flatpak", "run", APP, "-l", INSTANCE, "-a", profile_name()]
    if world:
        command += ["-w", WORLD]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def stop_everything(game: Game) -> None:
    """Close Minecraft and Prism so a run always starts from nothing."""
    for window in game.windows():
        if window["app"] == APP or window["caption"].startswith("Minecraft"):
            game.op("window.control", {"action": "close", "windowId": window["id"]})
    subprocess.run(["flatpak", "kill", APP], check=False, capture_output=True)
    wait_until(lambda: not any(w["app"] == APP for w in game.windows()), "Prism to exit", 10)


def create_world_through_the_ui(game: Game) -> None:
    """Title screen -> Singleplayer -> Create New World, peaceful, fixed seed."""
    game.wait_window(lambda caption: caption.startswith("Minecraft"), "the game window", LAUNCH_TIMEOUT_S)
    game.wait_title_screen(LAUNCH_TIMEOUT_S)
    game.focus()
    game.click_px(958, 747)  # Continue, on the first-run accessibility prompt (no-op when absent)
    time.sleep(1.5)
    game.click_px(958, 535)  # Singleplayer: with no saves this opens Create New World
    time.sleep(2.5)
    game.click_px(958, 415)
    game.op("keyboard", {"action": "press", "keys": "ctrl+a"})
    game.type_text(WORLD)
    for _ in range(2):  # Normal -> Hard -> Peaceful
        game.click_px(958, 527)
        time.sleep(0.3)
    game.click_px(960, 327)  # World tab
    time.sleep(0.8)
    game.click_px(958, 471)
    game.type_text(SEED)
    game.click_px(800, 747)  # Create New World


def wait_until(condition, what: str, timeout: float, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise E2EFailure(f"timed out after {timeout:g}s waiting for {what}")


def in_world(game: Game) -> bool:
    window = game.find_window()
    return bool(window and window["caption"].endswith("Singleplayer"))


def save_and_quit(game: Game) -> None:
    """Escape, Save and Quit to Title, then wait for the world to be left (the save flushes stats)."""
    game.focus()
    game.batch([
        {"op": "keyboard", "args": {"action": "press", "keys": "Escape"}},
        {"op": "sleep", "args": {"ms": 250}},  # the pause menu animates in
        game.click_step(958, 647),  # Save and Quit to Title
    ])
    wait_until(lambda: not in_world(game), "the world to be saved and left", 30)


def quit_game(game: Game) -> None:
    """Click Quit Game until the window goes: the title screen is not clickable the instant it is shown."""
    for _ in range(8):
        if game.find_window() is None:
            return
        game.batch([game.click_step(1059, 703)])
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and game.find_window() is not None:
            time.sleep(0.1)
    wait_until(lambda: game.find_window() is None, "the game to exit", 10)


def run_snapshot(game: Game) -> None:
    shutil.rmtree(SAVES / WORLD, ignore_errors=True)
    shutil.rmtree(SAVES / f"{WORLD}.pristine", ignore_errors=True)
    launch(world=False)
    create_world_through_the_ui(game)
    game.wait_window(lambda caption: caption.endswith("Singleplayer"), "the new world to load", 90)
    time.sleep(3)
    save_and_quit(game)
    quit_game(game)
    stop_everything(game)
    shutil.copytree(SAVES / WORLD, SAVES / f"{WORLD}.pristine")
    print(f"snapshot stored: {SAVES / (WORLD + '.pristine')}")


def route_steps(game: Game) -> list[dict]:
    """The whole walk-aim-mine sequence as one batch: no round trip, so no agent lag, between actions."""
    left, top, width, height = game.window["client"]
    centre = {"x": left + width // 2, "y": top + height // 2, "button": "left"}
    steps: list[dict] = []
    for step in ROUTE:
        if step[0] == "look":
            steps.append({"op": "mouse", "args": {"action": "look", "dx": step[1], "dy": step[2]}})
        else:
            steps.append({"op": "keyboard", "args": {"action": "hold", "keys": step[1], "holdMs": step[2]}})
        steps.append({"op": "sleep", "args": {"ms": SETTLE_MS}})
    steps.append({"op": "mouse", "args": {"action": "down", "leaseMs": BREAK_MS + 1000, **centre}})
    steps.append({"op": "sleep", "args": {"ms": BREAK_MS}})
    steps.append({"op": "mouse", "args": {"action": "up", **centre}})
    if TRACE:
        steps.append({"op": "screenshot"})
    return steps


def run_tree(game: Game) -> dict:
    pristine = SAVES / f"{WORLD}.pristine"
    if not pristine.exists():
        raise E2EFailure("no pristine world; run once with --snapshot")
    shutil.rmtree(SAVES / WORLD, ignore_errors=True)
    shutil.copytree(pristine, SAVES / WORLD)
    timings: dict[str, float] = {}
    clock = time.monotonic()

    def lap(name: str) -> None:
        nonlocal clock
        now = time.monotonic()
        timings[name] = round(now - clock, 2)
        clock = now

    launch(world=True)
    wait_until(lambda: in_world(game), "the world to load", LAUNCH_TIMEOUT_S, interval=0.25)
    lap("launch_to_world_s")
    time.sleep(WORLD_SETTLE_S)  # the first chunks must arrive before the player moves
    game.focus()
    lap("settle_focus_s")
    before = stats_mined()
    results = game.batch(route_steps(game))
    lap("route_and_mine_s")
    if TRACE:
        print(f"  after mining: {results[-1]['result']['path']}")
    save_and_quit(game)
    lap("save_quit_s")
    after = stats_mined()
    quit_game(game)
    lap("quit_game_s")
    stop_everything(game)
    lap("stop_s")
    if after <= before:
        raise E2EFailure(f"no oak log was mined (before={before}, after={after})")
    return {"oak_logs_mined": after - before, "total_s": round(sum(timings.values()), 1), **timings}


def main() -> int:
    snapshot = "--snapshot" in sys.argv[1:]
    bridge = Bridge()
    game = Game(bridge)
    try:
        stop_everything(game)
        if snapshot:
            run_snapshot(game)
        outcome = run_tree(game)
        print(f"PASS {json.dumps(outcome)}")
        return 0
    except E2EFailure as failure:
        for step, path in game.steps:
            print(f"  {step}: {path}")
        print(f"FAIL {failure}")
        return 1
    finally:
        try:
            game.op("keyboard", {"action": "release_all"})
        finally:
            bridge.close()


if __name__ == "__main__":
    sys.exit(main())
