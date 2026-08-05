"""Capture, normalize, query, and restore typed terminal session context."""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .domain import (
    ClosingPaneCapture,
    CommandEvent,
    CommandRecord,
    KittyWindow,
    PaneContext,
    RestoreCandidate,
    RestoreKind,
    RestoreSpec,
    SessionContext,
    TabContext,
)
from .kitty_client import LiveTab
from .model import utc_now

CONTEXT_SCHEMA_VERSION = 1
COMMAND_HISTORY_LIMIT = 2000
COMMAND_LENGTH_LIMIT = 4096
ARGUMENT_COUNT_LIMIT = 64
ARGUMENT_LENGTH_LIMIT = 2048
LAST_COMMAND_OUTPUT_LIMIT = 64 * 1024
TERMINAL_HISTORY_LINE_LIMIT = 2000
TERMINAL_HISTORY_CHARACTER_LIMIT = 1024 * 1024

ContextInput = Mapping[str, object] | None
PaneLocation = tuple[int, int]

_AGENTS = {
    "aider": "aider",
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "opencode": "opencode",
}
_SHELLS = {"ash", "bash", "dash", "fish", "nu", "pwsh", "sh", "tcsh", "zsh"}
_INTERACTIVE_PROGRAMS = {
    "aider",
    "btop",
    "claude",
    "codex",
    "gemini",
    "helix",
    "htop",
    "hx",
    "lazygit",
    "nvim",
    "opencode",
    "top",
    "vi",
    "vim",
    "yazi",
}


@dataclass(slots=True, frozen=True)
class BoundedText:
    """Sanitized terminal text paired with its truncation state."""

    text: str
    truncated: bool = False


@dataclass(slots=True, frozen=True)
class ScreenCapture:
    """Separate normal scrollback from an alternate-screen application's frame."""

    normal: BoundedText
    alternate: BoundedText


def _mapping(value: object) -> Mapping[str, object] | None:
    """Narrow an untrusted value to a string-keyed mapping."""
    return value if isinstance(value, Mapping) else None


def _items(value: object) -> list[object]:
    """Narrow an untrusted value to a JSON-style list."""
    return list(value) if isinstance(value, list) else []


def _integer(value: object) -> int | None:
    """Parse a non-boolean integer from runtime or persisted metadata."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _clean_text(value: object, limit: int) -> str:
    """Normalize a scalar to bounded text without embedded null bytes."""
    return str(value or "").replace("\x00", "").strip()[:limit]


def _plain_terminal_text(value: object) -> str:
    """Keep printable text, newlines, and tabs while dropping terminal controls."""
    if not isinstance(value, str):
        return ""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        character
        for character in normalized
        if character in "\n\t"
        or (
            ord(character) >= 0x20 and ord(character) != 0x7F and not 0x80 <= ord(character) <= 0x9F
        )
    )


def _bounded_command_output(value: object) -> BoundedText:
    """Keep the useful tail of one completed command's inert output."""
    plain = _plain_terminal_text(value)
    if not plain.strip():
        return BoundedText("")
    truncated = len(plain) > LAST_COMMAND_OUTPUT_LIMIT
    return BoundedText(plain[-LAST_COMMAND_OUTPUT_LIMIT:] if truncated else plain, truncated)


def _bounded_terminal_history(value: object) -> BoundedText:
    """Keep at most the newest 2,000 logical lines of inert terminal text."""
    plain = _plain_terminal_text(value)
    if not plain.strip():
        return BoundedText("")
    lines = plain.splitlines(keepends=True)
    truncated = len(lines) > TERMINAL_HISTORY_LINE_LIMIT
    bounded = "".join(lines[-TERMINAL_HISTORY_LINE_LIMIT:] if truncated else lines)
    if len(bounded) <= TERMINAL_HISTORY_CHARACTER_LIMIT:
        return BoundedText(bounded, truncated)
    bounded = bounded[-TERMINAL_HISTORY_CHARACTER_LIMIT:]
    if "\n" in bounded:
        bounded = bounded.split("\n", 1)[1]
    return BoundedText(bounded, True)


def _command_argv(value: object) -> list[str]:
    """Normalize a command string or sequence to bounded nonempty arguments."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        raw = list(value)
    elif isinstance(value, str) and value.strip():
        try:
            raw = shlex.split(value, posix=True)
        except ValueError:
            return []
    else:
        return []
    argv = [_clean_text(item, ARGUMENT_LENGTH_LIMIT) for item in raw[:ARGUMENT_COUNT_LIMIT]]
    return [item for item in argv if item]


def _command_text(value: object) -> str:
    """Normalize command metadata to one bounded shell-rendered string."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        argv = _command_argv(value)
        return shlex.join(argv)[:COMMAND_LENGTH_LIMIT] if argv else ""
    return _clean_text(value, COMMAND_LENGTH_LIMIT)


def _command_name(value: object) -> str | None:
    """Return the normalized executable basename for command metadata."""
    argv = _command_argv(value)
    if not argv:
        return None
    name = Path(argv[0]).name.lstrip("-").strip()
    return name or None


def _foreground_argv(window: KittyWindow) -> list[str]:
    """Return the deepest foreground process command reported for a pane."""
    for process in reversed(window.get("foreground_processes", [])):
        argv = _command_argv(process.get("cmdline"))
        if argv:
            return argv
    return []


def _agent(program: str | None) -> str | None:
    """Classify a known coding-agent executable by exact or suffixed name."""
    if not program:
        return None
    normalized = program.casefold()
    return next(
        (
            label
            for token, label in _AGENTS.items()
            if normalized == token or normalized.startswith(f"{token}-")
        ),
        None,
    )


def _exit_status(value: object) -> int | None:
    """Parse shell exit status metadata without treating booleans as integers."""
    return _integer(value)


def _event_time(value: object) -> str:
    """Normalize numeric or ISO event times to the persisted UTC representation."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 1_000_000_000:
        try:
            parsed = datetime.fromtimestamp(float(value), UTC)
        except (OSError, OverflowError, ValueError):
            parsed = None
        if parsed is not None:
            return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            return aware.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc_now()


def normalize_command_event(raw: Mapping[str, object]) -> CommandEvent | None:
    """Validate one watcher event and discard records lacking identity or command."""
    command = _command_text(raw.get("command", raw.get("cmdline")))
    window_id = _integer(raw.get("window_id"))
    if not command or window_id is None:
        return None
    event: CommandEvent = {
        "window_id": window_id,
        "command": command,
        "completed_at": _event_time(raw.get("completed_at", raw.get("time"))),
    }
    cwd = _clean_text(raw.get("cwd"), 4096)
    status = _exit_status(raw.get("exit_status"))
    if cwd:
        event["cwd"] = cwd
    if status is not None:
        event["exit_status"] = status
    return event


def _history(value: object) -> list[CommandRecord]:
    """Validate and bound persisted command-history records."""
    entries: list[CommandRecord] = []
    for item in _items(value)[-COMMAND_HISTORY_LIMIT:]:
        raw = _mapping(item)
        if raw is None:
            continue
        command = _command_text(raw.get("command"))
        if not command:
            continue
        entry: CommandRecord = {
            "command": command,
            "completed_at": _event_time(raw.get("completed_at")),
        }
        cwd = _clean_text(raw.get("cwd"), 4096)
        status = _exit_status(raw.get("exit_status"))
        if cwd:
            entry["cwd"] = cwd
        if status is not None:
            entry["exit_status"] = status
        entries.append(entry)
    return entries


def _append_history(
    history: list[CommandRecord],
    event: Mapping[str, object],
    fallback_cwd: str,
) -> None:
    """Append a normalized event unless it duplicates the latest captured event."""
    command = _command_text(event.get("command"))
    if not command:
        return
    entry: CommandRecord = {
        "command": command,
        "completed_at": _event_time(event.get("completed_at")),
    }
    cwd = _clean_text(event.get("cwd"), 4096) or fallback_cwd
    status = _exit_status(event.get("exit_status"))
    if cwd:
        entry["cwd"] = cwd
    if status is not None:
        entry["exit_status"] = status
    if history and all(
        history[-1].get(key) == entry.get(key) for key in ("command", "completed_at")
    ):
        return
    history.append(entry)
    del history[:-COMMAND_HISTORY_LIMIT]


def _claude_resume(argv: Sequence[str]) -> list[str]:
    """Reduce a live Claude invocation to its stable direct-resume form."""
    for index, token in enumerate(argv[1:], start=1):
        if token in {"--resume", "-r"} and index + 1 < len(argv):
            session = argv[index + 1]
            if not session.startswith("-"):
                return ["claude", "--resume", session]
        if token.startswith("--resume=") and token.partition("=")[2]:
            return ["claude", "--resume", token.partition("=")[2]]
        if token in {"--continue", "-c"}:
            return ["claude", "--continue"]
    return ["claude", "--continue"]


def _codex_resume(argv: Sequence[str]) -> list[str]:
    """Reduce a live Codex invocation to its stable direct-resume form."""
    try:
        resume_index = argv.index("resume", 1)
    except ValueError:
        return ["codex", "resume", "--last"]
    tail = list(argv[resume_index + 1 :])
    if "--last" in tail:
        return ["codex", "resume", "--last"]
    session = next((item for item in tail if item and not item.startswith("-")), None)
    return ["codex", "resume", session] if session else ["codex", "resume", "--last"]


def _restore_command(
    argv: Sequence[str],
    *,
    agent: str | None,
    alternate_screen: bool,
) -> RestoreSpec | None:
    """Build a safe foreground restore specification from live process metadata."""
    program = _command_name(argv)
    if not argv or not program or program.casefold() in _SHELLS:
        return None
    if agent == "claude":
        restore_argv = _claude_resume(argv)
    elif agent == "codex":
        restore_argv = _codex_resume(argv)
    else:
        restore_argv = list(argv)
    kind: RestoreKind = "agent" if agent else "foreground"
    return {
        "argv": restore_argv,
        "command": shlex.join(restore_argv),
        "kind": kind,
        "auto_run": bool(agent or alternate_screen or program.casefold() in _INTERACTIVE_PROGRAMS),
    }


@dataclass(slots=True)
class _PaneIndex:
    """Resolve prior pane context by stable window ID or layout position."""

    by_window: dict[int, Mapping[str, object]]
    by_position: dict[PaneLocation, Mapping[str, object]]

    @classmethod
    def from_context(cls, context: ContextInput) -> _PaneIndex:
        """Index every well-formed pane in an optional prior context."""
        by_window: dict[int, Mapping[str, object]] = {}
        by_position: dict[PaneLocation, Mapping[str, object]] = {}
        tabs = _items(context.get("tabs")) if context is not None else []
        for tab_index, item in enumerate(tabs):
            tab = _mapping(item)
            if tab is None:
                continue
            for pane_index, pane_item in enumerate(_items(tab.get("panes"))):
                pane = _mapping(pane_item)
                if pane is None:
                    continue
                by_position[(tab_index, pane_index)] = pane
                window_id = _integer(pane.get("window_id"))
                if window_id is not None:
                    by_window[window_id] = pane
        return cls(by_window, by_position)

    def resolve(self, window_id: int, location: PaneLocation) -> Mapping[str, object]:
        """Prefer a stable pane identity and fall back to its layout location."""
        return self.by_window.get(window_id) or self.by_position.get(location) or {}


def _summarize(tabs: list[TabContext], captured_at: str) -> SessionContext:
    """Build derived command, program, agent, and restore indexes for a context."""
    programs: set[str] = set()
    agents: set[str] = set()
    restores: list[RestoreCandidate] = []
    command_count = 0
    for tab_index, tab in enumerate(tabs):
        for pane_index, pane in enumerate(tab["panes"]):
            if pane["program"]:
                programs.add(pane["program"])
            if pane["agent"]:
                agents.add(pane["agent"])
            command_count += len(pane["command_history"])
            restore = pane["restore"]
            if restore is not None:
                restores.append(
                    {
                        **restore,
                        "tab_index": tab_index,
                        "pane_index": pane_index,
                        "tab_title": tab["title"],
                        "pane_title": pane["title"],
                        "cwd": pane["cwd"],
                    }
                )
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "captured_at": captured_at,
        "programs": sorted(programs, key=str.casefold),
        "agents": sorted(agents, key=str.casefold),
        "command_count": command_count,
        "restore_commands": restores,
        "tabs": tabs,
    }


class _ContextBuilder:
    """Build one session context from normalized live and prior state."""

    def __init__(
        self,
        existing: ContextInput,
        command_events: Iterable[Mapping[str, object]],
        command_outputs: Mapping[int, object],
        terminal_histories: Mapping[int, object],
    ) -> None:
        """Index prior panes and normalized completion events once per capture."""
        self.captured_at = utc_now()
        self.old_panes = _PaneIndex.from_context(existing)
        self.events_by_window: dict[int, list[CommandEvent]] = {}
        self.command_outputs = command_outputs
        self.terminal_histories = terminal_histories
        for raw_event in command_events:
            event = normalize_command_event(raw_event)
            if event is not None:
                self.events_by_window.setdefault(event["window_id"], []).append(event)

    def build(self, tabs: Iterable[LiveTab]) -> SessionContext:
        """Capture all tabs and derive the top-level context indexes."""
        context_tabs: list[TabContext] = []
        for tab_index, tab in enumerate(tabs):
            panes = [
                self._build_pane(window, (tab_index, pane_index))
                for pane_index, window in enumerate(tab.windows)
            ]
            context_tabs.append(
                {
                    "title": tab.title,
                    "layout": tab.layout,
                    "focused": tab.is_focused,
                    "panes": panes,
                }
            )
        return _summarize(context_tabs, self.captured_at)

    def _build_pane(self, window: KittyWindow, location: PaneLocation) -> PaneContext:
        """Combine live process, terminal, and command state for one pane."""
        window_id = window["id"]
        old = self.old_panes.resolve(window_id, location)
        cwd = _clean_text(window.get("cwd"), 4096)
        events = self.events_by_window.get(window_id, [])
        history = self._command_history(window, old, events, cwd)
        argv = _foreground_argv(window)
        program = _command_name(argv)
        agent = _agent(program)
        alternate_screen = bool(window.get("in_alternate_screen"))
        output, output_command = self._last_output(window, old, events, history)
        screens = self._screens(window_id, old, alternate_screen)
        pane: PaneContext = {
            "window_id": window_id,
            "title": _clean_text(window.get("title"), 1024),
            "cwd": cwd,
            "program": program,
            "agent": agent,
            "foreground_argv": argv,
            "foreground_command": shlex.join(argv) if argv else None,
            "restore": _restore_command(
                argv,
                agent=agent,
                alternate_screen=alternate_screen,
            ),
            "at_prompt": bool(window.get("at_prompt")),
            "alternate_screen": alternate_screen,
            "last_exit_status": _exit_status(window.get("last_cmd_exit_status")),
            "needs_attention": bool(window.get("needs_attention")),
            "had_activity": bool(window.get("has_activity_since_last_focus")),
            "command_history": history,
            "last_command": history[-1]["command"] if history else None,
            "last_command_output": output.text or None,
            "last_command_output_truncated": bool(output.text and output.truncated),
            "last_output_command": output_command if output.text else None,
            "terminal_history": screens.normal.text or None,
            "terminal_history_truncated": bool(screens.normal.text and screens.normal.truncated),
            "alternate_screen_text": screens.alternate.text or None,
            "alternate_screen_text_truncated": bool(
                screens.alternate.text and screens.alternate.truncated
            ),
        }
        last_focused = window.get("last_focused_at")
        if isinstance(last_focused, (int, float)) and not isinstance(last_focused, bool):
            pane["last_focused_at"] = float(last_focused)
        return pane

    def _command_history(
        self,
        window: KittyWindow,
        old: Mapping[str, object],
        events: Sequence[CommandEvent],
        cwd: str,
    ) -> list[CommandRecord]:
        """Merge prior history, watcher events, and prompt metadata without duplicates."""
        history = _history(old.get("command_history"))
        for event_index, original in enumerate(events):
            event: Mapping[str, object] = original
            if "exit_status" not in original and event_index == len(events) - 1:
                status = _exit_status(window.get("last_cmd_exit_status"))
                if status is not None:
                    event = {**original, "exit_status": status}
            _append_history(history, event, cwd)
        last_reported = _command_text(window.get("last_reported_cmdline"))
        if (
            bool(window.get("at_prompt"))
            and last_reported
            and (not history or history[-1]["command"] != last_reported)
        ):
            _append_history(
                history,
                {
                    "command": last_reported,
                    "completed_at": self.captured_at,
                    "exit_status": window.get("last_cmd_exit_status"),
                },
                cwd,
            )
        return history

    def _last_output(
        self,
        window: KittyWindow,
        old: Mapping[str, object],
        events: Sequence[CommandEvent],
        history: Sequence[CommandRecord],
    ) -> tuple[BoundedText, str | None]:
        """Select new completed output without erasing a just-restored prior capture."""
        old_output = _bounded_command_output(old.get("last_command_output"))
        old_output = BoundedText(
            old_output.text,
            bool(old.get("last_command_output_truncated")) or old_output.truncated,
        )
        old_command = _command_text(old.get("last_output_command")) or None
        window_id = window["id"]
        if window_id not in self.command_outputs:
            return old_output, old_command
        captured = _bounded_command_output(self.command_outputs[window_id])
        last_reported = _command_text(window.get("last_reported_cmdline"))
        observed_new_command = bool(events) or bool(
            last_reported and last_reported != _command_text(old.get("last_command"))
        )
        if not captured.text and not observed_new_command:
            return old_output, old_command
        command = history[-1]["command"] if captured.text and history else None
        return captured, command

    def _screens(
        self,
        window_id: int,
        old: Mapping[str, object],
        alternate_screen: bool,
    ) -> ScreenCapture:
        """Keep normal scrollback distinct from a full-screen application's frame."""
        normal = _bounded_terminal_history(old.get("terminal_history"))
        normal = BoundedText(
            normal.text,
            bool(old.get("terminal_history_truncated")) or normal.truncated,
        )
        alternate = _bounded_terminal_history(old.get("alternate_screen_text"))
        alternate = BoundedText(
            alternate.text,
            bool(old.get("alternate_screen_text_truncated")) or alternate.truncated,
        )
        if window_id not in self.terminal_histories:
            return ScreenCapture(normal, alternate)
        captured = _bounded_terminal_history(self.terminal_histories[window_id])
        if not captured.text:
            return ScreenCapture(normal, alternate)
        return (
            ScreenCapture(normal, captured)
            if alternate_screen
            else ScreenCapture(captured, alternate)
        )


def build_context(
    tabs: Iterable[LiveTab],
    existing: ContextInput = None,
    command_events: Iterable[Mapping[str, object]] = (),
    command_outputs: Mapping[int, object] | None = None,
    terminal_histories: Mapping[int, object] | None = None,
) -> SessionContext:
    """Capture typed pane context, bounded history, and safe resume candidates."""
    builder = _ContextBuilder(
        existing,
        command_events,
        command_outputs or {},
        terminal_histories or {},
    )
    return builder.build(tabs)


def _context_tabs(context: ContextInput) -> list[TabContext]:
    """Return structurally valid tab mappings from current or legacy context."""
    tabs = _items(context.get("tabs")) if context is not None else []
    return [cast(TabContext, tab) for tab in tabs if isinstance(tab, Mapping)]


def _copied_context_tabs(context: ContextInput) -> list[TabContext]:
    """Copy tab and pane containers before a targeted context mutation."""
    copied: list[TabContext] = []
    for tab in _context_tabs(context):
        panes = [cast(PaneContext, dict(pane)) for pane in tab.get("panes", [])]
        copied.append(
            {
                "title": str(tab.get("title", "")),
                "layout": str(tab.get("layout", "splits")),
                "focused": bool(tab.get("focused")),
                "panes": panes,
            }
        )
    return copied


def _preserve_snapshot_revision(context: SessionContext, existing: ContextInput) -> None:
    """Carry a valid layout revision through context-only updates."""
    revision = _integer(existing.get("snapshot_revision")) if existing is not None else None
    if revision is not None:
        context["snapshot_revision"] = revision


def remap_context_windows(existing: ContextInput, tabs: Sequence[LiveTab]) -> SessionContext:
    """Replace stale pane IDs after restoration without changing saved terminal state."""
    context_tabs = _copied_context_tabs(existing)
    for tab_index, live_tab in enumerate(tabs):
        if tab_index >= len(context_tabs):
            break
        panes = context_tabs[tab_index]["panes"]
        for pane_index, window in enumerate(live_tab.windows[: len(panes)]):
            panes[pane_index]["window_id"] = window["id"]
    captured_at = _clean_text(
        existing.get("captured_at") if existing is not None else None,
        128,
    )
    remapped = _summarize(context_tabs, captured_at or utc_now())
    _preserve_snapshot_revision(remapped, existing)
    return remapped


def _closing_pane_location(
    tabs: Sequence[TabContext],
    capture: ClosingPaneCapture,
) -> PaneLocation | None:
    """Resolve a closing pane by current window ID, then its session position."""
    window_id = capture["window"]["id"]
    for tab_index, tab in enumerate(tabs):
        for pane_index, pane in enumerate(tab["panes"]):
            if pane["window_id"] == window_id:
                return tab_index, pane_index
    fallback = capture["tab_index"], capture["pane_index"]
    if 0 <= fallback[0] < len(tabs) and 0 <= fallback[1] < len(tabs[fallback[0]]["panes"]):
        return fallback
    return None


def update_context_for_closing_pane(
    existing: ContextInput,
    capture: ClosingPaneCapture,
) -> SessionContext:
    """Merge synchronous pre-close text and commands into one persisted pane."""
    tabs = _copied_context_tabs(existing)
    location = _closing_pane_location(tabs, capture)
    if location is None:
        raise ValueError("closing pane is absent from the saved session context")
    window_id = capture["window"]["id"]
    builder = _ContextBuilder(
        existing,
        capture["command_events"],
        {window_id: capture["last_command_output"]},
        {window_id: capture["terminal_history"]},
    )
    tab_index, pane_index = location
    tabs[tab_index]["panes"][pane_index] = builder._build_pane(capture["window"], location)
    updated = _summarize(tabs, builder.captured_at)
    _preserve_snapshot_revision(updated, existing)
    return updated


def merge_context(existing: ContextInput, addition: ContextInput) -> SessionContext:
    """Append copied tab context while rebuilding every derived top-level index."""
    tabs = [*_context_tabs(existing), *_context_tabs(addition)]
    captured_at = _clean_text(
        addition.get("captured_at") if addition is not None else None,
        128,
    )
    return _summarize(tabs, captured_at or utc_now())


def _pane_context(
    context: ContextInput,
    tab_index: int,
    pane_index: int,
) -> Mapping[str, object] | None:
    """Resolve one pane from untrusted context indexes without raising."""
    tabs = _items(context.get("tabs")) if context is not None else []
    if not 0 <= tab_index < len(tabs):
        return None
    tab = _mapping(tabs[tab_index])
    panes = _items(tab.get("panes")) if tab is not None else []
    if not 0 <= pane_index < len(panes):
        return None
    return _mapping(panes[pane_index])


def pane_last_command_output(
    context: ContextInput,
    tab_index: int,
    pane_index: int,
) -> str:
    """Return one pane's sanitized last-completed-command output."""
    pane = _pane_context(context, tab_index, pane_index)
    return _bounded_command_output(pane.get("last_command_output") if pane else None).text


def pane_terminal_history(
    context: ContextInput,
    tab_index: int,
    pane_index: int,
) -> str:
    """Return inert normal-buffer scrollback with legacy last-output fallback."""
    pane = _pane_context(context, tab_index, pane_index)
    if pane is None:
        return ""
    history = _bounded_terminal_history(pane.get("terminal_history")).text
    return history or _bounded_command_output(pane.get("last_command_output")).text


def pane_alternate_screen_text(
    context: ContextInput,
    tab_index: int,
    pane_index: int,
) -> str:
    """Return the latest static frame captured from an alternate-screen program."""
    pane = _pane_context(context, tab_index, pane_index)
    return _bounded_terminal_history(pane.get("alternate_screen_text") if pane else None).text


def pane_command_history(
    context: ContextInput,
    tab_index: int,
    pane_index: int,
) -> list[str]:
    """Return bounded shell commands in oldest-to-newest recall order."""
    pane = _pane_context(context, tab_index, pane_index)
    return [entry["command"] for entry in _history(pane.get("command_history") if pane else None)]


def _safe_prompt_text(value: object) -> str:
    """Flatten a reminder so send-text can never execute terminal controls."""
    command = " ".join(_command_text(value).replace("\r", "\n").splitlines())
    printable = "".join(
        character for character in command if ord(character) >= 0x20 and character != "\x7f"
    )
    return printable.strip()[:COMMAND_LENGTH_LIMIT]


def pending_restore_commands(context: ContextInput) -> dict[PaneLocation, str]:
    """Return non-auto-run commands as inert, single-line prompt reminders."""
    pending: dict[PaneLocation, str] = {}
    raw_commands = _items(context.get("restore_commands")) if context is not None else []
    for item in raw_commands:
        raw = _mapping(item)
        if raw is None or bool(raw.get("auto_run")):
            continue
        tab_index = _integer(raw.get("tab_index"))
        pane_index = _integer(raw.get("pane_index"))
        if tab_index is None or pane_index is None:
            continue
        command = _safe_prompt_text(raw.get("command"))
        if not command:
            argv = _command_argv(raw.get("argv"))
            command = _safe_prompt_text(shlex.join(argv) if argv else "")
        if command:
            pending[(tab_index, pane_index)] = command
    return pending


def _auto_run_commands(context: ContextInput) -> dict[PaneLocation, list[str]]:
    """Index validated auto-run commands by their tab and pane location."""
    commands: dict[PaneLocation, list[str]] = {}
    raw_commands = _items(context.get("restore_commands")) if context is not None else []
    for item in raw_commands:
        raw = _mapping(item)
        if raw is None or not bool(raw.get("auto_run")):
            continue
        tab_index = _integer(raw.get("tab_index"))
        pane_index = _integer(raw.get("pane_index"))
        argv = _command_argv(raw.get("argv"))
        if tab_index is not None and pane_index is not None and argv:
            commands[(tab_index, pane_index)] = argv
    return commands


def _shell_state_locations(
    context: ContextInput,
    commands: Mapping[PaneLocation, Sequence[str]],
    shell_restore_argv: Sequence[str],
) -> set[PaneLocation]:
    """Find normal-shell panes that contain scrollback or recallable commands."""
    if not shell_restore_argv:
        return set()
    locations: set[PaneLocation] = set()
    for tab_index, tab in enumerate(_context_tabs(context)):
        for pane_index, pane in enumerate(tab["panes"]):
            location = (tab_index, pane_index)
            has_scrollback = bool(pane_terminal_history(context, *location))
            has_commands = bool(_history(pane.get("command_history")))
            if location not in commands and (has_scrollback or has_commands):
                locations.add(location)
    return locations


def _restored_launch(
    line: str,
    location: PaneLocation,
    command: Sequence[str] | None,
    shell_states: set[PaneLocation],
    shell_restore_argv: Sequence[str],
) -> str | None:
    """Append a foreground command or shell-state helper to one launch line."""
    if command is None and location not in shell_states:
        return None
    try:
        launch = shlex.split(line.strip(), posix=True)
    except ValueError:
        return None
    if not launch or launch[0] != "launch":
        return None
    suffix = (
        list(command)
        if command is not None
        else [
            *shell_restore_argv,
            "--tab-index",
            str(location[0]),
            "--pane-index",
            str(location[1]),
        ]
    )
    return shlex.join([*launch, *suffix])


def restore_session(
    snapshot: str,
    context: ContextInput,
    *,
    shell_restore_argv: Sequence[str] = (),
) -> str:
    """Inject validated foreground resumes or inert shell-state restoration."""
    if context is None:
        return snapshot
    commands = _auto_run_commands(context)
    shell_states = _shell_state_locations(context, commands, shell_restore_argv)
    if not commands and not shell_states:
        return snapshot
    output: list[str] = []
    tab_index = -1
    pane_index = 0
    for raw_line in snapshot.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("new_tab"):
            tab_index += 1
            pane_index = 0
            output.append(raw_line)
            continue
        if stripped.startswith("launch"):
            location = (max(0, tab_index), pane_index)
            pane_index += 1
            replacement = _restored_launch(
                raw_line,
                location,
                commands.get(location),
                shell_states,
                shell_restore_argv,
            )
            if replacement is not None:
                output.append(replacement)
                continue
        output.append(raw_line)
    return "\n".join(output).rstrip() + "\n"
