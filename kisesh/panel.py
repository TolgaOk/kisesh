"""Control the optional resident Kitty quick-access panel."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


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
