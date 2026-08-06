"""Non-blocking Kitty watcher that schedules event-driven session snapshots."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol, cast

SESSION_ID_VAR = "kisesh_session"
SESSION_SLUG_VAR = "kisesh_slug"
SESSION_NAME_VAR = "kisesh_name"
SESSION_SCOPE_VAR = "kisesh_scope"
AGENT_VAR = "kisesh_agent"
APP_VAR = "kisesh_app"
KISESH_UI_VAR = "kisesh_ui"
RESTORE_LAYOUT_VAR = "kisesh_restore_layout"
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
    """Window attributes required by KiSesh watcher callbacks."""

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
    """Subset of Kitty's Boss API used for identity and internal remote control."""

    def match_tabs(self, expression: str) -> Iterable[Iterable[WatcherWindow]]:
        """Return tabs matching a Kitty remote-control expression."""

    def call_remote_control(
        self,
        window: WatcherWindow,
        command: tuple[str, ...],
    ) -> object:
        """Execute a remote-control command inside Kitty's process."""


class WatcherAppProfile(Protocol):
    """Configured app identity needed for lightweight agent classification."""

    name: str
    agent: bool


class WatcherAppProfiles(Protocol):
    """Configured app lookup exposed through the installed KiSesh package."""

    def match(self, command: str | None) -> WatcherAppProfile | None:
        """Return the profile matching one executable basename."""


WatcherData = Mapping[str, object]
CommandPayload = dict[str, object]
AutosavePayload = dict[str, object]


@dataclass(slots=True, frozen=True)
class WindowIdentity:
    """Ownership and focus metadata for one pane observed by the watcher."""

    window: WatcherWindow
    session_id: str | None
    session_slug: str | None
    session_name: str | None
    session_scope: str | None
    native_session_name: str | None
    last_focused_at: float
    kisesh_ui: bool


_timers: dict[str, threading.Timer] = {}
_timer_generations: dict[str, int] = {}
_pending_commands: dict[str, list[CommandPayload]] = {}
_timer_lock = threading.Lock()


def _runtime_root() -> Path:
    """Resolve a direct Kitty import or packaged command to its stable runtime."""
    configured = os.environ.get("KISESH_INSTALL_ROOT")
    if configured:
        return Path(configured).expanduser()
    source = Path(__file__).absolute().parents[1]
    if (source / "bin" / "kisesh").is_file():
        return source
    return Path("~/.local/lib/kisesh").expanduser()


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


@cache
def _legacy_variable_aliases() -> Mapping[str, str]:
    """Load previous user-variable names without requiring a package import at startup."""
    project_root = str(_runtime_root())
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    module = importlib.import_module("kisesh.legacy")
    return cast(Mapping[str, str], module.VARIABLE_ALIASES)


def _variable(variables: Mapping[str, str], name: str) -> str | None:
    """Resolve a current watcher variable with its previous-name fallback."""
    value = variables.get(name)
    if value:
        return value
    return variables.get(_legacy_variable_aliases()[name])


def _variable_name_matches(value: object, name: str) -> bool:
    """Report whether an event key uses a current or previous variable name."""
    return value == name or value == _legacy_variable_aliases()[name]


def _window_environment(window: WatcherWindow) -> dict[str, str]:
    """Merge process and foreground environments over the Kitty process values."""
    environment = os.environ.copy()
    if window.child is not None:
        environment.update(_string_mapping(window.child.environ))
        environment.update(_string_mapping(window.child.foreground_environ))
    return environment


def _window_identity(window: WatcherWindow) -> WindowIdentity:
    """Normalize the unstable Kitty window object for inheritance decisions."""
    try:
        state = window.as_dict()
    except Exception:
        state = {}
    variables = _string_mapping(window.user_vars)
    native_name = state.get("session_name")
    focused_at = state.get("last_focused_at")
    return WindowIdentity(
        window=window,
        session_id=_variable(variables, SESSION_ID_VAR),
        session_slug=_variable(variables, SESSION_SLUG_VAR),
        session_name=_variable(variables, SESSION_NAME_VAR),
        session_scope=_variable(variables, SESSION_SCOPE_VAR),
        native_session_name=(
            native_name.strip() if isinstance(native_name, str) and native_name.strip() else None
        ),
        last_focused_at=(
            float(focused_at)
            if isinstance(focused_at, (int, float)) and not isinstance(focused_at, bool)
            else 0.0
        ),
        kisesh_ui=bool(_variable(variables, KISESH_UI_VAR)),
    )


def _tab_inheritance(
    window_id: int,
    boss: WatcherBoss,
) -> tuple[WindowIdentity, tuple[int, ...]] | None:
    """Select one unambiguous native-session owner and unstamped target tab."""
    try:
        tabs = [
            [_window_identity(candidate) for candidate in tab]
            for tab in boss.match_tabs("state:focused_os_window")
        ]
    except Exception:
        return None
    source_tab = next(
        (tab for tab in tabs if any(identity.window.id == window_id for identity in tab)),
        None,
    )
    if source_tab is None:
        return None
    source = next(identity for identity in source_tab if identity.window.id == window_id)
    if source.kisesh_ui or source.session_id is not None:
        return None
    sibling_owners = [identity for identity in source_tab if identity.session_id is not None]
    owners = sibling_owners
    if not owners and source.native_session_name is not None:
        owners = [
            identity
            for tab in tabs
            for identity in tab
            if identity.session_id is not None
            and identity.native_session_name == source.native_session_name
        ]
    owner_ids = {identity.session_id for identity in owners}
    if len(owner_ids) != 1:
        return None
    owner = max(owners, key=lambda identity: identity.last_focused_at)
    if owner.session_id is None or owner.session_slug is None or owner.session_scope is None:
        return None
    targets = tuple(
        identity.window.id
        for identity in source_tab
        if identity.session_id is None and not identity.kisesh_ui
    )
    return (owner, targets) if targets else None


def _inherit_tab_ownership(boss: WatcherBoss, window: WatcherWindow) -> str | None:
    """Stamp a new native-session tab without launching an external process."""
    inheritance = _tab_inheritance(window.id, boss)
    if inheritance is None:
        return None
    owner, window_ids = inheritance
    match = " or ".join(f"id:{window_id}" for window_id in window_ids)
    try:
        boss.call_remote_control(
            window,
            (
                "set-user-vars",
                "--match",
                match,
                f"{SESSION_ID_VAR}={owner.session_id}",
                f"{SESSION_SLUG_VAR}={owner.session_slug}",
                f"{SESSION_NAME_VAR}={owner.session_name or owner.session_slug}",
                f"{SESSION_SCOPE_VAR}={owner.session_scope}",
            ),
        )
    except Exception:
        return None
    return owner.session_id


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
                if (
                    session_id := _variable(
                        _string_mapping(sibling.user_vars),
                        SESSION_ID_VAR,
                    )
                )
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
    if data and _variable_name_matches(data.get("key"), SESSION_ID_VAR) and data.get("value"):
        return str(data["value"])
    variables = _string_mapping(window.user_vars)
    if _variable(variables, KISESH_UI_VAR):
        return None
    return _variable(variables, SESSION_ID_VAR) or _sibling_session_id(window.id, boss)


def _matching_tab(
    window: WatcherWindow,
    boss: WatcherBoss,
) -> tuple[object, tuple[WatcherWindow, ...]] | None:
    """Return the exact native tab object and a stable view of its windows."""
    try:
        tab = next(iter(boss.match_tabs(f"window_id:{window.id}")), None)
        return (tab, tuple(tab)) if tab is not None else None
    except Exception:
        return None


def _tab_has_kisesh_ui(window: WatcherWindow, boss: WatcherBoss) -> bool:
    """Report whether a temporary manager currently covers the pane's tab."""
    matched = _matching_tab(window, boss)
    return matched is not None and any(
        _variable(_string_mapping(sibling.user_vars), KISESH_UI_VAR) for sibling in matched[1]
    )


def _pause_manager_tab_autosaves(window: WatcherWindow, boss: WatcherBoss) -> None:
    """Cancel layout timers while retaining pending command events for the next save."""
    matched = _matching_tab(window, boss)
    session_ids = (
        {
            session_id
            for sibling in matched[1]
            if (
                session_id := _variable(
                    _string_mapping(sibling.user_vars),
                    SESSION_ID_VAR,
                )
            )
        }
        if matched is not None
        else set()
    )
    with _timer_lock:
        for session_id in session_ids:
            timer = _timers.pop(session_id, None)
            _timer_generations[session_id] = _timer_generations.get(session_id, 0) + 1
            if timer is not None:
                timer.cancel()


def _schedule_manager_tab_autosave(window: WatcherWindow, boss: WatcherBoss) -> None:
    """Resume persistence from one owned content pane after temporary UI state ends."""
    matched = _matching_tab(window, boss)
    content = (
        next(
            (
                sibling
                for sibling in matched[1]
                if not _variable(_string_mapping(sibling.user_vars), KISESH_UI_VAR)
                and _variable(_string_mapping(sibling.user_vars), SESSION_ID_VAR)
            ),
            None,
        )
        if matched is not None
        else None
    )
    if content is not None:
        _schedule(content, boss=boss)


def _restore_manager_layout(window: WatcherWindow, boss: WatcherBoss) -> bool:
    """Restore a closing manager's tab before allowing ordinary close capture."""
    variables = _string_mapping(window.user_vars)
    if not _variable(variables, KISESH_UI_VAR):
        return False
    layout = variables.get(RESTORE_LAYOUT_VAR, "").strip()
    matched = _matching_tab(window, boss)
    if not layout or matched is None:
        return True
    tab, _ = matched
    restore = cast(Callable[[str], object] | None, getattr(tab, "goto_layout", None))
    if not callable(restore):
        return True
    try:
        restore(layout)
    except Exception:
        return True
    _schedule_manager_tab_autosave(window, boss)
    return True


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
    project = _runtime_root()
    launcher = project / "bin" / "kisesh"
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
    """Locate a pane within only the tabs owned by its KiSesh session."""
    if boss is None:
        return -1, -1
    try:
        all_tabs = [list(tab) for tab in boss.match_tabs("all")]
        session_tabs = [
            tab
            for tab in all_tabs
            if any(
                _variable(_string_mapping(sibling.user_vars), SESSION_ID_VAR) == session_id
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


def _has_other_session_tab(
    window: WatcherWindow,
    boss: WatcherBoss | None,
    session_id: str,
) -> bool:
    """Report whether the closing pane's session owns another Kitty tab."""
    if boss is None:
        return False
    try:
        tabs = [list(tab) for tab in boss.match_tabs("all")]
    except Exception:
        return False
    closing_tab = next(
        (tab for tab in tabs if any(sibling.id == window.id for sibling in tab)),
        None,
    )
    if closing_tab is None:
        return False
    return any(
        tab is not closing_tab
        and any(
            _variable(_string_mapping(sibling.user_vars), SESSION_ID_VAR) == session_id
            for sibling in tab
        )
        for tab in tabs
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
    if not _tab_has_kisesh_ui(window, boss):
        _schedule(window, data, boss)


def on_focus_change(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Ignore focus-only changes because the next material event captures focus."""
    del boss, window, data


def on_close(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Persist closing text, then resave layouts that retain other session tabs."""
    if _restore_manager_layout(window, boss):
        return
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
    if _has_other_session_tab(window, boss, session_id):
        _schedule(window, boss=boss)


def on_set_user_var(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Schedule only ownership-variable changes and ignore unrelated metadata."""
    if data.get("key") == RESTORE_LAYOUT_VAR:
        if data.get("value"):
            _pause_manager_tab_autosaves(window, boss)
        else:
            _schedule_manager_tab_autosave(window, boss)
        return
    if any(
        _variable_name_matches(data.get("key"), name)
        for name in (SESSION_ID_VAR, SESSION_SLUG_VAR, SESSION_NAME_VAR)
    ):
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


def _command_arguments(value: object) -> tuple[str, ...]:
    """Normalize shell-integration command metadata without executing it."""
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item))
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        return tuple(shlex.split(value, posix=True))
    except ValueError:
        return ()


def _refreshed_app_profiles() -> WatcherAppProfiles:
    """Load event-cached profiles even when Kitty imported this watcher by path."""
    project_root = str(_runtime_root())
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    module = importlib.import_module("kisesh.app_profiles")
    return cast(WatcherAppProfiles, module.refresh_app_profiles())


def _command_profile(value: object) -> WatcherAppProfile | None:
    """Recognize a configured application including common command wrappers."""
    arguments = _command_arguments(value)
    index = 0
    while index < len(arguments):
        executable = Path(arguments[index]).name.casefold()
        if executable in {"command", "exec"}:
            index += 1
            continue
        if executable == "env":
            index += 1
            while index < len(arguments) and (
                arguments[index].startswith("-") or "=" in arguments[index]
            ):
                index += 1
            continue
        return _refreshed_app_profiles().match(executable)
    return None


def _update_app_markers(
    boss: WatcherBoss,
    window: WatcherWindow,
    profile: WatcherAppProfile | None,
) -> None:
    """Cache lightweight app and agent markers for the in-process native tab bar."""
    current = _string_mapping(window.user_vars)
    application = profile.name if profile is not None else None
    agent = profile.name if profile is not None and profile.agent else None
    current_desired = ((APP_VAR, application), (AGENT_VAR, agent))
    desired = (
        *current_desired,
        *((_legacy_variable_aliases()[name], None) for name, _value in current_desired),
    )
    assignments = tuple(
        f"{name}={value}" if value is not None else name
        for name, value in desired
        if current.get(name) != value and not (current.get(name) is None and value is None)
    )
    if not assignments:
        return
    try:
        boss.call_remote_control(
            window,
            ("set-user-vars", "--match", f"id:{window.id}", *assignments),
        )
    except Exception:
        return


def on_cmd_startstop(boss: WatcherBoss, window: WatcherWindow, data: WatcherData) -> None:
    """Update app markers and queue completed shell commands for saving."""
    raw_command = data.get("cmdline")
    if data.get("is_start"):
        _update_app_markers(boss, window, _command_profile(raw_command))
        return
    _update_app_markers(boss, window, None)
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
    """Inherit new native-session tabs before scheduling their snapshot."""
    inherited_session_id = _inherit_tab_ownership(boss, window)
    inheritance_event = (
        {"key": SESSION_ID_VAR, "value": inherited_session_id}
        if inherited_session_id is not None
        else data
    )
    _schedule(window, inheritance_event, boss)
