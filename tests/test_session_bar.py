from __future__ import annotations

import builtins
import shutil
import subprocess
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, cast
from unittest import mock

from kitty_workbench import session_bar
from kitty_workbench.model import (
    AGENT_VAR,
    APP_VAR,
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SLUG_VAR,
)
from kitty_workbench.session_bar import SessionBarBoss, SessionBarTab, render_tab_label

PROJECT = Path(__file__).parents[1]


class Datum(NamedTuple):
    title: str
    tab_id: int
    is_active: bool = False
    needs_attention: bool = False


class LegacyDatum(NamedTuple):
    title: str
    tab_id: int


@dataclass(frozen=True, slots=True)
class ExtraData:
    prev_tab: Datum | None
    next_tab: Datum | None = None
    for_layout: bool = False


@dataclass(slots=True)
class Child:
    cmdline: object = ()


@dataclass(slots=True)
class Window:
    user_vars: object
    title: str = "Shell"
    child: Child | None = None


class NativeTab:
    def __init__(self, *windows: Window, fail: bool = False) -> None:
        self.windows = windows
        self.fail = fail

    def __iter__(self) -> Iterator[Window]:
        if self.fail:
            raise RuntimeError("tab disappeared")
        return iter(self.windows)


class Boss:
    def __init__(self, tabs: dict[int, NativeTab], fail: bool = False) -> None:
        self.tabs = tabs
        self.fail = fail

    def tab_for_id(self, tab_id: int) -> NativeTab | None:
        if self.fail:
            raise RuntimeError("boss unavailable")
        return self.tabs.get(tab_id)


@dataclass(frozen=True, slots=True)
class DrawData:
    tab_bar_edge: str
    active_bg: int = 101
    active_fg: int = 102
    inactive_bg: int = 201
    inactive_fg: int = 202

    def tab_bg(self, tab: object) -> int:
        return self.active_bg if bool(getattr(tab, "is_active", False)) else self.inactive_bg

    def tab_fg(self, tab: object) -> int:
        return self.active_fg if bool(getattr(tab, "is_active", False)) else self.inactive_fg


@dataclass(slots=True)
class Cursor:
    x: int
    bg: int = 0
    fg: int = 0


@dataclass(slots=True)
class Screen:
    cursor: Cursor


class RecordingDrawer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object, int, int, int, bool, object]] = []
        self.colors: list[tuple[int, int]] = []

    def __call__(
        self,
        draw_data: object,
        screen: object,
        tab: object,
        before: int,
        max_title_length: int,
        index: int,
        is_last: bool,
        extra_data: object,
    ) -> int:
        self.calls.append(
            (
                draw_data,
                screen,
                tab,
                before,
                max_title_length,
                index,
                is_last,
                extra_data,
            )
        )
        cursor = getattr(screen, "cursor", None)
        if isinstance(cursor, Cursor):
            self.colors.append((cursor.bg, cursor.fg))
            cursor.x = before + max_title_length
            return cursor.x
        return 41


class Cache:
    def __init__(self) -> None:
        self.clears = 0

    def clear_cached(self) -> None:
        self.clears += 1


class Manager:
    def __init__(self) -> None:
        self.events: list[str] = []

    def mark_tab_bar_dirty(self) -> None:
        self.events.append("dirty")

    def update_tab_bar_data(self) -> None:
        self.events.append("update")


class ReloadBoss:
    def __init__(self, managers: list[Manager]) -> None:
        self.all_tab_managers = managers
        self.refreshes = 0

    def refresh_active_tab_bar(self) -> bool:
        self.refreshes += 1
        return True


def _fixture_tabs() -> list[SessionBarTab]:
    return [
        SessionBarTab("shell", "research", "Research Work"),
        SessionBarTab("tests", "research", "Research Work", ("claude", "nvim")),
        SessionBarTab(
            "agent review",
            "research",
            "Research Work",
            ("codex", "claude", "claude"),
        ),
        SessionBarTab("notes", None, None),
        SessionBarTab("scratch", None, None),
    ]


def _render_fixture(width: int) -> str:
    labels: list[str] = []
    previous: SessionBarTab | None = None
    for tab in _fixture_tabs():
        labels.append(render_tab_label(tab, previous, width))
        previous = tab
    return "    ".join(labels)


class SessionBarRenderingTests(unittest.TestCase):
    def test_reviewed_wide_and_compact_rows_match_the_golden_render(self) -> None:
        rendered = "\n".join(
            (
                f"top wide     {_render_fixture(60)}",
                f"bottom wide  {_render_fixture(60)}",
                f"top narrow   {_render_fixture(14)}",
                f"bottom tiny  {_render_fixture(8)}",
            )
        )
        golden = Path(__file__).parent / "golden" / "session-bar.txt"
        self.assertMultiLineEqual(rendered + "\n", golden.read_text(encoding="utf-8"))

    def test_group_boundaries_and_widths_preserve_the_useful_identity(self) -> None:
        first, second, _, unattached, next_unattached = _fixture_tabs()
        other = SessionBarTab("deploy", "other", "Operations", ("codex",))

        self.assertIn(" Research Work", render_tab_label(first, None, 60))
        self.assertNotIn("Research Work", render_tab_label(second, first, 60))
        self.assertIn(" Operations", render_tab_label(other, second, 60))
        self.assertIn("󰌸 Unattached", render_tab_label(unattached, other, 60))
        self.assertNotIn("Unattached", render_tab_label(next_unattached, unattached, 60))
        self.assertIn("shell", render_tab_label(first, None, 14))
        self.assertEqual(render_tab_label(first, None, 1), "…")
        self.assertEqual(render_tab_label(first, None, 0), "")
        self.assertEqual(session_bar._ellipsize("anything", 0), "")

        medium_group = render_tab_label(
            SessionBarTab("test", "id", "Team", ("claude",)),
            None,
            20,
        )
        self.assertEqual(medium_group, " Team │ test ✻")
        tiny_tab = render_tab_label(
            SessionBarTab("long title", "id", "Team", ("claude", "codex")),
            SessionBarTab("before", "id", "Team"),
            2,
        )
        self.assertEqual(tiny_tab, "l…")

    def test_controls_unicode_and_agent_noise_cannot_break_cell_budgets(self) -> None:
        tab = SessionBarTab(
            "測試\n\x1b[31m terminal",
            "id",
            "Team\tSession",
            ("unknown", "CODEX", "codex"),
        )

        for width in range(1, 35):
            with self.subTest(width=width):
                label = render_tab_label(tab, None, width)
                self.assertLessEqual(session_bar._cell_width(label), width)
                self.assertNotIn("\x1b", label)
        self.assertEqual(session_bar._ellipsize("測試", 3), "測…")
        self.assertEqual(session_bar._ellipsize("e\N{COMBINING ACUTE ACCENT}", 1), "é")
        self.assertEqual(session_bar._suffix(("unknown",), verbose=True), "")


class SessionBarAdapterTests(unittest.TestCase):
    def test_session_prefix_never_inherits_first_tab_highlight(self) -> None:
        first_active = Datum("Shell", 1, is_active=True)
        first_inactive = first_active._replace(is_active=False)
        second_active = Datum("Tests", 2, is_active=True)
        variables = {SESSION_ID_VAR: "id", SESSION_NAME_VAR: "Silver Seal"}
        boss = Boss(
            {
                1: NativeTab(Window(variables)),
                2: NativeTab(Window(variables)),
            }
        )
        draw_data = DrawData("top")
        first_drawer = RecordingDrawer()
        drawer_holder = [first_drawer]

        with (
            mock.patch.object(session_bar, "_kitty_boss", return_value=boss),
            mock.patch.object(
                session_bar,
                "_kitty_drawer",
                side_effect=lambda: drawer_holder[0],
            ),
            mock.patch(
                "kitty_workbench.session_bar.importlib.import_module",
                return_value=SimpleNamespace(as_rgb=lambda color: color),
            ),
        ):
            active_screen = Screen(Cursor(x=7, bg=draw_data.active_bg, fg=draw_data.active_fg))
            result = session_bar.draw_tab(
                draw_data,
                active_screen,
                first_active,
                7,
                40,
                1,
                False,
                ExtraData(None, second_active),
            )

            switched_drawer = RecordingDrawer()
            drawer_holder[0] = switched_drawer
            switched_screen = Screen(Cursor(x=7))
            session_bar.draw_tab(
                draw_data,
                switched_screen,
                first_inactive,
                7,
                40,
                1,
                False,
                ExtraData(None, second_active),
            )
            switched_screen.cursor.bg = draw_data.active_bg
            switched_screen.cursor.fg = draw_data.active_fg
            session_bar.draw_tab(
                draw_data,
                switched_screen,
                second_active,
                switched_screen.cursor.x,
                40,
                2,
                True,
                ExtraData(first_inactive),
            )

        self.assertEqual(result, 47)
        self.assertEqual(
            [
                (cast(Datum, call[2]).title, cast(Datum, call[2]).is_active)
                for call in first_drawer.calls
            ],
            [(" Silver Seal", False), ("󰓩 Shell", True)],
        )
        self.assertEqual(
            first_drawer.colors,
            [
                (draw_data.inactive_bg, draw_data.inactive_fg),
                (draw_data.active_bg, draw_data.active_fg),
            ],
        )
        self.assertEqual(
            [
                (cast(Datum, call[2]).title, cast(Datum, call[2]).is_active)
                for call in switched_drawer.calls
            ],
            [(" Silver Seal", False), ("󰓩 Shell", False), ("󰓩 Tests", True)],
        )
        self.assertEqual(
            switched_drawer.colors,
            [
                (draw_data.inactive_bg, draw_data.inactive_fg),
                (draw_data.inactive_bg, draw_data.inactive_fg),
                (draw_data.active_bg, draw_data.active_fg),
            ],
        )

    def test_incomplete_native_renderers_fall_back_to_one_decorated_tab(self) -> None:
        variables = {SESSION_ID_VAR: "id", SESSION_NAME_VAR: "Project"}
        boss = Boss({1: NativeTab(Window(variables))})
        drawer = RecordingDrawer()
        screen = Screen(Cursor(x=3))

        with (
            mock.patch.object(session_bar, "_kitty_boss", return_value=boss),
            mock.patch.object(session_bar, "_kitty_drawer", return_value=drawer),
        ):
            result = session_bar.draw_tab(
                DrawData("top"),
                screen,
                LegacyDatum("Shell", 1),
                3,
                30,
                1,
                True,
                ExtraData(None),
            )

        self.assertEqual(result, 33)
        self.assertEqual(len(drawer.calls), 1)
        self.assertIn(" Project", cast(LegacyDatum, drawer.calls[0][2]).title)

        incomplete_drawer = RecordingDrawer()
        with (
            mock.patch.object(session_bar, "_kitty_boss", return_value=boss),
            mock.patch.object(
                session_bar,
                "_kitty_drawer",
                return_value=incomplete_drawer,
            ),
        ):
            session_bar.draw_tab(
                object(),
                Screen(Cursor(x=3)),
                Datum("Shell", 1, is_active=True),
                3,
                30,
                1,
                True,
                ExtraData(None),
            )

        self.assertEqual(len(incomplete_drawer.calls), 1)
        self.assertIn(" Project", cast(Datum, incomplete_drawer.calls[0][2]).title)

    def test_native_color_resolution_rejects_incomplete_or_failed_theme_apis(self) -> None:
        incomplete = SimpleNamespace(tab_bg=lambda _tab: 1)
        complete = SimpleNamespace(tab_bg=lambda _tab: 1, tab_fg=lambda _tab: 2)

        self.assertIsNone(session_bar._native_tab_colors(object(), object()))
        self.assertIsNone(session_bar._native_tab_colors(incomplete, object()))
        with mock.patch(
            "kitty_workbench.session_bar.importlib.import_module",
            return_value=SimpleNamespace(
                as_rgb=mock.Mock(side_effect=RuntimeError("theme changed"))
            ),
        ):
            self.assertIsNone(session_bar._native_tab_colors(complete, object()))

    def test_native_adapter_uses_cached_metadata_and_preserves_kitty_draw_state(self) -> None:
        first = Datum("Shell", 1)
        second = Datum("Tests", 2)
        variables = {
            SESSION_ID_VAR: "session-id",
            SESSION_SLUG_VAR: "fallback-slug",
            SESSION_NAME_VAR: "Exact Name",
            APP_VAR: "nvim",
            AGENT_VAR: "claude",
        }
        boss = Boss(
            {
                1: NativeTab(Window(lambda: variables, child=Child(["-zsh"]))),
                2: NativeTab(
                    Window(
                        {SESSION_ID_VAR: "session-id", SESSION_NAME_VAR: "Exact Name"},
                        child=Child(["/opt/bin/codex-nightly"]),
                    )
                ),
            }
        )
        drawer = RecordingDrawer()
        screen = object()
        extra = ExtraData(first)

        with (
            mock.patch.object(session_bar, "_kitty_boss", return_value=boss),
            mock.patch.object(session_bar, "_kitty_drawer", return_value=drawer),
            mock.patch.object(builtins, "open", side_effect=AssertionError("render read a file")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("render spawned a process")),
        ):
            result = session_bar.draw_tab(
                DrawData("bottom"), screen, second, 17, 60, 2, True, extra
            )

        self.assertEqual(result, 41)
        call = drawer.calls[0]
        self.assertEqual(call[0], DrawData("bottom"))
        self.assertIs(call[1], screen)
        self.assertEqual(cast(Datum, call[2]).title, "󰓩 Tests 󰋙 Codex")
        self.assertEqual(call[3], 17)
        self.assertEqual(call[4:], (60, 2, True, extra))

    def test_adapter_marks_groups_unattached_tabs_and_metadata_failures_safely(self) -> None:
        tracked = Datum("Editor", 1)
        unowned = Datum("Scratch", 2)
        missing = Datum("Original", 3)
        boss = Boss(
            {
                1: NativeTab(Window({SESSION_ID_VAR: "id", SESSION_SLUG_VAR: "fallback-name"})),
                2: NativeTab(Window({})),
            }
        )
        drawer = RecordingDrawer()

        with (
            mock.patch.object(session_bar, "_kitty_boss", return_value=boss),
            mock.patch.object(session_bar, "_kitty_drawer", return_value=drawer),
        ):
            session_bar.draw_tab(object(), object(), tracked, 0, 60, 1, False, ExtraData(None))
            session_bar.draw_tab(object(), object(), unowned, 16, 60, 2, False, ExtraData(tracked))
            session_bar.draw_tab(object(), object(), missing, 38, 60, 3, True, ExtraData(unowned))

        self.assertIn(" fallback-name", cast(Datum, drawer.calls[0][2]).title)
        self.assertIn("󰌸 Unattached", cast(Datum, drawer.calls[1][2]).title)
        self.assertIs(drawer.calls[2][2], missing)

    def test_unstable_cached_apis_and_lazy_kitty_boundaries_fail_open(self) -> None:
        def broken_variables() -> object:
            raise RuntimeError("gone")

        datum = Datum("Original", 1)
        self.assertIsNone(session_bar._native_tab(object(), 1))
        self.assertIsNone(session_bar._native_tab(Boss({}, fail=True), 1))
        self.assertIsNone(session_bar._bar_tab(datum, Boss({1: NativeTab(fail=True)})))
        self.assertEqual(session_bar._mapping(broken_variables), {})
        self.assertEqual(session_bar._mapping("not a mapping"), {})
        self.assertIsNone(session_bar._command_application("'unterminated"))
        self.assertIsNone(session_bar._command_application(42))
        self.assertEqual(
            session_bar._cached_application(Window({}, child=Child("codex"))),
            "codex",
        )
        self.assertEqual(session_bar._cached_application(Window({}, title="Claude")), "claude")

        drawer = RecordingDrawer()
        with (
            mock.patch.object(session_bar, "_kitty_boss", side_effect=RuntimeError("gone")),
            mock.patch.object(session_bar, "_kitty_drawer", return_value=drawer),
        ):
            session_bar.draw_tab(object(), object(), datum, 0, 20, 1, True, ExtraData(None))
        self.assertIs(drawer.calls[0][2], datum)

    def test_current_tab_stays_decorated_when_previous_metadata_is_malformed(self) -> None:
        previous = cast(Datum, SimpleNamespace(title="Gone"))
        current = Datum("Editor", 1)
        boss = Boss({1: NativeTab(Window({SESSION_ID_VAR: "id", SESSION_NAME_VAR: "Project"}))})
        drawer = RecordingDrawer()

        with (
            mock.patch.object(session_bar, "_kitty_boss", return_value=boss),
            mock.patch.object(session_bar, "_kitty_drawer", return_value=drawer),
        ):
            session_bar.draw_tab(object(), object(), current, 23, 60, 2, True, ExtraData(previous))

        self.assertIn(" Project", cast(Datum, drawer.calls[0][2]).title)

    def test_lazy_kitty_imports_cache_only_the_drawer(self) -> None:
        boss = object()
        drawer = RecordingDrawer()
        modules = {
            "kitty.fast_data_types": SimpleNamespace(get_boss=lambda: boss),
            "kitty.tab_bar": SimpleNamespace(draw_tab_with_powerline=drawer),
        }
        session_bar._drawer = None
        with mock.patch(
            "kitty_workbench.session_bar.importlib.import_module",
            side_effect=lambda name: modules[name],
        ) as imported:
            self.assertIs(session_bar._kitty_boss(), boss)
            self.assertIs(session_bar._kitty_drawer(), drawer)
            self.assertIs(session_bar._kitty_drawer(), drawer)

        self.assertEqual(
            [call.args[0] for call in imported.call_args_list],
            ["kitty.fast_data_types", "kitty.tab_bar"],
        )
        session_bar._drawer = None

    def test_reload_clears_every_kitty_cache_and_repaints_each_native_bar(self) -> None:
        caches = [Cache(), Cache()]
        module = SimpleNamespace(
            load_custom_draw_tab=caches[0],
            load_custom_draw_tab_module=caches[1],
        )
        managers = [Manager(), Manager()]
        boss = ReloadBoss(managers)
        session_bar._drawer = RecordingDrawer()

        with mock.patch(
            "kitty_workbench.session_bar.importlib.import_module",
            return_value=module,
        ):
            session_bar.reload_session_bar(cast(SessionBarBoss, boss))

        self.assertEqual([cache.clears for cache in caches], [1, 1])
        self.assertEqual([manager.events for manager in managers], [["dirty", "update"]] * 2)
        self.assertEqual(boss.refreshes, 1)
        self.assertIsNone(session_bar._drawer)

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_real_kitty_runtime_loads_the_exact_custom_bar_entrypoint(self) -> None:
        script = (
            "import runpy,sys; "
            "loaded=runpy.run_path(sys.argv[1]); "
            "print(loaded['draw_tab'].__module__)"
        )
        result = subprocess.run(
            ["kitty", "+runpy", script, str(PROJECT / "integration" / "tab_bar.py")],
            cwd=PROJECT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "kitty_workbench.session_bar")

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_real_kitty_callback_uses_integer_before_and_neighbor_data(self) -> None:
        script = (
            "import sys; "
            f"sys.path.insert(0,{str(PROJECT)!r}); "
            "from types import SimpleNamespace as N; "
            "from kitty.tab_bar import ExtraData,TabBarData; "
            "import kitty_workbench.session_bar as s; "
            f"v={{'{SESSION_ID_VAR}':'id','{SESSION_NAME_VAR}':'Project'}}; "
            "w=N(user_vars=v,child=N(cmdline=('-zsh',)),title='Shell'); "
            "tabs={1:[w],2:[w]}; "
            "boss=N(tab_for_id=tabs.get); s._kitty_boss=lambda boss=boss:boss; "
            "seen=[]; "
            "s._kitty_drawer=lambda seen=seen:"
            "lambda d,sc,t,b,m,i,last,e:seen.append((t.title,b)) or 41; "
            "previous=TabBarData('Shell',tab_id=1); "
            "current=TabBarData('Tests',tab_id=2); "
            "extra=ExtraData(); extra.prev_tab=previous; "
            "extra.next_tab=None; extra.for_layout=False; "
            "result=s.draw_tab(N(),N(),current,17,60,2,True,extra); "
            "print(result,seen==[(s.TAB_ICON+' Tests',17)])"
        )
        result = subprocess.run(
            ["kitty", "+runpy", script],
            cwd=PROJECT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "41 True")

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_real_kitty_first_tab_keeps_session_segment_inactive(self) -> None:
        script = (
            "import sys; "
            f"sys.path.insert(0,{str(PROJECT)!r}); "
            "from types import SimpleNamespace as N; "
            "from kitty.tab_bar import ExtraData,TabBarData; "
            "import kitty_workbench.session_bar as s; "
            f"v={{'{SESSION_ID_VAR}':'id','{SESSION_NAME_VAR}':'Silver Seal'}}; "
            "w=N(user_vars=v,child=N(cmdline=('-zsh',)),title='Shell'); "
            "boss=N(tab_for_id={1:[w],2:[w]}.get); "
            "s._kitty_boss=lambda boss=boss:boss; "
            "current=TabBarData('Shell',is_active=True,tab_id=1); "
            "following=TabBarData('Tests',tab_id=2); "
            "extra=ExtraData(); extra.prev_tab=None; extra.next_tab=following; "
            "extra.for_layout=False; "
            "draw=N(tab_bg=lambda t:0x101010 if t.is_active else 0x202020,"
            "tab_fg=lambda t:0xf0f0f0 if t.is_active else 0x808080); "
            "screen=N(cursor=N(x=7,bg=0,fg=0)); seen=[]; "
            "drawer=lambda d,sc,t,b,m,i,last,e,seen=seen:"
            "(seen.append((t.title,t.is_active,sc.cursor.bg,sc.cursor.fg)),"
            "setattr(sc.cursor,'x',b+m),b+m)[-1]; "
            "s._kitty_drawer=lambda drawer=drawer:drawer; "
            "result=s.draw_tab(draw,screen,current,7,60,1,False,extra); "
            "print(result==67,len(seen)==2,seen[0][1] is False,"
            "seen[1][1] is True,seen[0][2]!=seen[1][2])"
        )
        result = subprocess.run(
            ["kitty", "+runpy", script],
            cwd=PROJECT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True True True True True")

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_real_entrypoint_loads_with_pre_feature_model_cached_in_kitty(self) -> None:
        entrypoint = PROJECT / "integration" / "tab_bar.py"
        script = (
            "import runpy,sys; "
            f"sys.path.insert(0,{str(PROJECT)!r}); "
            "import kitty_workbench.model as model; "
            "del model.AGENT_VAR; del model.SESSION_NAME_VAR; "
            "sys.modules.pop('kitty_workbench.session_bar',None); "
            "loaded=runpy.run_path(sys.argv[1]); "
            "print(loaded['draw_tab'].__module__)"
        )
        result = subprocess.run(
            ["kitty", "+runpy", script, str(entrypoint)],
            cwd=PROJECT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "kitty_workbench.session_bar")


if __name__ == "__main__":
    unittest.main()
