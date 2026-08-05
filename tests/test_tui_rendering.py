from __future__ import annotations

import unittest
from collections.abc import Iterable
from pathlib import Path

from kitty_workbench.store import StoredSession
from kitty_workbench.tui import SessionManager
from tests.render_fixture import Canvas, StaticService, rendered_manager


class RecordingService(StaticService):
    """Track manager service calls while retaining complete fixture behavior."""

    def __init__(self) -> None:
        """Initialize deterministic rows and action ledgers."""
        super().__init__()
        self.opened: list[str] = []
        self.timeline: list[str] = []
        self.unarchived: list[str] = []
        self.added: list[str] = []
        self.detached: list[str] = []
        self.copied: list[str] = []
        self.removed: list[str] = []

    def open(self, identifier: str) -> StoredSession:
        """Record and resolve a focus or restore request."""
        self.opened.append(identifier)
        self.timeline.append("open")
        return self._stored(identifier)

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
        self.assertEqual(rendered, "kitty-workbench needs at least 48x8 cells\n")

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
        manager.selected = 1
        selected_id = manager.filtered[manager.selected].stored.manifest.id
        screen = Canvas(16, 100)

        manager._handle_key(screen, "a")
        manager._handle_key(screen, "d")
        manager._handle_key(screen, "c")

        self.assertEqual(service.added, [selected_id])
        self.assertEqual(service.detached, [selected_id])
        self.assertEqual(service.copied, [selected_id])

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
