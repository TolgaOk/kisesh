"""Identifiers required to upgrade installations created before KiSesh."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

PRODUCT_DIRECTORY: Final = "kitty-workbench"
INTEGRATION_FILE: Final = "kitty-workbench.conf"
MANAGED_BEGIN: Final = "# BEGIN kitty-workbench (managed by ./install)"
MANAGED_END: Final = "# END kitty-workbench (managed by ./install)"
INTEGRATION_INCLUDE: Final = "include ~/.local/lib/kitty-workbench/integration/kitty-workbench.conf"
TAB_BAR_BACKUP: Final = "tab_bar.py.before-workbench"
SESSION_ID_VARIABLE: Final = "kitty_workbench_session"
SESSION_SLUG_VARIABLE: Final = "kitty_workbench_slug"
SESSION_NAME_VARIABLE: Final = "kitty_workbench_name"
SESSION_SCOPE_VARIABLE: Final = "kitty_workbench_scope"
CAPTURE_VARIABLE: Final = "kitty_workbench_capture"
AGENT_VARIABLE: Final = "kitty_workbench_agent"
APP_VARIABLE: Final = "kitty_workbench_app"
UI_VARIABLE: Final = "kitty_workbench_ui"
VARIABLE_ALIASES: Final[Mapping[str, str]] = {
    "kisesh_session": SESSION_ID_VARIABLE,
    "kisesh_slug": SESSION_SLUG_VARIABLE,
    "kisesh_name": SESSION_NAME_VARIABLE,
    "kisesh_scope": SESSION_SCOPE_VARIABLE,
    "kisesh_capture": CAPTURE_VARIABLE,
    "kisesh_agent": AGENT_VARIABLE,
    "kisesh_app": APP_VARIABLE,
    "kisesh_ui": UI_VARIABLE,
}
MANAGED_VARIABLES: Final = frozenset(VARIABLE_ALIASES.values())
