from __future__ import annotations

import curses
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from kitty_workbench.tui import (
    Palette,
    Screen,
    SessionManager,
    _compact_path,
    _configure_palette,
    _edit_prompt_value,
    _ellipsize,
    _format_row_time,
    _help_lines,
    _safe_hline,
    _safe_vline,
)
from tests.render_fixture import Canvas, StaticService, rendered_manager
from tests.test_tui_rendering import RecordingService, ScriptedCanvas


class BrokenRuleCanvas(Canvas):
    def hline(self, y: int, x: int, value: object, length: int) -> None:
        del y, x, value, length
        raise curses.error("horizontal rule unavailable")

    def vline(self, y: int, x: int, value: object, length: int) -> None:
        del y, x, value, length
        raise AttributeError("vertical rule unavailable")


class TuiBoundaryTests(unittest.TestCase):
    def test_preview_ellipsis_handles_zero_single_and_unclipped_cell_budgets(self) -> None:
        self.assertEqual(_ellipsize("agent", 0), "")
        self.assertEqual(_ellipsize("agent", 1), "…")
        self.assertEqual(_ellipsize("agent", 5), "agent")
        self.assertEqual(_ellipsize("agent", 4), "age…")

    def test_main_loop_surfaces_action_errors_then_continues_until_close(self) -> None:
        manager = SessionManager(StaticService())
        screen = ScriptedCanvas(16, 100, ["x", "q"])
        palette = Palette(normal=1)
        with (
            mock.patch("kitty_workbench.tui._set_cursor"),
            mock.patch("kitty_workbench.tui._configure_palette", return_value=palette),
            mock.patch.object(manager, "_draw"),
            mock.patch.object(
                manager,
                "_handle_key",
                side_effect=(ValueError("action failed"), 0),
            ),
        ):
            result = manager._main(cast(curses.window, screen))

        self.assertEqual(result, 0)
        self.assertEqual(manager.message, "action failed")
        self.assertTrue(screen.keypad_enabled)
        self.assertEqual(screen.timeout_ms, -1)

    def test_refresh_retains_selection_when_possible_and_clamps_when_it_disappears(self) -> None:
        service = StaticService()
        manager = SessionManager(service)
        manager._refresh()
        manager.selected = 1
        selected_id = manager.filtered[1].stored.manifest.id
        manager._refresh()
        self.assertEqual(manager.filtered[manager.selected].stored.manifest.id, selected_id)

        service._rows = [row for row in service._rows if row.stored.manifest.id != selected_id]
        manager._refresh()
        self.assertEqual(manager.selected, 0)

    def test_empty_and_no_match_panels_render_specific_guidance_and_messages(self) -> None:
        service = StaticService()
        service._rows = []
        manager = SessionManager(service)
        manager._refresh()
        manager.palette = rendered_manager()[2]
        screen = Canvas(16, 100)
        manager._draw(screen)
        self.assertIn("No sessions. Press n to create one.", screen.render())
        self.assertIsNone(manager._selected())

        searched = SessionManager(StaticService())
        searched.query = "definitely absent"
        searched._refresh()
        searched.palette = rendered_manager()[2]
        searched.message = "No matching session"
        searched_screen = Canvas(16, 100)
        searched._draw(searched_screen)
        self.assertIn("No matches. Keep typing or press Esc.", searched_screen.render())
        self.assertIn("No matching session", searched_screen.render())

    def test_global_help_and_create_actions_cover_empty_and_successful_prompts(self) -> None:
        service = StaticService()
        manager = SessionManager(service)
        manager._refresh()
        manager._handle_key(Canvas(16, 100), "?")
        self.assertTrue(manager.help_open)
        self.assertEqual(manager.help_scroll, 0)
        manager.help_open = False

        empty = ScriptedCanvas(16, 100, ["\n"])
        manager._handle_key(empty, "n")
        self.assertEqual(len(service._rows), 3)

        created = ScriptedCanvas(16, 100, ["N", "e", "w", "\n"])
        manager._handle_key(created, "n")
        self.assertEqual(len(service._rows), 4)
        self.assertEqual(service._rows[-1].stored.manifest.name, "New")
        self.assertEqual(manager.message, "created New")

    def test_empty_navigation_unarchive_guidance_save_archive_and_rename_paths(self) -> None:
        empty_service = StaticService()
        empty_service._rows = []
        empty = SessionManager(empty_service)
        empty._refresh()
        self.assertTrue(empty._handle_navigation_key("j"))
        self.assertTrue(empty._handle_navigation_key("k"))
        self.assertEqual(empty.selected, 0)

        service = RecordingService()
        manager = SessionManager(service)
        manager._refresh()
        screen = Canvas(16, 100)
        manager._handle_key(screen, "u")
        self.assertIn("archived list", manager.message)
        manager.selected = 1
        self.assertIsNone(manager._handle_key(screen, "x"))
        self.assertIn("only a live session", manager.message)

        manager._handle_key(screen, "s")
        self.assertIn("saved Dotfiles", manager.message)
        manager._handle_key(screen, "e")
        self.assertIn("archived Dotfiles", manager.message)

        rename_keys = ["\x7f"] * len("Dotfiles") + list("Renamed") + ["\n"]
        renamed_screen = ScriptedCanvas(16, 100, rename_keys)
        manager._handle_key(renamed_screen, "r")
        self.assertIn("renamed to Renamed", manager.message)

        with mock.patch.object(manager, "_prompt", return_value=""):
            manager._handle_key(screen, "r")
        self.assertIsNone(manager._handle_key(screen, "z"))

    def test_help_supports_all_vim_scroll_keys_back_quit_and_unrecognized_input(self) -> None:
        manager = SessionManager(StaticService())
        manager._refresh()
        manager.help_open = True
        screen = Canvas(16, 80)
        for key in ("j", curses.KEY_DOWN, "k", curses.KEY_UP, "\x04", "\x15", "g", "G", "x"):
            with self.subTest(key=key):
                self.assertIsNone(manager._handle_help_key(screen, key))
        self.assertEqual(manager._handle_help_key(screen, "Q"), 0)
        manager.help_open = True
        self.assertIsNone(manager._handle_help_key(screen, "?"))
        self.assertFalse(manager.help_open)

    def test_prompt_escape_nonprintable_input_and_edit_boundaries_are_inert(self) -> None:
        manager = SessionManager(StaticService())
        escaped = ScriptedCanvas(16, 80, ["\x1b"])
        self.assertEqual(manager._prompt(escaped, "rename", "Original"), "")

        ignored = ScriptedCanvas(16, 80, [curses.KEY_LEFT, "\n"])
        self.assertEqual(manager._prompt(ignored, "new session"), "")

        value: list[str] = []
        self.assertFalse(_edit_prompt_value(value, "\b"))
        value.extend("ab")
        self.assertTrue(_edit_prompt_value(value, curses.KEY_BACKSPACE))
        self.assertEqual(value, ["a"])
        self.assertFalse(_edit_prompt_value(value, curses.KEY_LEFT))

    def test_palette_path_time_and_panel_help_fallbacks_remain_theme_aware(self) -> None:
        with mock.patch.object(curses, "has_colors", return_value=False):
            plain = _configure_palette()
        self.assertEqual(plain.selected, curses.A_REVERSE)

        with (
            mock.patch.object(curses, "has_colors", return_value=True),
            mock.patch.object(curses, "start_color"),
            mock.patch.object(curses, "use_default_colors", side_effect=curses.error("no default")),
            mock.patch.object(curses, "init_pair"),
            mock.patch.object(curses, "color_pair", side_effect=lambda number: number * 10),
        ):
            colored = _configure_palette()
        self.assertEqual(colored.normal, 10)

        home = str(Path.home())
        self.assertEqual(_compact_path(home), "~")
        self.assertEqual(_compact_path(f"{home}/project"), "~/project")
        self.assertEqual(_compact_path("/srv/project"), "/srv/project")
        self.assertEqual(_format_row_time("not-a-time"), "unknown")
        self.assertTrue(
            any("Hide panel; Q terminates it." in line for line in _help_lines(70, panel=True))
        )

    def test_rule_renderers_handle_zero_lengths_and_native_failures_with_full_borders(self) -> None:
        screen = BrokenRuleCanvas(5, 20)
        _safe_hline(cast(Screen, screen), 0, 0, 0, 3)
        _safe_vline(cast(Screen, screen), 0, 0, 0, 3)
        self.assertEqual(screen.render(), "\n")

        _safe_hline(cast(Screen, screen), 1, 2, 5, 7)
        _safe_vline(cast(Screen, screen), 1, 1, 3, 8)
        self.assertEqual("".join(screen.cells[1][2:7]), "─────")
        self.assertEqual([screen.cells[y][1] for y in range(1, 4)], ["│", "│", "│"])
        self.assertTrue(all(screen.styles[1][x] == 7 for x in range(2, 7)))


if __name__ == "__main__":
    unittest.main()
