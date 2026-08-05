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
from kitty_workbench.model import AGENT_VAR, SESSION_ID_VAR, SESSION_NAME_VAR, SESSION_SLUG_VAR
from kitty_workbench.session_bar import SessionBarBoss, SessionBarTab, render_tab_label

PROJECT = Path(__file__).parents[1]


class Datum(NamedTuple):
    title: str
    tab_id: int


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


class RecordingDrawer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object, object, int, int, bool, object]] = []

    def __call__(
        self,
        draw_data: object,
        screen: object,
        tab: object,
        before: object,
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
        SessionBarTab("tests", "research", "Research Work", ("claude",)),
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
        self.assertIn("○ Unattached", render_tab_label(unattached, other, 60))
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


class SessionBarAdapterTests(unittest.TestCase):
    def test_native_adapter_uses_cached_metadata_and_preserves_kitty_draw_state(self) -> None:
        first = Datum("Shell", 1)
        second = Datum("Tests", 2)
        variables = {
            SESSION_ID_VAR: "session-id",
            SESSION_SLUG_VAR: "fallback-slug",
            SESSION_NAME_VAR: "Exact Name",
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
        extra = object()

        with (
            mock.patch.object(session_bar, "_kitty_boss", return_value=boss),
            mock.patch.object(session_bar, "_kitty_drawer", return_value=drawer),
            mock.patch.object(builtins, "open", side_effect=AssertionError("render read a file")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("render spawned a process")),
        ):
            result = session_bar.draw_tab(
                DrawData("bottom"), screen, second, first, 60, 2, True, extra
            )

        self.assertEqual(result, 41)
        call = drawer.calls[0]
        self.assertEqual(call[0], DrawData("bottom"))
        self.assertIs(call[1], screen)
        self.assertEqual(cast(Datum, call[2]).title, "󰓩 Tests ◇ Codex")
        self.assertIs(call[3], first)
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
            session_bar.draw_tab(object(), object(), tracked, None, 60, 1, False, object())
            session_bar.draw_tab(object(), object(), unowned, tracked, 60, 2, False, object())
            session_bar.draw_tab(object(), object(), missing, unowned, 60, 3, True, object())

        self.assertIn(" fallback-name", cast(Datum, drawer.calls[0][2]).title)
        self.assertIn("○ Unattached", cast(Datum, drawer.calls[1][2]).title)
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
        self.assertIsNone(session_bar._command_agent("'unterminated"))
        self.assertIsNone(session_bar._command_agent(42))
        self.assertEqual(
            session_bar._cached_agent(Window({}, child=Child("codex"))),
            "codex",
        )
        self.assertEqual(session_bar._cached_agent(Window({}, title="Claude")), "claude")

        drawer = RecordingDrawer()
        with (
            mock.patch.object(session_bar, "_kitty_boss", side_effect=RuntimeError("gone")),
            mock.patch.object(session_bar, "_kitty_drawer", return_value=drawer),
        ):
            session_bar.draw_tab(object(), object(), datum, None, 20, 1, True, object())
        self.assertIs(drawer.calls[0][2], datum)

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
