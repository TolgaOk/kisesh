"""Identifiers required to upgrade installations created before KiSesh."""

from __future__ import annotations

from typing import Final

PRODUCT_DIRECTORY: Final = "kitty-workbench"
INTEGRATION_FILE: Final = "kitty-workbench.conf"
MANAGED_BEGIN: Final = "# BEGIN kitty-workbench (managed by ./install)"
MANAGED_END: Final = "# END kitty-workbench (managed by ./install)"
INTEGRATION_INCLUDE: Final = "include ~/.local/lib/kitty-workbench/integration/kitty-workbench.conf"
TAB_BAR_BACKUP: Final = "tab_bar.py.before-workbench"
UI_VARIABLE: Final = "kitty_workbench_ui"
MANAGED_VARIABLES: Final = frozenset(
    {
        "kitty_workbench_agent",
        "kitty_workbench_app",
        "kitty_workbench_capture",
        "kitty_workbench_name",
        "kitty_workbench_scope",
        "kitty_workbench_session",
        "kitty_workbench_slug",
        UI_VARIABLE,
    }
)
