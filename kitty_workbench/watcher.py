"""Non-blocking Kitty watcher that schedules event-driven session snapshots."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

SESSION_ID_VAR = "kitty_workbench_session"
SESSION_SLUG_VAR = "kitty_workbench_slug"
WORKBENCH_UI_VAR = "kitty_workbench_ui"
DEBOUNCE_SECONDS = 1.25
AUTOSAVE_COMPLETION_TIMEOUT_SECONDS = 30.0
COMMAND_HISTORY_LIMIT = 2000


class WatcherChild(Protocol):
    """Child-process attributes read from Kitty watcher objects."""

    environ: object
    foreground_environ: object
    foreground_cwd: object


class WatcherLineBuffer(Protocol):
    """Text renderer exposed by Kitty's concrete line-buffer object."""

    def as_text(
        self,
        callback: Callable[[str], object],
        as_ansi: bool,
        add_wrap_markers: bool,
    ) -> None:
        """Stream visible buffer lines to a callback."""


class WatcherScreen(Protocol):
    """History and main-line buffers exposed by Kitty's screen object."""

    main_linebuf: WatcherLineBuffer

    def as_text_for_history_buf(
        self,
        callback: Callable[[str], object],
        as_ansi: bool,
        add_wrap_markers: bool,
    ) -> None:
        """Stream the in-memory scrollback buffer to a callback."""


class WatcherWindow(Protocol):
    """Window attributes required by Workbench watcher callbacks."""

    id: int
    user_vars: object
    child: WatcherChild | None
    screen: WatcherScreen

    def as_dict(self) -> Mapping[str, object]:
        """Return serializable pane metadata before the screen is destroyed."""

    def as_text(
        self,
        as_ansi: bool = False,
        add_history: bool = False,
        add_wrap_markers: bool = False,
        alternate_screen: bool = False,
        add_cursor: bool = False,
    ) -> str:
        """Return current terminal text with optional scrollback."""

    def cmd_output(self) -> str:
        """Return the most recent shell-integration output."""


class WatcherBoss(Protocol):
    """Subset of Kitty's Boss API used to find sibling panes."""

    def match_tabs(self, expression: str) -> Iterable[Iterable[WatcherWindow]]:
        """Return tabs matching a Kitty remote-control expression."""


WatcherData = Mapping[str, object]
CommandPayload = dict[str, object]
AutosavePayload = dict[str, object]

_timers: dict[str, threading.Timer] = {}
_timer_generations: dict[str, int] = {}
_pending_commands: dict[str, list[CommandPayload]] = {}
_timer_lock = threading.Lock()


def _string_mapping(value: object) -> dict[str, str]:
    """Normalize a mapping or zero-argument mapping provider to strings."""
    if callable(value):
        try:
            value = value()
        except Exception:
            return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _window_environment(window: WatcherWindow) -> dict[str, str]:
    """Merge process and foreground environments over the Kitty process values."""
    environment = os.environ.copy()
    if window.child is not None:
        environment.update(_string_mapping(window.child.environ))
        environment.update(_string_mapping(window.child.foreground_environ))
    return environment


def _sibling_session_id(window_id: int, boss: WatcherBoss | None) -> str | None:
    """Find ownership on a stamped sibling when a new split has no user variables."""
    if boss is None:
        return None
    try:
        tabs = boss.match_tabs(f"window_id:{window_id}")
        return next(
            (
                session_id
                for tab in tabs
                for sibling in tab
                if (session_id := _string_mapping(sibling.user_vars).get(SESSION_ID_VAR))
            ),
            None,
        )
    except Exception:
        return None


def _session_id(
    window: WatcherWindow,
    data: WatcherData | None = None,
    boss: WatcherBoss | None = None,
) -> str | None:
    """Resolve direct, newly assigned, or sibling-inherited session ownership."""
    if data and data.get("key") == SESSION_ID_VAR and data.get("value"):
        return str(data["value"])
    variables = _string_mapping(window.user_vars)
    if variables.get(WORKBENCH_UI_VAR):
        return None
    return variables.get(SESSION_ID_VAR) or _sibling_session_id(window.id, boss)


def _launch_autosave(
    session_id: str,
    environment: dict[str, str],
    payload: AutosavePayload,
) -> subprocess.Popen[str] | None:
    """Launch one isolated autosave process and return its completion handle."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    project = Path(__file__).resolve().parents[1]
    launcher = project / "bin" / "kitty-workbench"
    if not launcher.is_file():
        return None
    command = [str(launcher)]
    socket = environment.get("KITTY_LISTEN_ON")
    if socket:
        command.extend(("--socket", socket))
    command.extend(("autosave", session_id, "--payload-stdin"))
    try:
        process: subprocess.Popen[str] = subprocess.Popen(
            command,
            cwd=project,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except OSError:
        return None
    try:
        if process.stdin is not None:
            process.stdin.write(encoded)
            process.stdin.close()
    except (BrokenPipeError, OSError):
        return process
    return process


def _run_autosave(
    session_id: str,
    environment: dict[str, str],
    generation: int,
) -> None:
    """Drain events only when invoked by the session's current debounce timer."""
    with _timer_lock:
        if _timer_generations.get(session_id) != generation:
            return
        _timers.pop(session_id, None)
        command_events = list(_pending_commands.get(session_id, []))
    process = _launch_autosave(
        session_id,
        environment,
        {"command_events": command_events},
    )
    if process is None:
        return
    try:
        return_code = process.wait(timeout=AUTOSAVE_COMPLETION_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return
    if return_code:
        return
    saved_event_ids = {id(event) for event in command_events}
    with _timer_lock:
        pending = _pending_commands.get(session_id)
        if pending is None:
            return
        pending[:] = [event for event in pending if id(event) not in saved_event_ids]
        if not pending:
            _pending_commands.pop(session_id, None)


def _read_window_text(reader: Callable[[], object]) -> str:
    """Read one unstable Kitty text API without breaking its close path."""
    try:
        value = reader()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _read_hidden_main_buffer(window: WatcherWindow) -> str:
    """Read main-screen scrollback while a full-screen application is visible."""
    parts: list[str] = []
    try:
        window.screen.as_text_for_history_buf(parts.append, True, False)
        window.screen.main_linebuf.as_text(parts.append, True, False)
    except Exception:
        return ""
    return "".join(parts)


def _session_location(
    window: WatcherWindow,
    boss: WatcherBoss | None,
    session_id: str,
) -> tuple[int, int]:
    """Locate a pane within only the tabs owned by its Workbench session."""
    if boss is None:
        return -1, -1
    try:
        all_tabs = [list(tab) for tab in boss.match_tabs("all")]
        session_tabs = [
            tab
            for tab in all_tabs
            if any(
                _string_mapping(sibling.user_vars).get(SESSION_ID_VAR) == session_id
                for sibling in tab
            )
        ]
    except Exception:
        return -1, -1
    return next(
        (
            (tab_index, pane_index)
            for tab_index, tab in enumerate(session_tabs)
            for pane_index, sibling in enumerate(tab)
            if sibling.id == window.id
        ),
        (-1, -1),
    )


_WINDOW_STATE_KEYS = (
    "id",
    "title",
    "cwd",
    "user_vars",
    "foreground_processes",
    "is_active",
    "is_focused",
    "at_prompt",
    "in_alternate_screen",
    "last_cmd_exit_status",
    "last_reported_cmdline",
    "last_focused_at",
    "needs_attention",
    "has_activity_since_last_focus",
)


def _closing_pane_capture(
    window: WatcherWindow,
    boss: WatcherBoss | None,
    session_id: str,
    command_events: list[CommandPayload],
) -> AutosavePayload:
    """Capture scrollback and metadata synchronously before Kitty destroys them."""
    try:
        raw_window = window.as_dict()
    except Exception:
        raw_window = {"id": window.id}
    state = {key: raw_window[key] for key in _WINDOW_STATE_KEYS if key in raw_window}
    state["id"] = window.id
    tab_index, pane_index = _session_location(window, boss, session_id)
    alternate_screen = bool(state.get("in_alternate_screen"))
    terminal_history = (
        _read_hidden_main_buffer(window)
        if alternate_screen
        else _read_window_text(lambda: window.as_text(as_ansi=True, add_history=True))
    )
    return {
        "tab_index": tab_index,
        "pane_index": pane_index,
        "window": state,
        "terminal_history": terminal_history,
        "alternate_screen_text": (
            _read_window_text(lambda: window.as_text(as_ansi=True, alternate_screen=True))
            if alternate_screen
            else ""
        ),
        "last_command_output": _read_window_text(window.cmd_output),
        "command_events": command_events,
    }


def _drain_closing_events(session_id: str) -> list[CommandPayload]:
    """Cancel a delayed save and atomically take its pending command events."""
    with _timer_lock:
        timer = _timers.pop(session_id, None)
        _timer_generations[session_id] = _timer_generations.get(session_id, 0) + 1
        if timer is not None:
            timer.cancel()
        return _pending_commands.pop(session_id, [])


def _schedule(
    window: WatcherWindow,
    data: WatcherData | None = None,
    boss: WatcherBoss | None = None,
    command_event: CommandPayload | None = None,
) -> None:
    """Replace a session's one-shot timer while retaining bounded command events."""
    session_id = _session_id(window, data, boss)
    if not session_id:
        return
    environment = _window_environment(window)
    with _timer_lock:
        generation = _timer_generations.get(session_id, 0) + 1
        timer = threading.Timer(
            DEBOUNCE_SECONDS,
            _run_autosave,
            (session_id, environment, generation),
        )
        timer.daemon = True
        if command_event is not None:
            pending = _pending_commands.setdefault(session_id, [])
            pending.append(command_event)
            del pending[:-COMMAND_HISTORY_LIMIT]
        previous = _timers.get(session_id)
        if previous is not None:
            previous.cancel()
        _timers[session_id] = timer
        _timer_generations[session_id] = generation
    timer.start()


def on_resize(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Schedule a snapshot after a pane-size or layout change."""
    _schedule(window, data, boss)


def on_focus_change(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Ignore focus-only changes because the next material event captures focus."""
    del boss, window, data


def on_close(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Persist pane text synchronously before Kitty destroys its screen buffer."""
    session_id = _session_id(window, data, boss)
    if not session_id:
        return
    capture = _closing_pane_capture(
        window,
        boss,
        session_id,
        _drain_closing_events(session_id),
    )
    _launch_autosave(
        session_id,
        _window_environment(window),
        {"closing_pane": capture},
    )


def on_set_user_var(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Schedule only ownership-variable changes and ignore unrelated metadata."""
    if data.get("key") in {SESSION_ID_VAR, SESSION_SLUG_VAR}:
        _schedule(window, data, boss)


def on_title_change(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Schedule a snapshot after a pane title changes."""
    _schedule(window, data, boss)


def _foreground_cwd(window: WatcherWindow) -> str | None:
    """Read a child foreground cwd whether Kitty exposes a value or callable."""
    if window.child is None:
        return None
    cwd = window.child.foreground_cwd
    if callable(cwd):
        try:
            cwd = cwd()
        except Exception:
            return None
    return str(cwd) if cwd else None


def on_cmd_startstop(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Queue completed shell commands and ignore command-start notifications."""
    if data.get("is_start"):
        return
    raw_command = data.get("cmdline")
    command: object = (
        [str(item) for item in raw_command]
        if isinstance(raw_command, (list, tuple))
        else str(raw_command or "")
    )
    completed_at = data.get("time")
    if (
        not isinstance(completed_at, (int, float))
        or isinstance(completed_at, bool)
        or completed_at <= 1_000_000_000
    ):
        completed_at = time.time()
    event: CommandPayload = {
        "window_id": window.id,
        "command": command,
        "completed_at": completed_at,
    }
    cwd = _foreground_cwd(window)
    if cwd:
        event["cwd"] = cwd
    _schedule(window, data, boss, event)


def on_tab_bar_dirty(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Schedule a snapshot after tabs are created, moved, or retitled."""
    _schedule(window, data, boss)
