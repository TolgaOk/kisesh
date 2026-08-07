"""Launch and toggle KiSesh's optional Kitty quick-access panel."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_PANEL_SOCKET = "unix:/tmp/kisesh-panel"


class PanelLaunchError(RuntimeError):
    """A missing executable or control socket that prevents panel startup."""


def _is_executable(path: Path) -> bool:
    """Report whether a path names an executable regular file."""
    return path.is_file() and os.access(path, os.X_OK)


def _configured_executable(environment: Mapping[str, str], name: str) -> Path | None:
    """Validate one explicitly configured executable when present."""
    configured = environment.get(name)
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    if not _is_executable(candidate):
        raise PanelLaunchError(f"{name} is not executable: {configured}")
    return candidate


def _kitten_executable(environment: Mapping[str, str]) -> Path:
    """Resolve Kitty's helper through explicit, platform, then PATH locations."""
    if configured := _configured_executable(environment, "KISESH_KITTEN"):
        return configured
    candidates = (
        Path("/Applications/kitty.app/Contents/MacOS/kitten"),
        Path(found) if (found := shutil.which("kitten", path=environment.get("PATH"))) else None,
    )
    for candidate in candidates:
        if candidate is not None and _is_executable(candidate):
            return candidate
    raise PanelLaunchError("kitten was not found")


def _cli_executable(environment: Mapping[str, str], invoked_as: Path) -> Path:
    """Resolve the paired KiSesh command without relying on an interactive PATH."""
    if configured := _configured_executable(environment, "KISESH_CLI"):
        return configured
    candidates = (
        invoked_as.absolute().with_name("kisesh"),
        Path(sys.executable).with_name("kisesh"),
        Path("~/.local/bin/kisesh").expanduser(),
        Path(found) if (found := shutil.which("kisesh", path=environment.get("PATH"))) else None,
    )
    for candidate in candidates:
        if candidate is not None and _is_executable(candidate):
            return candidate
    raise PanelLaunchError("the kisesh command was not found")


def _runtime_root(environment: Mapping[str, str], invoked_as: Path) -> Path:
    """Resolve the stable runtime used by panel child processes."""
    if configured := environment.get("KISESH_INSTALL_ROOT"):
        return Path(configured).expanduser()
    invoked_root = invoked_as.absolute().parent.parent
    if (invoked_root / "integration").is_dir() and (invoked_root / "kisesh").is_dir():
        return invoked_root
    return Path("~/.local/lib/kisesh").expanduser()


def _target_socket(environment: Mapping[str, str]) -> str:
    """Resolve and validate the main Kitty control socket."""
    socket = environment.get("KISESH_TARGET_SOCKET") or environment.get("KITTY_LISTEN_ON")
    if not socket:
        raise PanelLaunchError("the main Kitty control socket is unavailable")
    if socket.startswith("fd:"):
        raise PanelLaunchError("the panel needs a persistent unix Kitty socket")
    return socket


def _panel_socket(environment: Mapping[str, str]) -> str | None:
    """Resolve an explicit or currently listening panel control socket."""
    if configured := environment.get("KISESH_PANEL_SOCKET"):
        return configured
    prefix = Path(DEFAULT_PANEL_SOCKET.removeprefix("unix:"))
    candidates = sorted(prefix.parent.glob(f"{prefix.name}*"))
    sockets = [candidate for candidate in candidates if candidate.is_socket()]
    return f"unix:{sockets[-1]}" if sockets else None


def _quick_access_command(
    arguments: Sequence[str],
    environment: Mapping[str, str],
    invoked_as: Path,
) -> tuple[list[str], dict[str, str], Path]:
    """Build one complete quick-access invocation and its child environment."""
    runtime = _runtime_root(environment, invoked_as)
    kitten = _kitten_executable(environment)
    cli = _cli_executable(environment, invoked_as)
    target_socket = _target_socket(environment)
    panel_config = Path(
        environment.get(
            "KISESH_PANEL_CONFIG",
            str(runtime / "integration" / "quick-access-terminal.conf"),
        )
    ).expanduser()
    panel_group = environment.get("KISESH_PANEL_GROUP", "kisesh")
    prewarm = bool(arguments and arguments[0] == "--prewarm")
    child_arguments = list(arguments[1:] if prewarm else arguments) or ["manager"]
    child_environment = dict(environment)
    child_environment.update(
        {
            "KISESH_CALLER": "panel",
            "KISESH_INSTALL_ROOT": str(runtime),
            "KISESH_PANEL_CONFIG": str(panel_config),
            "KISESH_PANEL_GROUP": panel_group,
            "KISESH_PANEL_SOCKET": environment.get("KISESH_PANEL_SOCKET", DEFAULT_PANEL_SOCKET),
            "KISESH_TARGET_SOCKET": target_socket,
        }
    )
    command = [
        str(kitten),
        "quick-access-terminal",
        f"--instance-group={panel_group}",
        f"--config={panel_config}",
        f"--override=start_as_hidden={'yes' if prewarm else 'no'}",
        "/usr/bin/env",
        *(
            f"{name}={child_environment[name]}"
            for name in (
                "KISESH_CALLER",
                "KISESH_INSTALL_ROOT",
                "KISESH_PANEL_CONFIG",
                "KISESH_PANEL_GROUP",
                "KISESH_PANEL_SOCKET",
                "KISESH_TARGET_SOCKET",
            )
        ),
        str(cli),
        *child_arguments,
    ]
    return command, child_environment, kitten


def run(arguments: Sequence[str], environment: Mapping[str, str] | None = None) -> int:
    """Toggle the panel and wake its resident manager after a successful edge."""
    active_environment = dict(os.environ if environment is None else environment)
    invoked_as = Path(sys.argv[0])
    command, child_environment, kitten = _quick_access_command(
        arguments,
        active_environment,
        invoked_as,
    )
    result = subprocess.run(command, env=child_environment, check=False)
    if result.returncode:
        return result.returncode
    socket = _panel_socket(child_environment)
    if socket is None:
        return 0
    wake = subprocess.run(
        [
            str(kitten),
            "@",
            "--to",
            socket,
            "send-key",
            "--match",
            "env:KISESH_CALLER=panel",
            "ctrl+g",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0 if wake.returncode in {0, 1} else wake.returncode


def main(argv: Sequence[str] | None = None) -> int:
    """Run the panel launcher and report expected setup failures concisely."""
    try:
        return run(tuple(sys.argv[1:] if argv is None else argv))
    except (OSError, PanelLaunchError) as error:
        print(f"kisesh-panel: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
