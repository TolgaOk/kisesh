"""Kitty-native close routing that protects a tracked session's final tab."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from .legacy import VARIABLE_ALIASES as LEGACY_VARIABLE_ALIASES
from .model import KISESH_UI_VAR, SESSION_ID_VAR, SESSION_SLUG_VAR
from .paths import runtime_root


class CloseGuardChild(Protocol):
    """Child-process environment attributes exposed by a Kitty window."""

    environ: object
    foreground_environ: object


class CloseGuardWindow(Protocol):
    """Window attributes required to route one close request safely."""

    id: int
    user_vars: object
    child: CloseGuardChild | None
    overlay_parent: object

    def set_user_var(self, key: str, value: str | bytes | None) -> None:
        """Set one Kitty user variable on the window."""


class CloseGuardTab(Protocol):
    """Tab identity and panes required by the close guard."""

    id: int
    os_window_id: int

    def __iter__(self) -> Iterator[CloseGuardWindow]:
        """Iterate over the tab's windows."""


ConfirmationCallback = Callable[..., None]


class CloseGuardBoss(Protocol):
    """Subset of Kitty's in-process API used by the close guard."""

    active_window: CloseGuardWindow | None
    active_tab: CloseGuardTab | None
    window_id_map: Mapping[int, CloseGuardWindow]

    def match_tabs(self, expression: str) -> Iterable[CloseGuardTab]:
        """Return tabs matching a Kitty expression."""

    def close_tab(self, tab: CloseGuardTab | None = None) -> None:
        """Close an exact Kitty tab."""

    def close_window(self) -> None:
        """Close the active Kitty pane or overlay."""

    def confirm(
        self,
        message: str,
        callback: ConfirmationCallback,
        *args: object,
        window: CloseGuardWindow | None = None,
        confirm_on_cancel: bool = False,
        confirm_on_accept: bool = True,
        title: str = "",
    ) -> CloseGuardWindow:
        """Open a native confirmation overlay and invoke its callback."""


@dataclass(frozen=True, slots=True)
class TabOwnership:
    """Normalized and consistency-checked ownership for one Kitty tab."""

    session_id: str | None
    label: str | None
    consistent: bool


@dataclass(frozen=True, slots=True)
class CloseRequest:
    """Durable close parameters passed from Kitty to the KiSesh CLI."""

    session_id: str
    os_window_id: int
    environment: dict[str, str]


_pending_sessions: set[str] = set()
_pending_lock = threading.Lock()


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


def _compatible_variable(variables: Mapping[str, str], name: str) -> str | None:
    """Resolve a current Kitty user variable with its previous-name fallback."""
    value = variables.get(name)
    if value:
        return value
    return variables.get(LEGACY_VARIABLE_ALIASES[name])


def _window_environment(window: CloseGuardWindow) -> dict[str, str]:
    """Merge child and foreground environments over Kitty's own environment."""
    environment = os.environ.copy()
    if window.child is not None:
        environment.update(_string_mapping(window.child.environ))
        environment.update(_string_mapping(window.child.foreground_environ))
    return environment


def _tab_ownership(tab: CloseGuardTab) -> TabOwnership:
    """Resolve one unambiguous session identity from every pane in a tab."""
    variables = [_string_mapping(window.user_vars) for window in tab]
    session_ids = {
        value for item in variables if (value := _compatible_variable(item, SESSION_ID_VAR))
    }
    if len(session_ids) > 1:
        return TabOwnership(None, None, False)
    session_id = next(iter(session_ids), None)
    labels = {
        value for item in variables if (value := _compatible_variable(item, SESSION_SLUG_VAR))
    }
    label = next(iter(labels)) if len(labels) == 1 else session_id
    return TabOwnership(session_id, label, True)


def _tracked_tab_count(boss: CloseGuardBoss, session_id: str) -> int | None:
    """Count a session's tabs, returning no answer for inconsistent Kitty state."""
    try:
        ownership = [_tab_ownership(tab) for tab in boss.match_tabs("all")]
    except Exception:
        return None
    if any(not item.consistent for item in ownership):
        return None
    return sum(item.session_id == session_id for item in ownership)


def _reserve_session(session_id: str) -> bool:
    """Reserve one final-session close and reject repeated requests in flight."""
    with _pending_lock:
        if session_id in _pending_sessions:
            return False
        _pending_sessions.add(session_id)
        return True


def _release_session(session_id: str) -> None:
    """Release a completed, cancelled, or failed final-session close."""
    with _pending_lock:
        _pending_sessions.discard(session_id)


def _launch_close(request: CloseRequest) -> subprocess.Popen[str] | None:
    """Launch the shell-free save-close operation without blocking Kitty."""
    project = runtime_root()
    launcher = project / "bin" / "kisesh"
    if not launcher.is_file():
        return None
    command = [str(launcher)]
    socket = request.environment.get("KISESH_TARGET_SOCKET") or request.environment.get(
        "KITTY_LISTEN_ON"
    )
    if socket:
        command.extend(("--socket", socket))
    command.extend(
        (
            "close",
            request.session_id,
            "--promote-os-window",
            str(request.os_window_id),
        )
    )
    try:
        process: subprocess.Popen[str] = subprocess.Popen(
            command,
            cwd=project,
            env=request.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except OSError:
        return None
    return process


def _wait_for_close(session_id: str, process: subprocess.Popen[str]) -> None:
    """Hold the close reservation until its isolated CLI process exits."""
    try:
        process.wait()
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        _release_session(session_id)


def _run_close_request(request: CloseRequest) -> None:
    """Launch and track one reserved close entirely within its worker thread."""
    process = _launch_close(request)
    if process is None:
        _release_session(request.session_id)
        return
    _wait_for_close(request.session_id, process)


def _confirmed_close(confirmed: bool, request: CloseRequest) -> None:
    """Launch a confirmed close or release its reservation after cancellation."""
    if not confirmed:
        _release_session(request.session_id)
        return
    waiter = threading.Thread(
        target=_run_close_request,
        args=(request,),
        daemon=True,
    )
    try:
        waiter.start()
    except RuntimeError:
        _release_session(request.session_id)


def _close_transient_window(boss: CloseGuardBoss, window: CloseGuardWindow) -> bool:
    """Close a KiSesh or Kitty overlay without touching its underlying tab."""
    try:
        variables = _string_mapping(window.user_vars)
        transient = window.overlay_parent is not None or bool(
            _compatible_variable(variables, KISESH_UI_VAR)
        )
    except Exception:
        return True
    if not transient:
        return False
    with suppress(Exception):
        boss.close_window()
    return True


def _active_window(target_window_id: int, boss: CloseGuardBoss) -> CloseGuardWindow | None:
    """Resolve the mapped target only while it remains Kitty's active window."""
    try:
        window = boss.window_id_map.get(target_window_id)
        active = boss.active_window
    except Exception:
        return None
    return (
        window
        if window is not None and active is not None and active.id == target_window_id
        else None
    )


def _active_tab(target_window_id: int, boss: CloseGuardBoss) -> CloseGuardTab | None:
    """Resolve the active tab only when it still contains the invocation pane."""
    try:
        tab = boss.active_tab
        if tab is None:
            return None
        return tab if target_window_id in {candidate.id for candidate in tab} else None
    except Exception:
        return None


def _request_tracked_close(
    boss: CloseGuardBoss,
    window: CloseGuardWindow,
    tab: CloseGuardTab,
    session_id: str,
    label: str,
) -> None:
    """Close a non-final tracked tab or reserve and confirm a final close."""
    tab_count = _tracked_tab_count(boss, session_id)
    if tab_count is None or tab_count < 1:
        return
    if tab_count > 1:
        with suppress(Exception):
            boss.close_tab(tab)
        return
    if not _reserve_session(session_id):
        return
    request = CloseRequest(
        session_id,
        tab.os_window_id,
        _window_environment(window),
    )
    try:
        prompt = boss.confirm(
            f'Save and close the final tab of "{label}"?',
            _confirmed_close,
            request,
            window=window,
            confirm_on_cancel=False,
            confirm_on_accept=False,
            title="Close KiSesh session",
        )
    except Exception:
        _release_session(session_id)
        return
    with suppress(Exception):
        prompt.set_user_var(KISESH_UI_VAR, "yes")


def request_tab_close(target_window_id: int, boss: CloseGuardBoss) -> None:
    """Close an ordinary tab immediately or guard a tracked session's final tab."""
    window = _active_window(target_window_id, boss)
    if window is None or _close_transient_window(boss, window):
        return
    tab = _active_tab(target_window_id, boss)
    if tab is None:
        return
    try:
        ownership = _tab_ownership(tab)
    except Exception:
        return
    if not ownership.consistent:
        return
    if ownership.session_id is None:
        with suppress(Exception):
            boss.close_tab(tab)
        return
    _request_tracked_close(
        boss,
        window,
        tab,
        ownership.session_id,
        ownership.label or "this session",
    )
