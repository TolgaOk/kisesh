"""Present the pane-scoped manager overlay across its complete Kitty tab."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass

from .kitty_client import KittyController, KittyError
from .model import RESTORE_LAYOUT_VAR


@dataclass(frozen=True, slots=True)
class ManagerSurface:
    """Exact tab and layout state needed after a temporary full-tab presentation."""

    controller: KittyController
    overlay_window_id: int
    tab_id: int
    original_layout: str


def _overlay_window_id(environment: Mapping[str, str]) -> int | None:
    """Return the current window only for an explicitly launched manager overlay."""
    if environment.get("KISESH_CALLER") != "overlay":
        return None
    value = environment.get("KITTY_WINDOW_ID")
    try:
        return int(value) if value else None
    except ValueError:
        return None


def expand_manager_surface(
    controller: KittyController | None,
    environment: Mapping[str, str] | None = None,
) -> ManagerSurface | None:
    """Expand an overlay's tab while recording enough state for exact restoration."""
    overlay_id = _overlay_window_id(environment if environment is not None else os.environ)
    if controller is None or overlay_id is None:
        return None
    try:
        state = controller.list_state()
        tab = controller.focused_tab(state, exclude_window_id=overlay_id)
        original_layout = tab.layout.strip()
        if not original_layout or original_layout.partition(":")[0].casefold() == "stack":
            return None
        controller.set_user_vars((overlay_id,), {RESTORE_LAYOUT_VAR: original_layout})
        try:
            controller.set_tab_layout(tab.tab_id, "stack")
        except KittyError:
            with suppress(KittyError):
                controller.set_user_vars((overlay_id,), {RESTORE_LAYOUT_VAR: None})
            return None
    except KittyError:
        return None
    return ManagerSurface(controller, overlay_id, tab.tab_id, original_layout)


def restore_manager_surface(surface: ManagerSurface | None) -> bool:
    """Restore a normally exiting manager and leave forced-close recovery metadata intact."""
    if surface is None:
        return False
    try:
        surface.controller.set_tab_layout(surface.tab_id, surface.original_layout)
    except KittyError:
        return False
    with suppress(KittyError):
        surface.controller.set_user_vars(
            (surface.overlay_window_id,),
            {RESTORE_LAYOUT_VAR: None},
        )
    return True
