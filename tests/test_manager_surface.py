from __future__ import annotations

import unittest
from collections.abc import Iterable, Mapping
from typing import ClassVar
from unittest.mock import patch

from kisesh.kitty_client import KittyError
from kisesh.model import KISESH_UI_VAR, RESTORE_LAYOUT_VAR, KittyOsWindowState, KittyWindow
from kisesh.panel import (
    _overlay_window_id,
    expand_manager_surface,
    restore_manager_surface,
)
from tests.fakes import FakeKitty


class FailingKitty(FakeKitty):
    """Inject bounded remote-control failures into a complete fake controller."""

    def __init__(self, failure: str) -> None:
        """Select the operation that should reject the presentation change."""
        super().__init__()
        self.failure = failure
        self.clear_attempted = False

    def list_state(self) -> list[KittyOsWindowState]:
        """Fail state discovery only in its dedicated scenario."""
        if self.failure == "state":
            raise KittyError("state unavailable")
        return super().list_state()

    def set_user_vars(
        self,
        window_ids: Iterable[int],
        variables: Mapping[str, str | None],
    ) -> None:
        """Fail marker creation or cleanup without changing unrelated behavior."""
        clearing = variables.get(RESTORE_LAYOUT_VAR, "sentinel") is None
        self.clear_attempted = self.clear_attempted or clearing
        if self.failure == "marker" or (self.failure == "cleanup" and clearing):
            raise KittyError("marker unavailable")
        super().set_user_vars(window_ids, variables)

    def set_tab_layout(self, tab_id: int, layout: str) -> None:
        """Reject the temporary stack transition in layout-failure scenarios."""
        if self.failure == "layout":
            raise KittyError("layout unavailable")
        super().set_tab_layout(tab_id, layout)


class ManagerSurfaceTests(unittest.TestCase):
    """Exercise full-tab promotion without a persistent manager window."""

    environment: ClassVar[dict[str, str]] = {
        "KISESH_CALLER": "overlay",
        "KITTY_WINDOW_ID": "99",
    }

    def test_overlay_is_promoted_to_stack_with_exact_restore_metadata(self) -> None:
        kitty = FakeKitty()
        overlay: KittyWindow = {"id": 99, "user_vars": {KISESH_UI_VAR: "yes"}}
        kitty.tab.windows.append(overlay)

        surface = expand_manager_surface(kitty, self.environment)

        self.assertIsNotNone(surface)
        assert surface is not None
        self.assertEqual(
            (surface.overlay_window_id, surface.tab_id, surface.original_layout),
            (99, 7, "splits"),
        )
        self.assertEqual(kitty.changed_layouts, [(7, "stack")])
        self.assertEqual(overlay["user_vars"][RESTORE_LAYOUT_VAR], "splits")
        self.assertEqual(
            kitty.user_var_updates,
            [((99,), {RESTORE_LAYOUT_VAR: "splits"})],
        )
        self.assertTrue(restore_manager_surface(surface))
        self.assertEqual(kitty.changed_layouts, [(7, "stack"), (7, "splits")])
        self.assertNotIn(RESTORE_LAYOUT_VAR, overlay["user_vars"])

    def test_non_overlay_invalid_id_and_existing_stack_are_noops(self) -> None:
        self.assertIsNone(_overlay_window_id({"KISESH_CALLER": "panel"}))
        self.assertIsNone(
            _overlay_window_id({"KISESH_CALLER": "overlay", "KITTY_WINDOW_ID": "invalid"})
        )
        self.assertFalse(expand_manager_surface(None, self.environment))
        self.assertFalse(restore_manager_surface(None))

        with patch.dict("os.environ", self.environment, clear=True):
            self.assertFalse(expand_manager_surface(FakeKitty(), {}))

        kitty = FakeKitty()
        kitty.tab.layout = "stack"
        self.assertFalse(expand_manager_surface(kitty, self.environment))
        self.assertEqual(kitty.changed_layouts, [])
        self.assertEqual(kitty.user_var_updates, [])

        kitty.tab.layout = ""
        self.assertFalse(expand_manager_surface(kitty, self.environment))
        self.assertEqual(kitty.changed_layouts, [])
        self.assertEqual(kitty.user_var_updates, [])

    def test_remote_failures_leave_the_manager_usable_and_clear_stale_markers(self) -> None:
        for failure, clear_attempted in (
            ("state", False),
            ("marker", False),
            ("layout", True),
        ):
            kitty = FailingKitty(failure)
            with self.subTest(failure=failure):
                self.assertFalse(expand_manager_surface(kitty, self.environment))
                self.assertEqual(kitty.clear_attempted, clear_attempted)

        layout_failure = FailingKitty("")
        failed_surface = expand_manager_surface(layout_failure, self.environment)
        self.assertIsNotNone(failed_surface)
        layout_failure.failure = "layout"
        self.assertFalse(restore_manager_surface(failed_surface))

        cleanup_failure = FailingKitty("cleanup")
        cleanup_surface = expand_manager_surface(cleanup_failure, self.environment)
        self.assertIsNotNone(cleanup_surface)
        self.assertTrue(restore_manager_surface(cleanup_surface))
        self.assertTrue(cleanup_failure.clear_attempted)


if __name__ == "__main__":
    unittest.main()
