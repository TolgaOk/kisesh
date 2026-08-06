"""Typed reusable fakes for KiSesh integration tests."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from kisesh.domain import KittyOsWindowState, KittyWindow
from kisesh.kitty_client import LiveTab
from kisesh.model import (
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    SessionManifest,
    session_marker_name,
)
from kisesh.session_file import rename_snapshot_tab

DEFAULT_CAPTURE = "new_tab Project\nlaunch --cwd=/tmp/project\n"


class RecordingCommandRunner:
    """Record subprocess-style calls while returning configured text streams."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        """Initialize response streams and empty call ledgers."""
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        input: str | None = None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """Record one invocation and return its configured completed process."""
        del check, capture_output, text, timeout
        copied = list(command)
        self.commands.append(copied)
        self.inputs.append(input)
        return subprocess.CompletedProcess(
            copied,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class FakeKitty:
    """Implement Kitty's service contract over mutable in-memory tabs."""

    def __init__(self) -> None:
        """Create one focused shell tab and empty action ledgers."""
        self.window: KittyWindow = {
            "id": 11,
            "title": "Shell",
            "cwd": "/tmp/project",
            "user_vars": {},
            "foreground_processes": [{"cmdline": ["-zsh"], "cwd": "/tmp/project"}],
            "is_active": True,
            "at_prompt": True,
            "env": {"KITTY_PID": "321"},
        }
        self.tab = LiveTab(
            os_window_id=1,
            tab_id=7,
            index=0,
            title="Project",
            layout="splits",
            windows=[self.window],
            is_focused=True,
            is_active=True,
        )
        self.opened: list[Path] = []
        self.opened_contents: list[str] = []
        self.focused: list[int] = []
        self.renamed_tabs: list[tuple[int, str]] = []
        self.activated_sessions: list[tuple[str, int]] = []
        self.closed_sessions: list[str] = []
        self.closed_tabs: list[int] = []
        self.include_tab = True
        self.extra_tabs: list[LiveTab] = []
        self.current_tab = self.tab
        self.capture_session_text = DEFAULT_CAPTURE
        self.capture_tab_text = DEFAULT_CAPTURE
        self.command_outputs: dict[int, str] = {}
        self.terminal_histories: dict[int, str] = {}
        self.terminal_history_hook: Callable[[int], None] | None = None
        self.sent_text: list[tuple[int, str]] = []
        self.next_open_window_id: int | None = None
        self.next_open_tab: LiveTab | None = None

    def list_state(self) -> list[KittyOsWindowState]:
        """Return one focused operating-system window."""
        return [{"id": 1, "is_focused": True, "tabs": []}]

    def tabs(self, state: list[KittyOsWindowState] | None = None) -> list[LiveTab]:
        """Return currently visible content and optional placeholder tabs."""
        del state
        tabs = [self.tab] if self.include_tab else []
        tabs.extend(self.extra_tabs)
        return tabs

    def focused_tab(
        self,
        state: list[KittyOsWindowState] | None = None,
        *,
        exclude_window_id: int | None = None,
    ) -> LiveTab:
        """Return the configured source tab independently of exclusion metadata."""
        del state, exclude_window_id
        return self.current_tab

    def tabs_for_session(
        self,
        session_id: str,
        state: list[KittyOsWindowState] | None = None,
    ) -> list[LiveTab]:
        """Filter visible tabs by their stable KiSesh ownership marker."""
        return [tab for tab in self.tabs(state) if tab.session_id() == session_id]

    def stamp_tab(
        self,
        tab: LiveTab,
        manifest: SessionManifest,
        *,
        exclude_window_id: int | None = None,
    ) -> None:
        """Apply all current and legacy membership variables to a tab."""
        for window in tab.windows:
            if window["id"] == exclude_window_id:
                continue
            window.setdefault("user_vars", {}).update(
                {
                    SESSION_ID_VAR: manifest.id,
                    SESSION_SLUG_VAR: manifest.slug,
                    SESSION_NAME_VAR: session_marker_name(manifest.name, manifest.slug),
                }
            )

    def restamp_session(self, session_id: str, slug: str, name: str) -> None:
        """Update display markers on every visible member of one session."""
        for tab in self.tabs_for_session(session_id):
            for window in tab.windows:
                window.setdefault("user_vars", {}).update(
                    {SESSION_SLUG_VAR: slug, SESSION_NAME_VAR: name}
                )

    def clear_tab_session(self, tab: LiveTab) -> None:
        """Remove membership markers without closing the tab."""
        for window in tab.windows:
            for name in (SESSION_ID_VAR, SESSION_SLUG_VAR, SESSION_NAME_VAR):
                window.setdefault("user_vars", {}).pop(name, None)

    def capture_session(self, session_id: str, destination: Path) -> None:
        """Write the configured session serialization for a matching live session."""
        if not self.tabs_for_session(session_id):
            raise AssertionError("wrong session capture")
        destination.write_text(self.capture_session_text, encoding="utf-8")

    def capture_tab(self, tab: LiveTab, destination: Path, capture_id: str) -> None:
        """Write the configured single-tab serialization."""
        del tab, capture_id
        destination.write_text(self.capture_tab_text, encoding="utf-8")

    def last_command_output(self, window_id: int) -> str | None:
        """Return configured last-command output for a pane."""
        return self.command_outputs.get(window_id)

    def terminal_history(self, window_id: int) -> str | None:
        """Return configured plain scrollback for a pane."""
        if self.terminal_history_hook is not None:
            self.terminal_history_hook(window_id)
        return self.terminal_histories.get(window_id)

    def send_text(self, window_id: int, text: str) -> None:
        """Record inert prompt text sent to a restored pane."""
        self.sent_text.append((window_id, text))

    def open_snapshot(self, path: Path) -> None:
        """Record a snapshot open and expose its restored tab with a fresh ID."""
        self.opened.append(path)
        content = path.read_text(encoding="utf-8")
        self.opened_contents.append(content)
        if self.next_open_tab is not None:
            variables = {
                name: value
                for line in content.splitlines()
                for token in shlex.split(line)
                if token.startswith("--var=") and "=" in token.removeprefix("--var=")
                for name, value in (token.removeprefix("--var=").split("=", 1),)
            }
            for window in self.next_open_tab.windows:
                window.setdefault("user_vars", {}).update(variables)
            self.extra_tabs.append(self.next_open_tab)
            self.next_open_tab = None
            return
        if self.next_open_window_id is not None:
            self.window["id"] = self.next_open_window_id
            self.next_open_window_id = None
        self.include_tab = True

    def focus_tab(self, tab_id: int) -> None:
        """Record the tab selected through remote control."""
        self.focused.append(tab_id)

    def rename_tab(self, tab_id: int, title: str) -> None:
        """Rename one live tab and the fake serialization Kitty would capture."""
        tabs = self.tabs()
        tab = next((candidate for candidate in tabs if candidate.tab_id == tab_id), None)
        if tab is None:
            raise AssertionError("unknown tab rename")
        tab.title = title
        self.renamed_tabs.append((tab_id, title))
        session_id = tab.session_id()
        session_tabs = self.tabs_for_session(session_id) if session_id is not None else tabs
        tab_index = next(
            index for index, candidate in enumerate(session_tabs) if candidate.tab_id == tab_id
        )
        self.capture_session_text = rename_snapshot_tab(
            self.capture_session_text,
            tab_index,
            title,
        )

    def activate_session(self, session_id: str, tab: LiveTab) -> None:
        """Record the isolated session and its focused tab."""
        self.activated_sessions.append((session_id, tab.tab_id))
        self.focus_tab(tab.tab_id)

    def close_session_tabs(self, session_id: str) -> None:
        """Remove all matching fake tabs while recording the close boundary."""
        self.closed_sessions.append(session_id)
        for tab in self.tabs():
            for window in tab.windows:
                window.setdefault("user_vars", {}).pop(SESSION_SCOPE_VAR, None)
        if self.tab.session_id() == session_id:
            self.include_tab = False
        self.extra_tabs = [tab for tab in self.extra_tabs if tab.session_id() != session_id]

    def close_tabs(self, tab_ids: Iterable[int]) -> None:
        """Remove exactly identified fake tabs and retain every other tab."""
        identifiers = set(tab_ids)
        self.closed_tabs.extend(tab.tab_id for tab in self.tabs() if tab.tab_id in identifiers)
        if self.tab.tab_id in identifiers:
            self.include_tab = False
        self.extra_tabs = [tab for tab in self.extra_tabs if tab.tab_id not in identifiers]
