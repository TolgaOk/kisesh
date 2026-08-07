"""Control KiSesh's transient overlay and resident quick-access surfaces."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .kitty_client import KittyController, KittyError
from .model import RESTORE_LAYOUT_VAR


class PanelError(RuntimeError):
    """Raised when a resident quick-access panel cannot be controlled."""


class PanelRunner(Protocol):
    """Callable contract for the quick-access toggle subprocess."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a panel command and return decoded output."""


def _run_panel_command(
    command: Sequence[str],
    *,
    check: bool,
    capture_output: bool,
    text: bool,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Adapt subprocess.run to the narrow panel-runner contract."""
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
    )


def is_panel_process() -> bool:
    """Report whether the current manager was launched by the panel helper."""
    return os.environ.get("KISESH_CALLER") == "panel"


def hide_quick_access_panel(
    *,
    executable: str | None = None,
    instance_group: str | None = None,
    runner: PanelRunner = _run_panel_command,
) -> None:
    """Toggle the named running quick-access instance into its hidden state."""
    group = instance_group or os.environ.get("KISESH_PANEL_GROUP")
    if not group:
        raise PanelError("quick-access panel instance group is unavailable")
    command = [
        executable or _find_kitten(),
        "quick-access-terminal",
        f"--instance-group={group}",
    ]
    config = os.environ.get("KISESH_PANEL_CONFIG")
    if config:
        command.append(f"--config={config}")
    try:
        result = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PanelError(f"cannot hide the quick-access panel: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PanelError(f"cannot hide the quick-access panel: {detail or result.returncode}")


def _find_kitten() -> str:
    """Resolve kitten from PATH or its conventional macOS application path."""
    found = shutil.which("kitten")
    if found:
        return found
    app_binary = Path("/Applications/kitty.app/Contents/MacOS/kitten")
    if app_binary.is_file():
        return str(app_binary)
    raise PanelError("cannot find the kitten executable")


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
