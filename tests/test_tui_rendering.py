from __future__ import annotations

import curses
import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from unittest import mock

from kisesh.kitty_client import LiveTab
from kisesh.model import KittyWindow
from kisesh.service import (
    KiSeshService,
    UnownedTabsAction,
    UnownedTabsDecision,
)
from kisesh.store import SessionStore, StoredSession
from kisesh.tui import SessionManager
from tests.fakes import DelayedCloseKitty, FakeKitty
from tests.render_fixture import Canvas, StaticService, rendered_manager


class RecordingService(StaticService):
    """Track manager service calls while retaining complete fixture behavior."""

    def __init__(self) -> None:
        """Initialize deterministic rows and action ledgers."""
        super().__init__()
        self.opened: list[str] = []
        self.open_decisions: list[UnownedTabsDecision | None] = []
        self.timeline: list[str] = []
        self.closed: list[str] = []
        self.unarchived: list[str] = []
        self.added: list[str] = []
        self.detached: list[str] = []
        self.copied: list[str] = []
        self.removed: list[str] = []
        self.renamed_tabs: list[tuple[str, int, str]] = []

    def open(
        self,
        slug_or_id: str,
        unowned_decision: UnownedTabsDecision | None = None,
    ) -> StoredSession:
        """Record and resolve a focus or restore request."""
        self.opened.append(slug_or_id)
        self.open_decisions.append(unowned_decision)
        self.timeline.append("open")
        return self._stored(slug_or_id)

    def save_and_close(self, slug_or_id: str) -> StoredSession:
        """Record a successful save-close transition and update live state."""
        self.closed.append(slug_or_id)
        self.timeline.append("save-close")
        return super().save_and_close(slug_or_id)

    def unarchive(self, identifier: str) -> StoredSession:
        """Record and apply an unarchive request."""
        self.unarchived.append(identifier)
        stored = self._stored(identifier)
        stored.manifest.status = "active"
        return stored

    def add_current_tab(self, identifier: str) -> StoredSession:
        """Record the session selected for tab attachment."""
        self.added.append(identifier)
        return self._stored(identifier)

    def detach_current_tab(self, identifier: str) -> StoredSession:
        """Record the session selected for tab detachment."""
        self.detached.append(identifier)
        return self._stored(identifier)

    def copy_current_tab(self, identifier: str) -> StoredSession:
        """Record the saved session selected for a safe tab copy."""
        self.copied.append(identifier)
        return self._stored(identifier)

    def rename_tab(self, identifier: str, tab_index: int, new_title: str) -> StoredSession:
        """Record and apply one tab-title change."""
        self.renamed_tabs.append((identifier, tab_index, new_title))
        return super().rename_tab(identifier, tab_index, new_title)

    def remove(self, identifier: str) -> Path:
        """Record and remove one inactive session from visible rows."""
        self.removed.append(identifier)
        self._rows = [row for row in self._rows if row.stored.manifest.id != identifier]
        return Path("/tmp/trash") / identifier


class ScriptedCanvas(Canvas):
    """A terminal canvas with deterministic input for interactive prompt tests."""

    def __init__(self, height: int, width: int, keys: Iterable[object]) -> None:
        """Initialize a strict canvas with an ordered input script."""
        super().__init__(height, width)
        self.keys = list(keys)
        self.cursor = (0, 0)

    def get_wch(self) -> object:
        """Return the next scripted key or reject an unexpected read."""
        if not self.keys:
            raise AssertionError("the interactive prompt requested unexpected input")
        return self.keys.pop(0)

    def move(self, y: int, x: int) -> None:
        """Move the simulated prompt cursor."""
        self.cursor = (y, x)

    def clrtoeol(self) -> None:
        """Clear the prompt row from the simulated cursor onward."""
        y, x = self.cursor
        for column in range(x, self.width):
            self.cells[y][column] = " "
            self.styles[y][column] = 0


class TrackingSearchManager(SessionManager):
    """Record each incremental query and its immediately visible rows."""

    def __init__(self, service: StaticService) -> None:
        """Initialize the manager and query-state ledger."""
        super().__init__(service)
        self.search_states: list[tuple[str, list[str]]] = []

    def _update_query(self, value: str) -> None:
        """Apply and record one incremental search update."""
        super()._update_query(value)
        self.search_states.append((value, [row.stored.manifest.name for row in self.filtered]))


class TuiRenderingTests(unittest.TestCase):
    def test_sample_manager_matches_reviewed_render(self) -> None:
        actual, _, _ = rendered_manager()
        golden = Path(__file__).parent / "golden" / "session-manager-100x16.txt"
        self.assertMultiLineEqual(actual, golden.read_text(encoding="utf-8"))

    def test_selected_saved_session_expands_into_two_line_tab_contents(self) -> None:
        rendered, canvas, palette = rendered_manager()
        lines = rendered.splitlines()

        editor_row = next(index for index, line in enumerate(lines) if "├─ editor" in line)
        shell_row = next(index for index, line in enumerate(lines) if "└─ shell" in line)
        self.assertEqual(lines[editor_row + 1].strip(), "│  • ✻ Claude ↻,  Vim ↻")
        self.assertEqual(lines[shell_row + 1].strip(), " zsh · last: git status")
        self.assertIn("2 panes · splits · focused", lines[editor_row])
        self.assertIn("1 pane · tall", lines[shell_row])
        self.assertNotIn("├─ agents", rendered)
        claude_x = lines[editor_row + 1].index("✻")
        self.assertEqual(
            canvas.styles[editor_row + 1][claude_x],
            palette.accent | curses.A_BOLD,
        )

    def test_selected_live_session_uses_current_agents_focus_and_attention(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        manager.selected = 0
        _, _, palette = rendered_manager()
        manager.palette = palette
        screen = Canvas(18, 100)

        manager._draw(screen)
        rendered = screen.render()
        lines = rendered.splitlines()

        self.assertIn("├─ agents · 2 panes · splits · focused", rendered)
        self.assertIn("│  • ✻ Claude, 󰋙 Codex", rendered)
        self.assertIn("├─ editor + tests · 3 panes · tall", rendered)
        self.assertIn(" Vim,  pytest,  zsh · last: git status", rendered)
        self.assertIn("└─ monitor · 1 pane · stack", rendered)
        self.assertIn("󰍛 top !", rendered)
        self.assertNotIn("↻", rendered)
        navigation = next(line for line in lines if "j/k" in line)
        self.assertIn(" n  new", navigation)
        self.assertIn(" x  save+close", navigation)
        agent_row = next(index for index, line in enumerate(lines) if "󰋙 Codex" in line)
        codex_x = lines[agent_row].index("󰋙")
        self.assertEqual(
            screen.styles[agent_row][codex_x],
            palette.good | curses.A_BOLD,
        )
        attention_row = next(index for index, line in enumerate(lines) if "󰍛 top !" in line)
        attention_x = lines[attention_row].index("top")
        self.assertEqual(screen.styles[attention_row][attention_x], palette.warning)

    def test_stacked_windows_are_not_described_as_simultaneously_visible_panes(self) -> None:
        service = StaticService()
        stack = service._rows[0].live_tabs[-1]
        windows: list[KittyWindow] = [
            {
                "id": 100 + index,
                "title": "zsh",
                "foreground_processes": [{"cmdline": ["-zsh"]}],
                "is_active": index == 7,
                "is_focused": index == 7,
            }
            for index in range(8)
        ]
        stack.windows = windows
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 0
        manager.palette = rendered_manager()[2]
        screen = Canvas(18, 100)

        manager._draw(screen)
        rendered = screen.render()

        self.assertIn("└─ monitor · 1 visible + 7 stacked · stack", rendered)
        lines = rendered.splitlines()
        monitor_row = next(index for index, line in enumerate(lines) if "└─ monitor" in line)
        self.assertEqual(lines[monitor_row + 1].count("zsh"), 8)
        self.assertNotIn("monitor · 8 panes", rendered)

    def test_preview_follows_vim_selection_without_expanding_other_sessions(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        manager.selected = 2
        manager.palette = rendered_manager()[2]
        screen = Canvas(16, 100)

        manager._draw(screen)
        archived = screen.render()
        self.assertIn("└─ notes · 1 pane · splits", archived)
        self.assertNotIn("├─ editor", archived)
        self.assertNotIn("├─ agents", archived)

        manager._handle_navigation_key("k")
        manager._draw(screen)
        saved = screen.render()
        self.assertIn("├─ editor", saved)
        self.assertNotIn("└─ notes", saved)

    def test_preview_is_height_aware_and_clips_long_saved_details(self) -> None:
        tiny, _, _ = rendered_manager(48, 8)
        self.assertNotIn("├─", tiny)
        self.assertNotIn("└─", tiny)

        service = StaticService()
        context = service._rows[1].context
        self.assertIsNotNone(context)
        assert context is not None
        context["tabs"][0]["title"] = "editor-" + "very-long-" * 20
        context["tabs"][0]["panes"][0]["last_command"] = "command " * 50
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        manager.palette = rendered_manager()[2]
        screen = Canvas(10, 60)

        manager._draw(screen)
        rendered = screen.render()
        self.assertIn("…", rendered)
        self.assertIn("2 panes", rendered)
        self.assertTrue(all(len(line) <= 60 for line in rendered.splitlines()))

    def test_empty_and_summary_only_tabs_have_honest_preview_fallbacks(self) -> None:
        service = StaticService()
        context = service._rows[1].context
        self.assertIsNotNone(context)
        assert context is not None
        context["tabs"][0]["panes"] = []
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        manager.palette = rendered_manager()[2]
        screen = Canvas(12, 100)

        manager._draw(screen)
        self.assertIn("empty tab", screen.render())

        service._rows[1].context = None
        service._rows[1].stored.manifest.summary.tab_titles = ["known tab"]
        manager._refresh()
        manager._draw(screen)
        summary = screen.render()
        self.assertIn("known tab · snapshot", summary)
        self.assertIn("pane details unavailable", summary)

        service._rows[1].stored.manifest.summary.tab_count = 0
        service._rows[1].stored.manifest.summary.tab_titles = []
        manager._refresh()
        manager._draw(screen)
        self.assertIn("└─ no tabs captured", screen.render())

    def test_rendering_stays_inside_practical_terminal_sizes(self) -> None:
        for width, height in ((48, 8), (60, 10), (80, 12), (120, 24)):
            with self.subTest(width=width, height=height):
                rendered, _, _ = rendered_manager(width, height)
                lines = rendered.splitlines()
                self.assertLessEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width for line in lines))

    def test_right_side_pane_width_keeps_the_session_table_readable(self) -> None:
        rendered, _, _ = rendered_manager(64, 30)

        self.assertIn("SESSIONS", rendered)
        self.assertIn("TABS", rendered)
        self.assertIn("PANES", rendered)
        self.assertIn("CREATED", rendered)
        self.assertIn("MODIFIED", rendered)
        self.assertIn("JAX Agents", rendered)
        self.assertIn("Main Vault", rendered)

    def test_selection_and_hint_keys_are_visually_distinct(self) -> None:
        _, canvas, palette = rendered_manager()
        self.assertEqual(canvas.styles[4][2], palette.selected)
        self.assertEqual(canvas.styles[14][2], palette.selected)
        self.assertEqual(canvas.styles[14][7], palette.muted)

    def test_archived_sessions_render_in_a_distinct_lower_section(self) -> None:
        rendered, _, _ = rendered_manager()
        self.assertLess(rendered.index("SESSIONS"), rendered.index("JAX Agents"))
        self.assertLess(rendered.index("Dotfiles"), rendered.index("ARCHIVED"))
        self.assertLess(rendered.index("ARCHIVED"), rendered.index("Main Vault"))

    def test_too_small_terminal_has_a_clear_fallback(self) -> None:
        rendered, _, _ = rendered_manager(47, 7)
        self.assertEqual(rendered, "KiSesh needs at least 48x8 cells\n")

    def test_vim_navigation_and_open_keys_drive_the_selected_session(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        screen = Canvas(16, 100)

        manager._handle_key(screen, "G")
        self.assertEqual(manager.selected, 2)
        manager._handle_key(screen, "g")
        manager._handle_key(screen, "j")
        self.assertEqual(manager.selected, 1)
        manager._handle_key(screen, "k")
        self.assertEqual(manager.selected, 0)
        manager._handle_key(screen, "\x04")
        self.assertEqual(manager.selected, 2)
        manager._handle_key(screen, "\x15")
        self.assertEqual(manager.selected, 0)

        selected_id = manager.filtered[0].stored.manifest.id
        self.assertEqual(manager._handle_key(screen, "l"), 0)
        self.assertEqual(service.opened, [selected_id])
        self.assertEqual(manager._handle_key(screen, " "), 0)
        self.assertEqual(service.opened, [selected_id, selected_id])
        self.assertEqual(manager._handle_key(screen, "h"), 0)

    def test_resident_panel_hides_without_exiting_and_wakes_with_fresh_state(self) -> None:
        service = RecordingService()
        hidden: list[bool] = []
        manager = SessionManager(
            service,
            on_dismiss=lambda: hidden.append(True),
        )
        manager._refresh()
        manager.palette = rendered_manager()[2]
        screen = Canvas(16, 100)

        manager._draw(screen)
        self.assertIn("q  hide", screen.render())
        self.assertIsNone(manager._handle_key(screen, "q"))
        self.assertEqual(hidden, [True])

        service._rows = service._rows[:1]
        self.assertIsNone(manager._handle_key(screen, "\x07"))
        self.assertEqual(len(manager.filtered), 1)
        self.assertEqual(manager._handle_key(screen, "Q"), 0)

    def test_opening_from_resident_panel_does_not_race_focus_loss_with_a_toggle(self) -> None:
        service = RecordingService()
        hidden: list[bool] = []

        def dismiss() -> None:
            """Record an attempted resident-panel hide."""
            hidden.append(True)
            service.timeline.append("hide")

        manager = SessionManager(
            service,
            on_dismiss=dismiss,
        )
        manager._refresh()
        screen = Canvas(16, 100)
        selected_id = manager.filtered[0].stored.manifest.id

        self.assertIsNone(manager._handle_key(screen, " "))

        self.assertEqual(service.opened, [selected_id])
        self.assertEqual(hidden, [])
        self.assertEqual(service.timeline, ["open"])

    def test_unowned_tab_modal_is_complete_and_routes_attach(self) -> None:
        service = RecordingService()
        service.unowned_count = 2
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(16, 100, ["ignored", "A"])
        selected = manager.filtered[manager.selected]

        self.assertEqual(manager._handle_key(screen, " "), 0)
        self.assertEqual(service.opened, [selected.stored.manifest.id])
        self.assertEqual(
            service.open_decisions,
            [UnownedTabsDecision(UnownedTabsAction.ATTACH)],
        )
        top = 4
        left = 14
        width = 72
        bottom = top + 6
        self.assertEqual(
            "".join(screen.cells[top][left : left + width]),
            "╭" + "─" * (width - 2) + "╮",
        )
        self.assertEqual(
            "".join(screen.cells[bottom][left : left + width]),
            "╰" + "─" * (width - 2) + "╯",
        )
        for y in range(top + 1, bottom):
            self.assertEqual(screen.cells[y][left], "│")
            self.assertEqual(screen.cells[y][left + width - 1], "│")
        rendered = screen.render()
        self.assertIn("Unowned tabs · 2", rendered)
        self.assertIn("attach to Dotfiles", rendered)
        self.assertIn("name + save separately", rendered)
        self.assertIn("discard tabs without saving", rendered)
        self.assertIn("cancel; change nothing", rendered)

    def test_new_session_modal_groups_all_unowned_tabs_without_adding_a_blank_tab(self) -> None:
        service = RecordingService()
        service.unowned_count = 3
        manager = SessionManager(service)
        manager._refresh()
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(16, 100, [*list("Research"), "\n", "a"])

        manager._handle_key(screen, "n")

        decision = UnownedTabsDecision(UnownedTabsAction.ATTACH)
        self.assertEqual(service.unowned_creations, [("Research", decision)])
        self.assertEqual(service._rows[-1].stored.manifest.name, "Research")
        self.assertEqual(manager.message, "created Research")
        rendered = screen.render()
        self.assertIn("New session · 3 unowned tabs", rendered)
        self.assertIn("use all tabs for Research", rendered)
        self.assertIn("save separately; start fresh shell", rendered)
        self.assertIn("discard; start fresh shell", rendered)
        top, left, width = 4, 14, 72
        self.assertEqual(
            "".join(screen.cells[top][left : left + width]),
            "╭" + "─" * (width - 2) + "╮",
        )
        self.assertEqual(
            "".join(screen.cells[top + 6][left : left + width]),
            "╰" + "─" * (width - 2) + "╯",
        )

    def test_new_session_from_attached_window_skips_unowned_tab_modal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            kitty = FakeKitty()
            store = SessionStore(Path(temporary) / "data")
            service = KiSeshService(store, kitty)
            current = service.create_from_active("Current Work")
            fresh_shell = LiveTab(
                1,
                9,
                1,
                "Fresh Shell",
                "splits",
                [{"id": 13, "cwd": "/tmp/project", "user_vars": {}}],
            )
            kitty.next_open_tab = fresh_shell
            manager = SessionManager(service)
            manager._refresh()
            manager.palette = rendered_manager()[2]
            screen = ScriptedCanvas(16, 100, [*list("New Direction"), "\n"])

            manager._handle_key(screen, "n")

            created = next(
                stored for stored in store.list() if stored.manifest.name == "New Direction"
            )
            self.assertEqual(kitty.tab.session_id(), current.manifest.id)
            self.assertEqual(fresh_shell.session_id(), created.manifest.id)
            self.assertEqual(manager.message, "created New Direction")
            self.assertNotIn("New session ·", screen.render())

    def test_new_session_can_preserve_tabs_under_the_default_name_and_start_fresh(self) -> None:
        service = RecordingService()
        service.unowned_count = 2
        service.unowned_suggested_name = "Amber Badger"
        manager = SessionManager(service)
        manager._refresh()
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(16, 100, [*list("Clean Room"), "\n", "s", "\n"])

        manager._handle_key(screen, "n")

        self.assertEqual(
            service.unowned_creations,
            [
                (
                    "Clean Room",
                    UnownedTabsDecision(
                        UnownedTabsAction.SAVE_SEPARATELY,
                        "Amber Badger",
                    ),
                )
            ],
        )
        self.assertIn("save tabs as · C-u clears> Amber Badger", screen.render())

    def test_canceling_new_session_resolution_preserves_tabs_and_session_list(self) -> None:
        service = RecordingService()
        service.unowned_count = 2
        manager = SessionManager(service)
        manager._refresh()
        original_ids = [row.stored.manifest.id for row in service._rows]
        screen = ScriptedCanvas(16, 100, [*list("Cancelled"), "\n", "q"])

        manager._handle_key(screen, "n")

        self.assertEqual(service.unowned_creations, [])
        self.assertEqual([row.stored.manifest.id for row in service._rows], original_ids)
        self.assertEqual(manager.message, "create cancelled; tabs unchanged")

    def test_unowned_tab_modal_remains_complete_on_narrow_terminals(self) -> None:
        for height, width in ((8, 48), (10, 60)):
            service = RecordingService()
            service.unowned_count = 2
            manager = SessionManager(service)
            manager._refresh()
            manager.palette = rendered_manager()[2]
            screen = ScriptedCanvas(height, width, ["q"])

            with self.subTest(height=height, width=width):
                self.assertIsNone(manager._handle_key(screen, "l"))
                box_width = width - 2
                top = (height - 7) // 2
                left = 1
                bottom = top + 6
                self.assertEqual(
                    "".join(screen.cells[top][left : left + box_width]),
                    "╭" + "─" * (box_width - 2) + "╮",
                )
                self.assertEqual(
                    "".join(screen.cells[bottom][left : left + box_width]),
                    "╰" + "─" * (box_width - 2) + "╯",
                )
                for y in range(top + 1, bottom):
                    self.assertEqual(screen.cells[y][left], "│")
                    self.assertEqual(screen.cells[y][left + box_width - 1], "│")
                self.assertIn("discard tabs without saving", screen.render())
                self.assertEqual(service.opened, [])

    def test_unowned_tabs_can_be_saved_under_an_edited_suggested_name(self) -> None:
        service = RecordingService()
        service.unowned_count = 2
        service.unowned_suggested_name = "Amber Badger"
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(
            16,
            100,
            ["s", "\x15", *list("Research notes"), "\n"],
        )

        self.assertEqual(manager._handle_key(screen, " "), 0)

        self.assertEqual(
            service.open_decisions,
            [
                UnownedTabsDecision(
                    UnownedTabsAction.SAVE_SEPARATELY,
                    "Research notes",
                )
            ],
        )
        self.assertIn("save tabs as · C-u clears> Research notes", screen.render())

    def test_random_unowned_name_can_be_accepted_unchanged(self) -> None:
        service = RecordingService()
        service.unowned_count = 2
        service.unowned_suggested_name = "Amber Badger"
        manager = SessionManager(service)
        manager._refresh()
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(16, 100, ["s", "\n"])

        self.assertEqual(manager._handle_key(screen, "l"), 0)

        self.assertEqual(
            service.open_decisions,
            [
                UnownedTabsDecision(
                    UnownedTabsAction.SAVE_SEPARATELY,
                    "Amber Badger",
                )
            ],
        )
        self.assertIn("save tabs as · C-u clears> Amber Badger", screen.render())

    def test_blank_unowned_name_or_declined_discard_cancels_without_changes(self) -> None:
        for keys in (["s", "\x15", "\n"], ["d", "n"]):
            service = RecordingService()
            service.unowned_count = 3
            manager = SessionManager(service)
            manager._refresh()
            manager.palette = rendered_manager()[2]
            screen = ScriptedCanvas(16, 100, keys)

            with self.subTest(keys=keys):
                self.assertIsNone(manager._handle_key(screen, "l"))
                self.assertEqual(service.opened, [])
                self.assertEqual(service.open_decisions, [])
                self.assertEqual(manager.message, "open cancelled; tabs unchanged")

    def test_confirmed_discard_is_forwarded_as_an_explicit_switch_decision(self) -> None:
        service = RecordingService()
        service.unowned_count = 3
        manager = SessionManager(service)
        manager._refresh()
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(16, 100, ["d", "y"])

        self.assertEqual(manager._handle_key(screen, "l"), 0)

        self.assertEqual(
            service.open_decisions,
            [UnownedTabsDecision(UnownedTabsAction.DISCARD)],
        )
        self.assertIn("close 3 unowned tab(s) without saving? [y/N]", screen.render())

    def test_canceling_unowned_tab_modal_changes_nothing_and_keeps_manager_open(self) -> None:
        service = RecordingService()
        service.unowned_count = 3
        manager = SessionManager(service)
        manager._refresh()
        screen = ScriptedCanvas(16, 100, ["\x1b"])

        self.assertIsNone(manager._handle_key(screen, "l"))

        self.assertEqual(service.opened, [])
        self.assertEqual(service.open_decisions, [])
        self.assertEqual(manager.message, "open cancelled; tabs unchanged")

    def test_save_close_requires_confirmation_and_turns_live_row_into_saved(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        live_id = manager.filtered[0].stored.manifest.id

        manager._handle_key(ScriptedCanvas(16, 100, ["n"]), "x")
        self.assertEqual(service.closed, [])
        self.assertTrue(manager.filtered[0].live)

        manager._handle_key(ScriptedCanvas(16, 100, ["y"]), "x")

        self.assertEqual(service.closed, [live_id])
        closed_row = next(row for row in manager.filtered if row.stored.manifest.id == live_id)
        self.assertFalse(closed_row.live)
        self.assertEqual(manager.message, "saved and closed JAX Agents")

    def test_real_save_close_waits_for_saved_row_and_preserves_source_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(Path(temporary) / "data")
            kitty = DelayedCloseKitty(stale_reads_after_close=2)
            service = KiSeshService(store, kitty)
            source = service.create_from_active("Dotfiles")
            kitty.tab.is_focused = False
            kitty.tab.is_active = False
            closing = store.create("Scratch", "/tmp/scratch")
            closing_tab = LiveTab(
                1,
                8,
                1,
                "Shell",
                "splits",
                [{"id": 12, "cwd": "/tmp/scratch", "user_vars": {}}],
            )
            kitty.stamp_tab(closing_tab, closing.manifest)
            unrelated = store.create("Work", "/tmp/work")
            unrelated_tab = LiveTab(
                1,
                9,
                2,
                "Vim",
                "splits",
                [{"id": 13, "cwd": "/tmp/work", "user_vars": {}}],
                is_focused=True,
                is_active=True,
            )
            kitty.stamp_tab(unrelated_tab, unrelated.manifest)
            kitty.extra_tabs.extend((closing_tab, unrelated_tab))
            manager = SessionManager(service)
            manager._refresh()
            manager.selected = next(
                index
                for index, row in enumerate(manager.filtered)
                if row.stored.manifest.id == closing.manifest.id
            )

            with mock.patch("kisesh.service.time.sleep") as pause:
                manager._handle_key(ScriptedCanvas(16, 100, ["y"]), "x")

            closed_row = next(
                row for row in manager.filtered if row.stored.manifest.id == closing.manifest.id
            )
            manager.palette = rendered_manager()[2]
            screen = Canvas(16, 100)
            manager._draw(screen)
            rendered_row = next(line for line in screen.render().splitlines() if "Scratch" in line)

        self.assertEqual(pause.call_count, 2)
        self.assertFalse(closed_row.live)
        self.assertIn("○ Scratch", rendered_row)
        self.assertEqual(
            kitty.activated_sessions[-1],
            (source.manifest.id, kitty.tab.tab_id),
        )

    def test_search_filters_after_each_keystroke_without_waiting_for_enter(self) -> None:
        manager = TrackingSearchManager(StaticService())
        manager._refresh()
        screen = ScriptedCanvas(16, 100, ["v", "a", "u", "l", "t", "\n"])

        manager._handle_key(screen, "/")

        typed = manager.search_states[:5]
        self.assertEqual([query for query, _ in typed], ["v", "va", "vau", "vaul", "vault"])
        self.assertTrue(all(names == ["Main Vault"] for _, names in typed))
        self.assertEqual(manager.query, "vault")
        self.assertEqual(screen.keys, [])

    def test_unarchive_action_moves_selection_out_of_archived_section(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = len(manager.filtered) - 1
        archived_id = manager.filtered[manager.selected].stored.manifest.id
        screen = Canvas(16, 100)

        manager._handle_key(screen, "u")

        self.assertEqual(service.unarchived, [archived_id])
        self.assertEqual(manager.archived_rows, [])
        self.assertEqual(len(manager.active_rows), 3)

    def test_remove_confirms_and_handles_saved_and_archived_lists(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        selected_id = manager.filtered[manager.selected].stored.manifest.id
        selected_name = manager.filtered[manager.selected].stored.manifest.name
        archived_id = manager.archived_rows[0].stored.manifest.id

        cancelled = ScriptedCanvas(16, 100, ["n"])
        manager._handle_key(cancelled, "D")
        self.assertEqual(service.removed, [])
        self.assertIn(selected_id, [row.stored.manifest.id for row in manager.filtered])
        self.assertIn(f"remove {selected_name} to recoverable trash? [y/N]", cancelled.render())

        confirmed = ScriptedCanvas(16, 100, ["y"])
        manager._handle_key(confirmed, "D")

        self.assertEqual(service.removed, [selected_id])
        self.assertNotIn(selected_id, [row.stored.manifest.id for row in manager.filtered])
        self.assertIn("recoverable trash", manager.message)

        manager.selected = len(manager.filtered) - 1
        archived_confirmation = ScriptedCanvas(16, 100, ["y"])
        manager._handle_key(archived_confirmation, "D")

        self.assertEqual(service.removed, [selected_id, archived_id])
        self.assertEqual(manager.archived_rows, [])

    def test_remove_key_is_explicit_and_never_detaches_a_saved_session(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        screen = Canvas(16, 100)

        manager._handle_key(screen, "d")

        self.assertEqual(service.detached, [])
        self.assertEqual(
            manager.message,
            "remove uses Shift+D; detach is only for live sessions",
        )

        manager.selected = 0
        manager._handle_key(screen, "D")

        self.assertEqual(service.removed, [])
        self.assertEqual(
            manager.message,
            "press x to save and close this live session before removing it",
        )

    def test_archived_selection_shows_an_explicit_unarchive_hint(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        manager.selected = len(manager.filtered) - 1
        manager.palette = rendered_manager()[2]
        screen = Canvas(16, 100)

        manager._draw(screen)

        self.assertIn("unarchive", screen.render())

    def test_membership_keys_target_the_selected_session(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        screen = Canvas(16, 100)

        live_id = manager.filtered[0].stored.manifest.id
        manager._handle_key(screen, "a")
        manager._handle_key(screen, "d")

        manager.selected = 1
        saved_id = manager.filtered[manager.selected].stored.manifest.id
        manager._handle_key(screen, "c")

        self.assertEqual(service.added, [live_id])
        self.assertEqual(service.detached, [live_id])
        self.assertEqual(service.copied, [saved_id])

    def test_shift_r_renames_a_chosen_saved_tab_through_prefilled_modals(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        manager.palette = rendered_manager()[2]
        selected_id = manager.filtered[1].stored.manifest.id
        screen = ScriptedCanvas(
            16,
            100,
            ["j", "\n", "\x15", *list("Build logs"), "\n"],
        )

        manager._handle_key(screen, "R")

        self.assertEqual(service.renamed_tabs, [(selected_id, 1, "Build logs")])
        context = service._rows[1].context
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            [tab["title"] for tab in context["tabs"]],
            ["editor", "Build logs"],
        )
        self.assertEqual(manager.message, "renamed tab in Dotfiles to Build logs")
        rendered = screen.render()
        self.assertIn("Rename tab · 2/2", rendered)
        self.assertIn("> Build logs", rendered)
        top, left, height, width = 3, 16, 6, 68
        self.assertEqual(
            "".join(screen.cells[top][left : left + width]),
            "╭" + "─" * (width - 2) + "╮",
        )
        self.assertEqual(
            "".join(screen.cells[top + height - 1][left : left + width]),
            "╰" + "─" * (width - 2) + "╯",
        )
        self.assertEqual(screen.styles[top + 1][left + 2], manager.palette.accent | curses.A_BOLD)

    def test_live_tab_picker_starts_at_focus_and_rejects_blank_before_saving(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 0
        manager.palette = rendered_manager()[2]
        selected_id = manager.filtered[0].stored.manifest.id
        screen = ScriptedCanvas(
            16,
            100,
            ["\n", "\x15", "\n", *list("Agent desk"), "\n"],
        )

        manager._handle_key(screen, "R")

        self.assertEqual(service.renamed_tabs, [(selected_id, 0, "Agent desk")])
        self.assertEqual(service._rows[0].live_tabs[0].title, "Agent desk")
        self.assertEqual(screen.keys, [])

    def test_tab_rename_cancel_and_unchanged_paths_never_mutate_a_session(self) -> None:
        for keys, expected in (
            (["\x1b"], "cancelled"),
            (["\n", "\x1b"], "cancelled"),
            (["\n", "\n"], "unchanged"),
        ):
            with self.subTest(keys=keys):
                service = RecordingService()
                manager = SessionManager(service)
                manager._refresh()
                manager.selected = 1
                manager.palette = rendered_manager()[2]
                screen = ScriptedCanvas(16, 100, keys)

                manager._handle_key(screen, "R")

                self.assertEqual(service.renamed_tabs, [])
                self.assertIn(expected, manager.message)

    def test_tab_picker_supports_vim_navigation_and_complete_small_rendering(self) -> None:
        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 0
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(
            8,
            48,
            [
                "k",
                "j",
                "G",
                "g",
                "\x04",
                "\x15",
                curses.KEY_DOWN,
                curses.KEY_UP,
                curses.KEY_F1,
                "q",
            ],
        )

        manager._handle_key(screen, "R")

        self.assertEqual(service.renamed_tabs, [])
        self.assertEqual(manager.message, "tab rename cancelled")
        rendered = screen.render()
        self.assertIn("Rename tab · 1/3", rendered)
        self.assertIn("󰓩 agents · focused", rendered)
        self.assertIn("j/k move · l/↵/Space edit", rendered)
        top, left, height, width = 0, 4, 5, 40
        self.assertEqual(
            "".join(screen.cells[top][left : left + width]),
            "╭" + "─" * (width - 2) + "╮",
        )
        self.assertEqual(
            "".join(screen.cells[top + height - 1][left : left + width]),
            "╰" + "─" * (width - 2) + "╯",
        )
        for y in range(top + 1, top + height - 1):
            self.assertEqual(screen.cells[y][left], "│")
            self.assertEqual(screen.cells[y][left + width - 1], "│")

    def test_tab_title_editor_clips_a_long_prefill_inside_minimum_size(self) -> None:
        service = RecordingService()
        service._rows[0].live_tabs[0].title = "very-long-title-" * 10
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 0
        manager.palette = rendered_manager()[2]
        screen = ScriptedCanvas(8, 48, [" ", curses.KEY_F1, "\x1b"])

        manager._handle_key(screen, "R")

        rendered = screen.render()
        self.assertIn("…", rendered)
        self.assertIn("Backspace edit", rendered)
        self.assertEqual(manager.message, "tab rename cancelled")

    def test_tab_rename_reports_a_session_without_any_captured_tabs(self) -> None:
        service = RecordingService()
        service._rows[1].context = None
        service._rows[1].stored.manifest.summary.tab_count = 0
        service._rows[1].stored.manifest.summary.tab_titles = []
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1

        manager._handle_key(Canvas(16, 100), "R")

        self.assertEqual(service.renamed_tabs, [])
        self.assertEqual(manager.message, "selected session has no captured tabs")

    def test_help_is_a_comprehensive_theme_aware_modal(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        manager.palette = rendered_manager()[2]
        manager.help_open = True
        screen = Canvas(42, 110)

        manager._draw(screen)
        first_page = screen.render()
        top, left, box_height, box_width, *_ = manager._help_layout(screen)
        self.assertLess(top + box_height - 1, screen.height - 3)

        manager._handle_key(screen, "G")
        manager._draw(screen)
        rendered = first_page + screen.render()

        for heading in (
            "CLOSE & NAVIGATION",
            "SESSION STATES",
            "TAB MEMBERSHIP",
            "SESSION ACTIONS",
            "AUTOSAVE & PRIVACY",
        ):
            self.assertIn(heading, rendered)
        self.assertNotIn("park", rendered.casefold())
        self.assertNotIn("undo", rendered.casefold())
        self.assertIn("Q close manager", rendered)
        self.assertIn("Remove inactive session to trash.", rendered)
        self.assertIn("Rename one tab from a modal picker.", rendered)
        self.assertEqual(screen.cells[top][left], "╭")
        self.assertEqual(screen.styles[top][left], manager.palette.muted)
        self.assertEqual(
            "".join(screen.cells[top][left : left + box_width]),
            "╭" + "─" * (box_width - 2) + "╮",
        )
        bottom = top + box_height - 1
        self.assertEqual(
            "".join(screen.cells[bottom][left : left + box_width]),
            "╰" + "─" * (box_width - 2) + "╯",
        )
        for y in range(top + 1, bottom):
            self.assertEqual(screen.cells[y][left], "│")
            self.assertEqual(screen.cells[y][left + box_width - 1], "│")
        self.assertNotEqual(screen.styles[top + 1][left + 2], manager.palette.normal)

    def test_every_session_row_shows_dates_inside_the_panel(self) -> None:
        rendered, _, _ = rendered_manager()

        session_lines = [
            line
            for line in rendered.splitlines()
            if any(name in line for name in ("JAX Agents", "Dotfiles", "Main Vault"))
        ]
        self.assertEqual(len(session_lines), 3)
        self.assertEqual(rendered.count("CREATED"), 1)
        self.assertEqual(rendered.count("MODIFIED"), 1)
        self.assertTrue(all("2026-07-18" in line for line in session_lines))
        self.assertTrue(all("2026-08-04 11:30" in line for line in session_lines))
        self.assertTrue(all("created" not in line.casefold() for line in session_lines))
        self.assertTrue(all("modified" not in line.casefold() for line in session_lines))
        self.assertTrue(
            all(
                state not in line.casefold()
                for line in session_lines
                for state in ("live", "saved", "archived")
            )
        )
        sessions_heading = next(line for line in rendered.splitlines() if "SESSIONS" in line)
        self.assertIn("SESSIONS  ·  2 shown", sessions_heading)
        self.assertNotIn("live", sessions_heading.casefold())
        for glyph, name in (("●", "JAX Agents"), ("○", "Dotfiles"), ("○", "Main Vault")):
            self.assertTrue(any(f"{glyph} {name}" in line for line in session_lines))
        self.assertTrue(all("→" not in line for line in session_lines))
        self.assertIn("\n  ~/dotfiles\n", rendered)

    def test_tab_and_pane_counts_are_aligned_under_named_columns(self) -> None:
        rendered, _, _ = rendered_manager()
        lines = rendered.splitlines()
        heading = next(line for line in lines if "SESSIONS" in line)
        tabs_column = heading.index("TABS")
        panes_column = heading.index("PANES")

        for name, tabs, panes in (
            ("JAX Agents", 3, 6),
            ("Dotfiles", 2, 3),
            ("Main Vault", 1, 1),
        ):
            row = next(line for line in lines if name in line)
            self.assertEqual(row[tabs_column : tabs_column + 4], f"{tabs:>4}")
            self.assertEqual(row[panes_column : panes_column + 5], f"{panes:>5}")
            self.assertNotIn(f"{tabs}t", row)
            self.assertNotIn(f"{panes}p", row)

    def test_date_column_names_remain_visible_in_the_narrow_table(self) -> None:
        rendered, _, _ = rendered_manager(48, 8)

        self.assertIn("TABS", rendered)
        self.assertIn("PANES", rendered)
        self.assertIn("CREATED", rendered)
        self.assertIn("MODIFIED", rendered)
        self.assertIn("07-18", rendered)
        self.assertIn("08-04", rendered)

    def test_help_scrolls_in_a_small_terminal_and_q_returns_to_manager(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        manager.palette = rendered_manager()[2]
        manager.help_open = True
        screen = Canvas(16, 80)

        manager._draw(screen)
        self.assertIn("CLOSE & NAVIGATION", screen.render())
        self.assertNotIn("AUTOSAVE & PRIVACY", screen.render())

        manager._handle_key(screen, "G")
        manager._draw(screen)
        self.assertIn("AUTOSAVE & PRIVACY", screen.render())
        self.assertTrue(manager.help_open)

        self.assertIsNone(manager._handle_key(screen, "q"))
        self.assertFalse(manager.help_open)
        self.assertEqual(manager._handle_key(screen, "q"), 0)

    def test_help_modal_fully_occludes_a_dirty_background(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        manager.palette = rendered_manager()[2]
        manager.help_open = True
        screen = Canvas(30, 100)
        for y in range(screen.height):
            screen.addstr(y, 0, "X" * screen.width)

        manager._draw_help(screen)

        top, left, _, box_width, lines, _ = manager._help_layout(screen)
        blank_line = lines.index("")
        interior = "".join(screen.cells[top + 2 + blank_line][left + 1 : left + box_width - 1])
        self.assertEqual(interior, " " * (box_width - 2))

    def test_help_rendering_stays_inside_boundary_terminal_sizes(self) -> None:
        for width, height in ((48, 8), (60, 10), (80, 12), (120, 24)):
            with self.subTest(width=width, height=height):
                manager = SessionManager(StaticService())
                manager._refresh()
                manager.palette = rendered_manager()[2]
                manager.help_open = True
                screen = Canvas(height, width)
                manager._draw(screen)
                rendered = screen.render().splitlines()
                self.assertLessEqual(len(rendered), height)
                self.assertTrue(all(len(line) <= width for line in rendered))

    def test_main_screen_close_keys_are_explicit(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        screen = Canvas(16, 100)

        for key in ("q", "h", "\x1b"):
            with self.subTest(key=repr(key)):
                self.assertEqual(manager._handle_key(screen, key), 0)


if __name__ == "__main__":
    unittest.main()
