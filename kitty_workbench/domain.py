"""Shared data contracts for Kitty state and persisted session context."""

from __future__ import annotations

from typing import Literal, NotRequired, Required, TypeAlias, TypedDict

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class KittyProcess(TypedDict, total=False):
    """Foreground-process metadata returned by Kitty remote control."""

    cmdline: list[str]
    cwd: str
    pid: int


class KittyWindow(TypedDict, total=False):
    """Window metadata used when capturing or restoring a Kitty pane."""

    id: Required[int]
    title: str
    cwd: str
    user_vars: dict[str, str]
    foreground_processes: list[KittyProcess]
    env: dict[str, str]
    is_active: bool
    is_focused: bool
    at_prompt: bool
    in_alternate_screen: bool
    last_cmd_exit_status: int
    last_reported_cmdline: str
    last_focused_at: float
    needs_attention: bool
    has_activity_since_last_focus: bool


class KittyTabState(TypedDict, total=False):
    """Tab metadata returned inside one Kitty operating-system window."""

    id: Required[int]
    title: str
    layout: str
    windows: list[KittyWindow]
    is_active: bool
    is_focused: bool


class KittyOsWindowState(TypedDict, total=False):
    """Top-level Kitty window metadata returned by remote control."""

    id: Required[int]
    tabs: list[KittyTabState]
    is_active: bool
    is_focused: bool
    last_focused: bool


class CommandEvent(TypedDict, total=False):
    """Raw or normalized notification that a shell command completed."""

    window_id: Required[int]
    command: Required[str]
    completed_at: Required[str]
    cwd: str
    exit_status: int


class CommandRecord(TypedDict):
    """Persisted, recallable shell-history entry."""

    command: str
    completed_at: str
    cwd: NotRequired[str]
    exit_status: NotRequired[int]


RestoreKind = Literal["agent", "foreground"]


class RestoreSpec(TypedDict):
    """Validated foreground command that may be offered during restoration."""

    argv: list[str]
    command: str
    kind: RestoreKind
    auto_run: bool


class PaneContext(TypedDict):
    """Persisted state needed to reconstruct one terminal pane safely."""

    window_id: int
    title: str
    cwd: str
    program: str | None
    agent: str | None
    foreground_argv: list[str]
    foreground_command: str | None
    restore: RestoreSpec | None
    at_prompt: bool
    alternate_screen: bool
    last_exit_status: int | None
    needs_attention: bool
    had_activity: bool
    command_history: list[CommandRecord]
    last_command: str | None
    last_command_output: str | None
    last_command_output_truncated: bool
    last_output_command: str | None
    terminal_history: str | None
    terminal_history_truncated: bool
    alternate_screen_text: str | None
    alternate_screen_text_truncated: bool
    last_focused_at: NotRequired[float]


class TabContext(TypedDict):
    """Persisted state for a Kitty tab and its ordered panes."""

    title: str
    layout: str
    focused: bool
    panes: list[PaneContext]


class RestoreCandidate(RestoreSpec):
    """Restore command annotated with its pane location and labels."""

    tab_index: int
    pane_index: int
    tab_title: str
    pane_title: str
    cwd: str


class SessionContext(TypedDict):
    """Versioned command, scrollback, and foreground state for a session."""

    schema_version: int
    captured_at: str
    programs: list[str]
    agents: list[str]
    command_count: int
    restore_commands: list[RestoreCandidate]
    tabs: list[TabContext]
    snapshot_revision: NotRequired[int]


class ClosingPaneCapture(TypedDict):
    """Synchronous pane state retained while Kitty begins closing a window."""

    tab_index: int
    pane_index: int
    window: KittyWindow
    terminal_history: str
    alternate_screen_text: str
    last_command_output: str
    command_events: list[CommandEvent]
