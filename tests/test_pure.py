"""Unit tests for the parts of the bridge that decide things.

These cover the pure logic that a live desktop cannot easily provoke: flatpak
cgroup identity, the evdev text plan and chord parsing, the KWin script
renderer, the AT-SPI top-level matching rules and their fail-closed reasons, and
the screenshot mapping decisions. Run with:

    python3 -m unittest discover -s tests -p 'test_*.py' -v

The live behaviour is covered separately by tests/integration.py.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from dsh_cu_bridge import atspi, flatpak, keys, kwin, rpc, session, shot, wait  # noqa: E402


def _window_matches(window: dict, **filters: str) -> bool:
    """The bridge's window filter; imported lazily to skip mounting the ops."""
    from dsh_cu_bridge.__main__ import _window_matches as matches

    return matches(window, **filters)


class PollUntilTests(unittest.TestCase):
    """Waiting is a budget, not a fixed sleep: it must stop the moment it can."""

    @staticmethod
    def _clock(*times: float):
        remaining = list(times)
        return lambda: remaining.pop(0) if remaining else times[-1]

    def test_a_ready_first_probe_never_sleeps(self) -> None:
        slept: list[float] = []
        value, elapsed = wait.poll_until(
            lambda: "here",
            lambda seen: seen == "here",
            10.0,
            clock=self._clock(0.0, 0.5),
            sleep=slept.append,
        )
        self.assertEqual(value, "here")
        self.assertEqual(slept, [])
        self.assertAlmostEqual(elapsed, 0.5)

    def test_it_keeps_probing_until_the_value_is_ready(self) -> None:
        seen = iter(["", "", "ready"])
        slept: list[float] = []
        value, _ = wait.poll_until(
            lambda: next(seen),
            lambda candidate: candidate == "ready",
            10.0,
            interval_s=0.2,
            clock=self._clock(0.0, 0.1, 0.3, 0.4),
            sleep=slept.append,
        )
        self.assertEqual(value, "ready")
        self.assertEqual(slept, [0.2, 0.2])

    def test_a_zero_timeout_probes_once(self) -> None:
        calls: list[int] = []

        def probe() -> int:
            calls.append(1)
            return len(calls)

        value, _ = wait.poll_until(probe, lambda _: False, 0.0, clock=self._clock(0.0, 1.0))
        self.assertEqual(value, 1)
        self.assertEqual(len(calls), 1)

    def test_the_sleep_never_runs_past_the_deadline(self) -> None:
        slept: list[float] = []
        wait.poll_until(
            lambda: False,
            lambda _: False,
            0.3,
            interval_s=5.0,
            clock=self._clock(0.0, 0.1, 0.4),
            sleep=slept.append,
        )
        self.assertEqual(len(slept), 1)
        self.assertLessEqual(slept[0], 0.3)


class WindowMatchTests(unittest.TestCase):
    """The wait filter must match what the listing keeps, or a wait never ends."""

    window = {
        "app": "org.prismlauncher.PrismLauncher",
        "caption": "Minecraft 1.12.2",
        "resourceName": "Minecraft",
    }

    def test_app_caption_and_text_match_case_insensitively(self) -> None:
        self.assertTrue(_window_matches(self.window, app="prism"))
        self.assertTrue(_window_matches(self.window, caption="minecraft 1.12"))
        self.assertTrue(_window_matches(self.window, text="1.12.2"))

    def test_a_non_match_is_rejected(self) -> None:
        self.assertFalse(_window_matches(self.window, text="firefox"))

    def test_no_filters_accepts_every_window(self) -> None:
        self.assertTrue(_window_matches(self.window))


class FlatpakScopeTests(unittest.TestCase):
    """The scope string is the only identity that survives the sandbox."""

    def test_app_id_from_real_scope_paths(self) -> None:
        cases = {
            "/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-com.brave.Browser-441856390.scope":
                "com.brave.Browser",
            "/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.vinegarhq.Vinegar-573876804.scope":
                "org.vinegarhq.Vinegar",
            "app-flatpak-com.github.tchx84.Flatseal-1.scope": "com.github.tchx84.Flatseal",
            # An app id containing dashes keeps them: only the trailing counter is stripped.
            "app-flatpak-io.github.celluloid-player.Celluloid-42.scope": "io.github.celluloid-player.Celluloid",
        }
        for scope, expected in cases.items():
            with self.subTest(scope=scope):
                self.assertEqual(flatpak.app_id_from_scope(scope), expected)

    def test_non_flatpak_scopes_are_not_identified(self) -> None:
        for scope in (
            "/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.kde.konsole-96520.scope",
            "/user.slice/user-1000.slice/user@1000.service/app.slice/dsh-subprocess-1.scope",
            "/",
            "",
            None,
        ):
            with self.subTest(scope=scope):
                self.assertIsNone(flatpak.app_id_from_scope(scope))

    def test_scope_unit_requires_the_counter(self) -> None:
        self.assertEqual(flatpak.scope_unit("app-flatpak-com.brave.Browser-1.scope"), "app-flatpak-com.brave.Browser-1.scope")
        self.assertIsNone(flatpak.scope_unit("app-flatpak-com.brave.Browser.scope"))

    def test_instance_keeps_the_counter(self) -> None:
        scope = "/a/b/app-flatpak-com.brave.Browser-441856390.scope"
        self.assertEqual(flatpak.instance_from_scope(scope), "com.brave.Browser-441856390")


class SystemdIdentityTests(unittest.TestCase):
    """The identity path that survives a PID namespace."""

    def test_unit_paths_from_the_real_systemd_resolve(self) -> None:
        cases = {
            "/org/freedesktop/systemd1/unit/app_2dflatpak_2dcom_2ebrave_2eBrowser_2d441856390_2escope":
                "com.brave.Browser",
            "/org/freedesktop/systemd1/unit/app_2dflatpak_2dorg_2evinegarhq_2eVinegar_2d484908348_2escope":
                "org.vinegarhq.Vinegar",
            # A bare unit name works too, so a caller holding either form can use it.
            "app_2dflatpak_2dcom_2ebrave_2eBrowser_2d441856390_2escope": "com.brave.Browser",
            # An escaped underscore inside the app id.
            "app_2dflatpak_2dio_2egithub_2efoo_5fbar_2d7_2escope": "io.github.foo_bar",
        }
        for unit, expected in cases.items():
            with self.subTest(unit=unit):
                self.assertEqual(flatpak.app_id_from_systemd_unit(unit), expected)

    def test_non_flatpak_and_malformed_units_are_not_identified(self) -> None:
        for unit in (
            "/org/freedesktop/systemd1/unit/plasma_2dplasmashell_2eservice",
            "/org/freedesktop/systemd1/unit/app_2dorg_2ekde_2ekonsole_2d96520_2escope",
            # A scope with no instance counter is not a flatpak app scope.
            "app_2dflatpak_2dcom_2ebrave_2eBrowser_2escope",
            "app_2dflatpak_2dcom_2ebrave_2eBrowser_2dnotanumber_2escope",
            "",
        ):
            with self.subTest(unit=unit):
                self.assertIsNone(flatpak.app_id_from_systemd_unit(unit))


class KeyTests(unittest.TestCase):
    """Keycode decisions happen before any input is injected."""

    def test_text_plan_marks_shifted_characters(self) -> None:
        plan = keys.text_plan("aA1!")
        self.assertEqual(plan, [(30, False), (30, True), (2, False), (2, True)])

    def test_text_plan_rejects_unmapped_text_before_typing(self) -> None:
        with self.assertRaises(rpc.BridgeError) as ctx:
            keys.text_plan("naïve")
        self.assertEqual(ctx.exception.code, "NOT_TYPEABLE")
        self.assertIn("ï", ctx.exception.detail["unmapped"])

    def test_parse_chord_orders_modifiers_and_accepts_named_keys(self) -> None:
        self.assertEqual(keys.parse_chord("ctrl+shift+v"), ([29, 42], 47))
        self.assertEqual(keys.parse_chord("Return"), ([], 28))
        self.assertEqual(keys.parse_chord("F5"), ([], 63))

    def test_parse_chord_with_only_a_modifier_presses_that_modifier(self) -> None:
        self.assertEqual(keys.parse_chord("ctrl"), ([], 29))

    def test_parse_chord_rejects_unknown_keys(self) -> None:
        with self.assertRaises(rpc.BridgeError) as ctx:
            keys.parse_chord("ctrl+notakey")
        self.assertEqual(ctx.exception.code, "UNKNOWN_KEY")

    def test_paste_chord_policy(self) -> None:
        self.assertEqual(keys.paste_chord("org.kde.konsole"), "ctrl+shift+v")
        self.assertEqual(keys.paste_chord("com.brave.Browser"), "ctrl+v")
        # A terminal whose binding we do not know must not receive a chord at all.
        self.assertIsNone(keys.paste_chord("some-term"))
        self.assertIsNone(keys.paste_chord("xterm"))
        self.assertIsNone(keys.paste_chord(""))
        self.assertIsNone(keys.paste_chord(None))


class RenderScriptTests(unittest.TestCase):
    """Script rendering must make every value data, never source."""

    def test_values_are_json_literals(self) -> None:
        rendered = kwin._render_script("var t = {window_id}; var q = {needle};", {"window_id": '{x}"y', "needle": "a'b"})
        self.assertEqual(rendered, 'var t = "{x}\\"y"; var q = "a\'b";')

    def test_a_value_cannot_inject_a_placeholder(self) -> None:
        rendered = kwin._render_script("var a = {one}; var b = {two};", {"one": "{two}", "two": "boom"})
        self.assertEqual(rendered, 'var a = "{two}"; var b = "boom";')

    def test_missing_placeholders_are_left_alone(self) -> None:
        self.assertEqual(kwin._render_script("no placeholders", {}), "no placeholders")


class ParseWindowTests(unittest.TestCase):
    """Window records decide what the model sees and what it can address."""

    def _record(self, **overrides: object) -> str:
        import json

        base = {
            "pid": 4242,
            "id": "{abc}",
            "app": "brave-browser",
            "resourceName": "",
            "caption": "Rojo - Creator Store - Brave",
            "normal": True,
            "popup": False,
            "managed": True,
            "deleted": False,
            "desktop": False,
            "dock": False,
            "notification": False,
            "active": True,
            "minimized": False,
            "fullScreen": False,
            "keepAbove": False,
            "closeable": True,
            "moveable": True,
            "resizeable": True,
            "maximizable": True,
            "maximizeMode": "0",
            "output": "HDMI-A-1",
            "desktops": ["1"],
            "frame": [0, 0, 960.5, 1026.4],
            "client": [0, 28, 960, 998],
            "buffer": [-10, -10, 980, 1046],
        }
        base.update(overrides)
        return json.dumps([base])

    def test_geometry_is_rounded_once_and_kind_is_classified(self) -> None:
        parsed = kwin._parse_windows(self._record())
        self.assertEqual(len(parsed), 1)
        window = parsed[0]
        self.assertEqual(window["frame"], [0, 0, 960, 1026])
        self.assertEqual(window["kind"], "normal")
        self.assertTrue(window["active"])

    def test_kind_prefers_the_special_roles(self) -> None:
        self.assertEqual(kwin._parse_windows(self._record(desktop=True))[0]["kind"], "desktop")
        self.assertEqual(kwin._parse_windows(self._record(dock=True))[0]["kind"], "dock")
        self.assertEqual(kwin._parse_windows(self._record(popup=True))[0]["kind"], "popup")

    def test_malformed_records_are_skipped_not_fatal(self) -> None:
        self.assertEqual(kwin._parse_windows("[]"), [])
        self.assertEqual(kwin._parse_windows("not json"), [])
        parsed = kwin._parse_windows('[{"pid": 1}]')
        self.assertEqual(parsed, [])


def _window(**overrides: object) -> dict[str, object]:
    """A KWin window record shaped like ``kwin._parse_windows`` output."""
    base: dict[str, object] = {
        "pid": 100,
        "id": "{w1}",
        "app": "app",
        "resourceName": "",
        "caption": "Rojo - Creator Store - Brave",
        "kind": "normal",
        "active": False,
        "minimized": False,
        "fullScreen": False,
        "keepAbove": False,
        "closeable": True,
        "moveable": True,
        "resizeable": True,
        "maximizable": True,
        "maximizeMode": "0",
        "output": "out",
        "desktops": ["1"],
        "frame": [0, 0, 960, 1026],
        "client": [0, 28, 960, 998],
        "buffer": [-10, -10, 980, 1046],
    }
    base.update(overrides)
    return base


class WindowMatchTests(unittest.TestCase):
    """Matching rules are fail-closed: a wrong coordinate clicks something else."""

    def test_client_rectangle_wins_over_buffer(self) -> None:
        window = _window()
        rect = (0, 0, 960, 998)
        matched, reason, anchor = atspi._match_toplevel(frozenset({100}), "Rojo - Creator Store - Brave", rect, [window])
        self.assertEqual(reason, "")
        self.assertEqual(matched["id"], "{w1}")
        self.assertEqual(anchor, (0, 28))

    def test_buffer_rectangle_is_used_for_a_client_side_decorated_window(self) -> None:
        window = _window()
        matched, reason, anchor = atspi._match_toplevel(
            frozenset({100}), "Rojo - Creator Store - Brave", (-10, -10, 980, 1046), [window]
        )
        self.assertEqual(reason, "")
        self.assertEqual(anchor, (-10, -10))

    def test_a_size_mismatch_is_no_kwin_window(self) -> None:
        _matched, reason, _anchor = atspi._match_toplevel(frozenset({100}), "x", (0, 0, 100, 100), [_window()])
        self.assertEqual(reason, "no-kwin-window")

    def test_two_candidate_windows_are_ambiguous(self) -> None:
        windows = [_window(), _window(id="{w2}", pid=101)]
        _matched, reason, _anchor = atspi._match_toplevel(
            frozenset({100, 101}), "Rojo - Creator Store - Brave", (0, 0, 960, 998), windows
        )
        self.assertEqual(reason, "ambiguous")

    def test_the_caption_check_applies_only_when_both_sides_are_non_empty(self) -> None:
        self.assertTrue(atspi._caption_consistent("Open File — KWrite", "Open File"))
        self.assertTrue(atspi._caption_consistent("file.txt  — KWrite", "file.txt — KWrite"))
        self.assertTrue(atspi._caption_consistent("anything", ""))
        self.assertFalse(atspi._caption_consistent("Document 2 — KWrite", "Document"))

    def test_special_windows_are_not_eligible(self) -> None:
        for kind in ("desktop", "dock", "notification"):
            with self.subTest(kind=kind):
                self.assertFalse(atspi._eligible(_window(kind=kind)))

    def test_finalize_translates_only_with_a_proven_offset(self) -> None:
        element = atspi.Element(
            role="push button", name="Go", description="", states=[], actions=[], text="",
            value=None, value_max=None, children_count=0, depth=2, has_extents=True,
            x=10, y=20, width=30, height=40,
        )
        translated = atspi._finalize(element, atspi._Mapping(_window(), "", offset=(5, 6)))
        self.assertTrue(translated.mapped)
        self.assertEqual((translated.x, translated.y), (15, 26))
        self.assertEqual(translated.window_id, "{w1}")

    def test_finalize_reports_the_reason_instead_of_a_rectangle(self) -> None:
        element = atspi.Element(
            role="frame", name="win", description="", states=[], actions=[], text="",
            value=None, value_max=None, children_count=0, depth=1, has_extents=True,
            x=0, y=0, width=10, height=10,
        )
        blocked = atspi._finalize(element, atspi._Mapping(None, "no-kwin-window"))
        self.assertFalse(blocked.mapped)
        self.assertEqual(blocked.unavailable, "no-kwin-window")
        self.assertIsNone(blocked.as_dict()["rect"])

    def test_element_without_extents_wins_over_mapping_failure(self) -> None:
        element = atspi.Element(
            role="label", name="x", description="", states=[], actions=[], text="",
            value=None, value_max=None, children_count=0, depth=3, has_extents=False,
        )
        blocked = atspi._finalize(element, atspi._Mapping(None, "no-kwin-window"))
        self.assertEqual(blocked.unavailable, "no-extents")


class WindowPidsTests(unittest.TestCase):
    """The flatpak alias is what makes a sandboxed app's tree clickable."""

    def setUp(self) -> None:
        self._original = (flatpak.cgroup_scope, flatpak.comm_of, flatpak.app_id_for_pid, flatpak.scope_unit)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        flatpak.cgroup_scope, flatpak.comm_of, flatpak.app_id_for_pid, flatpak.scope_unit = self._original

    def test_a_pid_kwin_knows_is_returned_as_is(self) -> None:
        flatpak.cgroup_scope = lambda pid: None  # type: ignore[assignment]
        flatpak.comm_of = lambda pid: "brave"  # type: ignore[assignment]
        self.assertEqual(atspi._window_pids(2183, [_window(pid=2183)]), frozenset({2183}))

    def test_the_proxy_pid_aliases_to_its_own_instance_scope(self) -> None:
        scope = "/app.slice/app-flatpak-com.brave.Browser-441856390.scope"
        other = "/app.slice/app-flatpak-com.brave.Browser-2286476163.scope"
        owners = {2179: scope, 2183: scope, 2211: other}
        flatpak.cgroup_scope = lambda pid: owners.get(int(pid))  # type: ignore[assignment]
        flatpak.comm_of = lambda pid: "xdg-dbus-proxy" if int(pid) == 2179 else "brave"  # type: ignore[assignment]
        flatpak.scope_unit = lambda value: value.rsplit("/", 1)[-1] if value else None  # type: ignore[assignment]
        windows = [_window(pid=2183), _window(pid=2211)]
        self.assertEqual(atspi._window_pids(2179, windows), frozenset({2179, 2183}))

    def test_a_non_proxy_pid_falls_back_to_the_app_id_table(self) -> None:
        flatpak.cgroup_scope = lambda pid: None  # type: ignore[assignment]
        flatpak.comm_of = lambda pid: "brave"  # type: ignore[assignment]
        flatpak.app_id_for_pid = lambda pid: "com.brave.Browser" if int(pid) in (2179, 2183) else None  # type: ignore[assignment]
        flatpak.scope_unit = lambda value: None  # type: ignore[assignment]
        self.assertEqual(atspi._window_pids(2179, [_window(pid=2183)]), frozenset({2179, 2183}))


class ScreenshotMappingTests(unittest.TestCase):
    """A frame's origin has to be proven, not assumed."""

    def test_window_origin_matches_frame_client_or_buffer(self) -> None:
        window = {"frame": [0, 0, 960, 1026], "client": [0, 28, 960, 998], "buffer": [-10, -10, 980, 1046]}
        self.assertEqual(shot._window_origin(window, 960, 1026, 1.0), ([0, 0], ""))
        self.assertEqual(shot._window_origin(window, 960, 998, 1.0), ([0, 28], ""))
        self.assertEqual(shot._window_origin(window, 980, 1046, 1.0), ([-10, -10], ""))
        origin, reason = shot._window_origin(window, 500, 500, 1.0)
        self.assertEqual(origin, [0, 0])
        self.assertIn("matches neither", reason)

    def test_every_raw_format_kwin_sends_is_mapped(self) -> None:
        for image_format in (4, 5, 6, 16):
            self.assertIn(image_format, shot.RAW_FORMATS)

    def test_uniform_frames_are_detected(self) -> None:
        from PIL import Image

        blank = Image.new("RGB", (8, 8), (0, 0, 0))
        self.assertEqual(shot._uniform_colour(blank), (0, 0, 0))
        varied = Image.new("RGB", (8, 8), (0, 0, 0))
        varied.putpixel((0, 0), (255, 255, 255))
        self.assertIsNone(shot._uniform_colour(varied))


class RpcTests(unittest.TestCase):
    """The frame contract the TypeScript client parses."""

    def test_frames_round_trip(self) -> None:
        import json

        self.assertEqual(json.loads(rpc.encode(rpc.ok(3, {"a": 1}))), {"id": 3, "ok": True, "result": {"a": 1}})
        failure = json.loads(rpc.encode(rpc.failure(4, rpc.BridgeError("no window", "NO_SUCH_WINDOW", {"app": "x"}))))
        self.assertEqual(failure["error"]["code"], "NO_SUCH_WINDOW")
        self.assertEqual(failure["error"]["detail"], {"app": "x"})

    def test_unexpected_errors_keep_their_class_name_as_the_code(self) -> None:
        frame = rpc.failure(5, ValueError("bad value"))
        self.assertEqual(frame["error"]["code"], "ValueError")
        self.assertEqual(frame["error"]["message"], "bad value")


class EnvironmentTests(unittest.TestCase):
    """Assumptions the modules make about the host are stated, not hidden."""

    def test_proc_is_where_flatpak_scopes_are_read_from(self) -> None:
        self.assertEqual(str(flatpak.PROC), "/proc")

    def test_screenshot_directory_can_be_redirected(self) -> None:
        previous = os.environ.get("DSH_CU_SCREENSHOT_DIR")
        os.environ["DSH_CU_SCREENSHOT_DIR"] = "/tmp/dsh-cu-test-shots"
        self.addCleanup(lambda: os.environ.pop("DSH_CU_SCREENSHOT_DIR", None) if previous is None else os.environ.update({"DSH_CU_SCREENSHOT_DIR": previous}))
        self.assertEqual(shot._screenshot_dir(), Path("/tmp/dsh-cu-test-shots"))


class WritabilityTests(unittest.TestCase):
    """A read-only filesystem is the condition that breaks Spectacle silently."""

    def test_a_read_only_directory_is_reported_unwritable(self) -> None:
        self.assertFalse(session.writable(Path("/proc/self/definitely-not-writable")))

    def test_a_temporary_directory_is_writable_and_left_clean(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "fresh" / "nested"
            self.assertTrue(session.writable(target))
            self.assertTrue(target.is_dir())
            # The probe file must not be left behind.
            self.assertEqual(list(target.iterdir()), [])

    def test_describe_names_the_sandbox_and_the_lock_state(self) -> None:
        described = session.describe()
        for key in ("locked", "sandboxed", "configWritable", "stateDirWritable", "tmpDir"):
            self.assertIn(key, described)
        self.assertIsInstance(described["sandboxed"], bool)


class SpectacleEnvironmentTests(unittest.TestCase):
    """Spectacle refuses to capture when it cannot write spectaclerc."""

    def test_ambient_xdg_is_used_when_it_is_writable(self) -> None:
        self.assertEqual(shot._spectacle_env_for(True, Path("/tmp/state"), Path("/tmp")), {})

    def test_private_xdg_directories_are_used_when_ambient_is_read_only(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            state = Path(scratch) / "state"
            overrides = shot._spectacle_env_for(False, state, Path("/tmp"))
            self.assertEqual(overrides["XDG_CONFIG_HOME"], str(state / "spectacle" / "config"))
            self.assertEqual(overrides["XDG_CACHE_HOME"], str(state / "spectacle" / "cache"))
            self.assertEqual(overrides["XDG_DATA_HOME"], str(state / "spectacle" / "data"))
            self.assertTrue(Path(overrides["XDG_CONFIG_HOME"]).is_dir())

    def test_the_temporary_directory_is_the_last_resort(self) -> None:
        # A state directory that cannot be created (inside the read-only tree)
        # must not stop Spectacle from getting a writable configuration.
        overrides = shot._spectacle_env_for(False, Path("/proc/self/no-state"), Path("/tmp"))
        self.assertTrue(overrides["XDG_CONFIG_HOME"].startswith("/tmp/dsh-cu-spectacle/"))
        self.assertTrue(Path(overrides["XDG_CONFIG_HOME"]).is_dir())

    def test_an_unwritable_explicit_destination_fails_loudly(self) -> None:
        with self.assertRaises(rpc.BridgeError) as ctx:
            shot._target_path("/proc/self/nope/shot.png", "workspace")
        self.assertEqual(ctx.exception.code, "SCREENSHOT_WRITE_FAILED")

    def test_the_default_destination_falls_back_to_tmp_when_the_state_dir_is_read_only(self) -> None:
        previous = os.environ.get("DSH_CU_SCREENSHOT_DIR")
        os.environ["DSH_CU_SCREENSHOT_DIR"] = "/proc/self/read-only/shots"
        self.addCleanup(lambda: os.environ.pop("DSH_CU_SCREENSHOT_DIR", None) if previous is None else os.environ.update({"DSH_CU_SCREENSHOT_DIR": previous}))
        path = shot._target_path(None, "workspace")
        self.assertTrue(path.parent.name == "dsh-cu-screenshots", str(path))


if __name__ == "__main__":
    unittest.main()
