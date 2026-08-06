"""Typed remote-control boundary between KiSesh and a running Kitty."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .domain import KittyOsWindowState, KittyWindow
from .legacy import VARIABLE_ALIASES as LEGACY_VARIABLE_ALIASES
from .model import (
    CAPTURE_VAR,
    KISESH_UI_VAR,
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    SessionManifest,
    session_marker_name,
)

SESSION_FILTER_KITTEN = Path(__file__).resolve().parents[1] / "integration" / "session_filter.py"


class KittyError(RuntimeError):
    """Raised when Kitty state is unavailable, invalid, or rejects a command."""


class CommandRunner(Protocol):
    """Callable contract used to execute one Kitty remote-control command."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        input: str | None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a command and return decoded standard streams."""


def _run_command(
    command: Sequence[str],
    *,
    check: bool,
    capture_output: bool,
    text: bool,
    input: str | None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Adapt subprocess.run to the narrow runner protocol."""
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=text,
        input=input,
        timeout=timeout,
    )


@dataclass(slots=True)
class LiveTab:
    """Normalized subset of live Kitty tab state used by KiSesh."""

    os_window_id: int
    tab_id: int
    index: int
    title: str
    layout: str
    windows: list[KittyWindow]
    is_focused: bool = False
    is_active: bool = False

    @property
    def representative_window_id(self) -> int:
        """Return the active pane ID, falling back to the first pane."""
        if not self.windows:
            raise KittyError(f"tab {self.tab_id} has no windows")
        active = next((window for window in self.windows if window.get("is_active")), None)
        return (active or self.windows[0])["id"]

    def session_id(self) -> str | None:
        """Return the stable KiSesh session UUID stamped on this tab."""
        return _first_user_var(self.windows, SESSION_ID_VAR)

    def session_scope(self) -> str | None:
        """Return the current or previous OS-window visibility scope."""
        return _first_user_var(self.windows, SESSION_SCOPE_VAR)

    def native_session_name(self) -> str | None:
        """Return Kitty's native session identity shared by inherited tabs."""
        return next(
            (name for window in self.windows if (name := window.get("session_name", "").strip())),
            None,
        )

    def suggested_root(self) -> str:
        """Infer a project root from the most recently focused live pane."""
        candidates = sorted(
            self.windows,
            key=lambda window: float(window.get("last_focused_at", 0)),
            reverse=True,
        )
        for window in candidates:
            processes = window.get("foreground_processes", [])
            if processes and processes[-1].get("cwd"):
                return str(processes[-1]["cwd"])
            if window.get("cwd"):
                return str(window["cwd"])
        return str(Path.cwd())


def _user_var(variables: Mapping[str, str], name: str) -> str | None:
    """Resolve a current user variable with its previous-name fallback."""
    value = variables.get(name)
    if value:
        return value
    return variables.get(LEGACY_VARIABLE_ALIASES[name])


def _first_user_var(windows: Iterable[KittyWindow], name: str) -> str | None:
    """Return the first compatible nonempty user variable across a tab's panes."""
    for window in windows:
        value = _user_var(window.get("user_vars", {}), name)
        if value:
            return value
    return None


def _replacement_variables(
    current: Mapping[str, str | None],
) -> dict[str, str | None]:
    """Set current variables while clearing their previous-name equivalents."""
    replacements = dict(current)
    replacements.update({LEGACY_VARIABLE_ALIASES[name]: None for name in current})
    return replacements


def _user_var_match(name: str, value: str) -> str:
    """Match either the current or previous spelling of one user variable."""
    return " or ".join(
        f"var:{candidate}={value}" for candidate in (name, LEGACY_VARIABLE_ALIASES[name])
    )


def _is_kisesh_ui_window(window: KittyWindow) -> bool:
    """Identify transient manager overlays that must never become session panes."""
    value = _user_var(window.get("user_vars", {}), KISESH_UI_VAR) or ""
    return value.casefold() not in {"", "0", "false", "no"}


class KittyController(Protocol):
    """Operations the service layer requires from a Kitty implementation."""

    def list_state(self) -> list[KittyOsWindowState]:
        """Return normalized remote state."""

    def tabs(self, state: list[KittyOsWindowState] | None = None) -> list[LiveTab]:
        """Return usable tabs from optional preloaded state."""

    def focused_tab(
        self,
        state: list[KittyOsWindowState] | None = None,
        *,
        exclude_window_id: int | None = None,
    ) -> LiveTab:
        """Return the focused usable tab."""

    def tabs_for_session(
        self,
        session_id: str,
        state: list[KittyOsWindowState] | None = None,
    ) -> list[LiveTab]:
        """Return every live tab stamped with a session UUID."""

    def stamp_tab(
        self,
        tab: LiveTab,
        manifest: SessionManifest,
        *,
        exclude_window_id: int | None = None,
    ) -> None:
        """Stamp session ownership on every eligible pane."""

    def clear_tab_session(self, tab: LiveTab) -> None:
        """Clear session ownership without closing a tab."""

    def restamp_session(self, session_id: str, slug: str, name: str) -> None:
        """Update live display markers after a rename."""

    def capture_session(self, session_id: str, destination: Path) -> None:
        """Capture all matching live tabs into a Kitty session file."""

    def capture_tab(self, tab: LiveTab, destination: Path, capture_id: str) -> None:
        """Capture one tab into a Kitty session file."""

    def last_command_output(self, window_id: int) -> str | None:
        """Return the last completed command output for a pane."""

    def terminal_history(self, window_id: int) -> str | None:
        """Return screen and scrollback text for a pane."""

    def send_text(self, window_id: int, text: str) -> None:
        """Prefill text without executing it."""

    def focus_tab(self, tab_id: int) -> None:
        """Focus one tab by ID."""

    def rename_tab(self, tab_id: int, title: str) -> None:
        """Assign one explicit native tab title by stable ID."""

    def activate_session(self, session_id: str, tab: LiveTab) -> None:
        """Focus a session and restrict the tab bar to its live tabs."""

    def close_session_tabs(self, session_id: str) -> None:
        """Reveal remaining tabs, then close every tab in one session."""

    def close_tabs(self, tab_ids: Iterable[int]) -> None:
        """Close an exact set of tabs without affecting other ownership groups."""

    def open_snapshot(self, path: Path) -> None:
        """Load a Kitty session snapshot."""


class KittyClient:
    """Execute and normalize Kitty remote-control operations."""

    def __init__(
        self,
        executable: str | None = None,
        socket: str | None = None,
        runner: CommandRunner = _run_command,
    ) -> None:
        """Resolve the executable and target socket without opening a connection."""
        self.executable = executable or _find_kitty()
        self.socket = (
            socket
            or os.environ.get("KISESH_TARGET_SOCKET")
            or os.environ.get("KITTY_LISTEN_ON")
            or _find_socket()
        )
        self.runner = runner

    def command(self, *arguments: str, check: bool = True, stdin: str | None = None) -> str:
        """Run one remote command and translate process failures to KittyError."""
        command = [self.executable, "@"]
        if self.socket:
            address = self.socket if not self.socket.startswith("/") else f"unix:{self.socket}"
            command.extend(("--to", address))
        command.extend(arguments)
        try:
            result = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                input=stdin,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise KittyError(f"cannot run Kitty remote command: {error}") from error
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise KittyError(f"Kitty command failed ({result.returncode}): {detail}")
        return result.stdout

    def list_state(self) -> list[KittyOsWindowState]:
        """Decode the full Kitty remote state as normalized typed dictionaries."""
        try:
            state: object = json.loads(self.command("ls"))
        except json.JSONDecodeError as error:
            raise KittyError("Kitty returned invalid window state") from error
        if not isinstance(state, list):
            raise KittyError("Kitty window state is not a list")
        return cast(list[KittyOsWindowState], state)

    def last_command_output(self, window_id: int) -> str:
        """Return plain output from the last completed shell command."""
        return self.command(
            "get-text",
            "--match",
            f"id:{window_id}",
            "--extent",
            "last_cmd_output",
        )

    def terminal_history(self, window_id: int) -> str:
        """Return styled text from the pane's active screen and scrollback buffer."""
        return self.command(
            "get-text",
            "--match",
            f"id:{window_id}",
            "--extent",
            "all",
            "--ansi",
        )

    def send_text(self, window_id: int, text: str) -> None:
        """Place literal text in a pane without appending an Enter key."""
        if text:
            self.command(
                "send-text",
                "--match",
                f"id:{window_id}",
                "--bracketed-paste=auto",
                "--stdin",
                stdin=text,
            )

    def tabs(self, state: list[KittyOsWindowState] | None = None) -> list[LiveTab]:
        """Flatten usable, non-manager Kitty tabs from remote state."""
        os_windows = state if state is not None else self.list_state()
        tabs: list[LiveTab] = []
        for os_window in os_windows:
            for index, tab in enumerate(os_window.get("tabs", [])):
                windows = [
                    window for window in tab.get("windows", []) if not _is_kisesh_ui_window(window)
                ]
                if windows:
                    tabs.append(
                        LiveTab(
                            os_window_id=os_window["id"],
                            tab_id=tab["id"],
                            index=index,
                            title=tab.get("title") or "untitled",
                            layout=tab.get("layout") or "splits",
                            windows=windows,
                            is_focused=bool(tab.get("is_focused")),
                            is_active=bool(tab.get("is_active")),
                        )
                    )
        return tabs

    def focused_tab(
        self,
        state: list[KittyOsWindowState] | None = None,
        *,
        exclude_window_id: int | None = None,
    ) -> LiveTab:
        """Return the highest-priority focused tab outside an optional overlay."""
        os_windows = sorted(
            state if state is not None else self.list_state(),
            key=lambda item: (
                bool(item.get("is_focused")),
                bool(item.get("is_active")),
                bool(item.get("last_focused")),
            ),
            reverse=True,
        )
        if not os_windows:
            raise KittyError("Kitty has no OS windows")
        for os_window in os_windows:
            indexed_tabs = sorted(
                enumerate(os_window.get("tabs", [])),
                key=lambda item: (
                    bool(item[1].get("is_focused")),
                    bool(item[1].get("is_active")),
                ),
                reverse=True,
            )
            for original_index, tab in indexed_tabs:
                windows = [
                    window
                    for window in tab.get("windows", [])
                    if not _is_kisesh_ui_window(window)
                    and (exclude_window_id is None or window["id"] != exclude_window_id)
                ]
                if windows:
                    return LiveTab(
                        os_window_id=os_window["id"],
                        tab_id=tab["id"],
                        index=original_index,
                        title=tab.get("title") or "untitled",
                        layout=tab.get("layout") or "splits",
                        windows=windows,
                        is_focused=bool(tab.get("is_focused")),
                        is_active=bool(tab.get("is_active")),
                    )
        message = (
            "Kitty has no usable tab outside the manager"
            if exclude_window_id is not None
            else "Kitty has no usable tabs"
        )
        raise KittyError(message)

    def tabs_for_session(
        self,
        session_id: str,
        state: list[KittyOsWindowState] | None = None,
    ) -> list[LiveTab]:
        """Return every usable tab stamped with a stable session UUID."""
        return [tab for tab in self.tabs(state) if tab.session_id() == session_id]

    def set_user_vars(
        self,
        window_ids: Iterable[int],
        variables: Mapping[str, str | None],
    ) -> None:
        """Set or clear user variables for all supplied panes in one request."""
        unique_ids = tuple(dict.fromkeys(window_ids))
        if not unique_ids:
            return
        encoded = [
            name if value is None else f"{name}={value}" for name, value in variables.items()
        ]
        match = " or ".join(f"id:{window_id}" for window_id in unique_ids)
        self.command("set-user-vars", "--match", match, *encoded)

    def stamp_tab(
        self,
        tab: LiveTab,
        manifest: SessionManifest,
        *,
        exclude_window_id: int | None = None,
    ) -> None:
        """Stamp missing or stale ownership variables on eligible panes."""
        desired = _replacement_variables(
            {
                SESSION_ID_VAR: manifest.id,
                SESSION_SLUG_VAR: manifest.slug,
                SESSION_NAME_VAR: session_marker_name(manifest.name, manifest.slug),
            }
        )
        window_ids = [
            window["id"]
            for window in tab.windows
            if window["id"] != exclude_window_id
            and any(window.get("user_vars", {}).get(key) != value for key, value in desired.items())
        ]
        self.set_user_vars(window_ids, desired)

    def clear_tab_session(self, tab: LiveTab) -> None:
        """Clear every current ownership variable without closing panes."""
        self.set_user_vars(
            (window["id"] for window in tab.windows),
            _replacement_variables(
                {SESSION_ID_VAR: None, SESSION_SLUG_VAR: None, SESSION_NAME_VAR: None}
            ),
        )

    def restamp_session(self, session_id: str, slug: str, name: str) -> None:
        """Update slug and display-name markers for all live session panes."""
        state = self.list_state()
        window_ids = [
            window["id"]
            for tab in self.tabs_for_session(session_id, state)
            for window in tab.windows
        ]
        self.set_user_vars(
            window_ids,
            _replacement_variables(
                {SESSION_ID_VAR: session_id, SESSION_SLUG_VAR: slug, SESSION_NAME_VAR: name}
            ),
        )

    def capture_session(self, session_id: str, destination: Path) -> None:
        """Save all live tabs for a session and verify Kitty wrote content."""
        self.command(
            "action",
            "save_as_session",
            "--save-only",
            f"--match=var:{SESSION_ID_VAR}={session_id}",
            str(destination),
        )
        _require_snapshot(destination, "session")

    def capture_tab(self, tab: LiveTab, destination: Path, capture_id: str) -> None:
        """Capture one tab through a temporary marker that is always cleared."""
        window_ids = [window["id"] for window in tab.windows]
        self.set_user_vars(window_ids, {CAPTURE_VAR: capture_id})
        try:
            self.command(
                "action",
                "save_as_session",
                "--save-only",
                f"--match=var:{CAPTURE_VAR}={capture_id}",
                str(destination),
            )
        finally:
            self.set_user_vars(window_ids, {CAPTURE_VAR: None})
        _require_snapshot(destination, "tab capture")

    def focus_tab(self, tab_id: int) -> None:
        """Focus a live tab by its Kitty ID."""
        self.command("focus-tab", "--match", f"id:{tab_id}")

    def rename_tab(self, tab_id: int, title: str) -> None:
        """Assign one explicit native title without changing tab focus."""
        self.command("set-tab-title", "--match", f"id:{tab_id}", title)

    def activate_session(self, session_id: str, tab: LiveTab) -> None:
        """Focus one session while leaving unrelated OS windows unfiltered."""
        scope = str(tab.os_window_id)
        tabs = self.tabs()
        scope_variables = _replacement_variables({SESSION_SCOPE_VAR: scope})
        scoped_windows = [
            window["id"]
            for candidate in tabs
            if candidate.os_window_id == tab.os_window_id
            for window in candidate.windows
            if any(
                window.get("user_vars", {}).get(name) != value
                for name, value in scope_variables.items()
            )
        ]
        outside_windows = [
            window["id"]
            for candidate in tabs
            if candidate.os_window_id != tab.os_window_id
            for window in candidate.windows
            if _user_var(window.get("user_vars", {}), SESSION_SCOPE_VAR) is not None
        ]
        self.set_user_vars(
            scoped_windows,
            scope_variables,
        )
        self.set_user_vars(
            outside_windows,
            _replacement_variables({SESSION_SCOPE_VAR: None}),
        )
        self.focus_tab(tab.tab_id)
        self.command(
            "kitten",
            str(SESSION_FILTER_KITTEN),
            f"{_user_var_match(SESSION_ID_VAR, session_id)} or not var:{SESSION_SCOPE_VAR}={scope}",
        )

    def close_session_tabs(self, session_id: str) -> None:
        """Reset tab visibility before closing every tab owned by a session."""
        self.command("kitten", str(SESSION_FILTER_KITTEN), "all")
        scoped_windows = [
            window["id"]
            for tab in self.tabs()
            for window in tab.windows
            if _user_var(window.get("user_vars", {}), SESSION_SCOPE_VAR) is not None
        ]
        self.set_user_vars(
            scoped_windows,
            _replacement_variables({SESSION_SCOPE_VAR: None}),
        )
        self.command(
            "close-tab",
            "--match",
            _user_var_match(SESSION_ID_VAR, session_id),
        )

    def close_tabs(self, tab_ids: Iterable[int]) -> None:
        """Close explicitly identified tabs in one atomic remote request."""
        unique_ids = tuple(dict.fromkeys(tab_ids))
        if not unique_ids:
            return
        match = " or ".join(f"id:{tab_id}" for tab_id in unique_ids)
        self.command("close-tab", "--match", match)

    def open_snapshot(self, path: Path) -> None:
        """Load a safe snapshot into the current Kitty operating-system window."""
        self.command("action", "goto_session", str(path))


def _require_snapshot(path: Path, label: str) -> None:
    """Reject a remote capture that did not create a nonempty snapshot."""
    if not path.is_file() or not path.stat().st_size:
        raise KittyError(f"Kitty did not produce a {label} snapshot")


def _find_kitty() -> str:
    """Resolve Kitty from PATH or its conventional macOS application path."""
    found = shutil.which("kitty")
    if found:
        return found
    app_binary = Path("/Applications/kitty.app/Contents/MacOS/kitty")
    if app_binary.exists():
        return str(app_binary)
    raise KittyError("cannot find the Kitty executable")


def _find_socket() -> str | None:
    """Return the only discoverable Kitty socket, avoiding ambiguous guesses."""
    candidates: list[Path] = []
    for candidate in (Path("/tmp/mykitty"), *Path("/tmp").glob("mykitty-*")):
        try:
            if candidate.exists() and _is_socket(candidate):
                candidates.append(candidate)
        except OSError:
            continue
    return f"unix:{candidates[0]}" if len(candidates) == 1 else None


def _is_socket(path: Path) -> bool:
    """Return whether a filesystem entry is a Unix-domain socket."""
    return stat.S_ISSOCK(path.stat().st_mode)
