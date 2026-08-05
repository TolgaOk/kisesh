"""Typed command-line interface for interactive and scriptable operations."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

import tyro

from .context import normalize_command_event, pane_last_command_output
from .domain import ClosingPaneCapture, CommandEvent, JsonObject, KittyWindow
from .kitty_client import KittyClient, KittyError
from .panel import PanelError, hide_quick_access_panel, is_panel_process
from .paths import data_root
from .service import WorkbenchError, WorkbenchService
from .shell_restore import run_restored_shell
from .store import SessionStore, StoreError
from .tui import SessionManager

PositionalString = Annotated[str, tyro.conf.Positional]
OptionalPositionalString = Annotated[str | None, tyro.conf.Positional]
INVALID_EVENTS_MESSAGE = "autosave command events must be a JSON list of objects"
INVALID_PAYLOAD_MESSAGE = "autosave input must be a JSON object"
INVALID_CLOSE_MESSAGE = "autosave closing-pane input is incomplete"


@dataclass(frozen=True, slots=True)
class Manager:
    """Open the interactive session manager."""


@dataclass(frozen=True, slots=True)
class ListSessions:
    """List known sessions and their lifecycle state."""

    json: bool = False
    """Emit structured JSON instead of tab-separated text."""


@dataclass(frozen=True, slots=True)
class ShowContext:
    """Show saved commands, output, history, and restore actions."""

    session: PositionalString
    """Session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class PrintLastOutput:
    """Print one pane's inert saved shell output."""

    session: PositionalString
    """Session name, slug, or identifier."""

    tab_index: int
    """Zero-based tab index in the saved context."""

    pane_index: int
    """Zero-based pane index in the selected tab."""


@dataclass(frozen=True, slots=True)
class RestoreShell:
    """Start an internal shell populated from saved pane context."""

    session: PositionalString
    """Session name, slug, or identifier."""

    tab_index: int
    """Zero-based tab index in the saved context."""

    pane_index: int
    """Zero-based pane index in the selected tab."""


@dataclass(frozen=True, slots=True)
class CreateSession:
    """Create a session from the focused Kitty tab."""

    name: PositionalString
    """Human-readable session name."""

    root: str | None = None
    """Project root override; defaults to the focused pane directory."""


@dataclass(frozen=True, slots=True)
class AddTab:
    """Add the focused tab to a live session."""

    session: PositionalString
    """Target session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class DetachTab:
    """Detach the focused tab while leaving it running."""

    session: PositionalString
    """Owning session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class CopyTab:
    """Copy the focused tab layout into an inactive session."""

    session: PositionalString
    """Target session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class SaveSession:
    """Save a live session or the session owning the focused tab."""

    session: OptionalPositionalString = None
    """Optional session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class OpenSession:
    """Focus a live session or restore a saved session."""

    session: PositionalString
    """Session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class RenameSession:
    """Rename a session without changing its stable identifier."""

    session: PositionalString
    """Session name, slug, or identifier."""

    name: PositionalString
    """New human-readable name."""


@dataclass(frozen=True, slots=True)
class ArchiveSession:
    """Move an inactive session into the archived list."""

    session: PositionalString
    """Session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class UnarchiveSession:
    """Return an archived session to the active list."""

    session: PositionalString
    """Session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class RemoveSession:
    """Move an inactive session into recoverable trash."""

    session: PositionalString
    """Session name, slug, or identifier."""


@dataclass(frozen=True, slots=True)
class AutosaveSession:
    """Save a session from a Kitty watcher event."""

    session_id: PositionalString
    """Stable identifier of the session to save."""

    payload_stdin: Annotated[
        bool,
        tyro.conf.arg(aliases=("--events-stdin",)),
    ] = False
    """Read watcher events and optional pre-close pane state from standard input."""


@dataclass(frozen=True, slots=True)
class Doctor:
    """Check storage, snapshots, context, and Kitty state."""


Command = (
    Annotated[Manager, tyro.conf.subcommand(name="manager")]
    | Annotated[ListSessions, tyro.conf.subcommand(name="list")]
    | Annotated[ShowContext, tyro.conf.subcommand(name="context")]
    | Annotated[PrintLastOutput, tyro.conf.subcommand(name="print-last-output")]
    | Annotated[RestoreShell, tyro.conf.subcommand(name="restore-shell")]
    | Annotated[CreateSession, tyro.conf.subcommand(name="create")]
    | Annotated[
        AddTab,
        tyro.conf.subcommand(name="add-tab"),
    ]
    | Annotated[DetachTab, tyro.conf.subcommand(name="detach-tab")]
    | Annotated[CopyTab, tyro.conf.subcommand(name="copy-tab")]
    | Annotated[SaveSession, tyro.conf.subcommand(name="save")]
    | Annotated[OpenSession, tyro.conf.subcommand(name="open")]
    | Annotated[RenameSession, tyro.conf.subcommand(name="rename")]
    | Annotated[ArchiveSession, tyro.conf.subcommand(name="archive")]
    | Annotated[
        UnarchiveSession,
        tyro.conf.subcommand(name="unarchive", aliases=("restore-archive",)),
    ]
    | Annotated[
        RemoveSession,
        tyro.conf.subcommand(name="remove", aliases=("trash",)),
    ]
    | Annotated[AutosaveSession, tyro.conf.subcommand(name="autosave")]
    | Annotated[Doctor, tyro.conf.subcommand(name="doctor")]
)

ReadCommand = ListSessions | ShowContext | PrintLastOutput | RestoreShell
MembershipCommand = CreateSession | AddTab | DetachTab | CopyTab | SaveSession | OpenSession
LifecycleCommand = RenameSession | ArchiveSession | UnarchiveSession | RemoveSession
MaintenanceCommand = AutosaveSession | Doctor


@dataclass(frozen=True, slots=True)
class CliConfig:
    """Global connection and storage options paired with one subcommand."""

    command: tyro.conf.OmitSubcommandPrefixes[Command]
    """Operation selected by the user."""

    data_dir: Path | None = None
    """Override the session data directory."""

    kitty: str | None = None
    """Override the Kitty executable path."""

    socket: str | None = None
    """Override the Kitty remote-control socket."""


def parse_arguments(argv: list[str] | None = None) -> CliConfig:
    """Parse arguments into a fully typed global configuration."""
    return tyro.cli(
        CliConfig,
        prog="kitty-workbench",
        description="Kitty-native recoverable session management.",
        args=argv,
        config=(
            tyro.conf.CascadeSubcommandArgs,
            tyro.conf.HelptextFromCommentsOff,
        ),
    )


def _needs_kitty(command: Command) -> bool:
    """Report whether a command requires an eager live Kitty client."""
    offline_types = (
        ListSessions,
        ShowContext,
        PrintLastOutput,
        RestoreShell,
    )
    return not isinstance(command, offline_types)


def _service(config: CliConfig) -> WorkbenchService:
    """Construct storage and optional live Kitty dependencies."""
    store = SessionStore(data_root(config.data_dir))
    connect = _needs_kitty(config.command) or bool(config.kitty or config.socket)
    kitty = KittyClient(executable=config.kitty, socket=config.socket) if connect else None
    return WorkbenchService(store, kitty)


def _run_manager(service: WorkbenchService) -> int:
    """Run the interactive manager with optional resident-panel dismissal."""
    dismiss = hide_quick_access_panel if is_panel_process() else None
    return SessionManager(service, on_dismiss=dismiss).run()


def _run_read(command: ReadCommand, service: WorkbenchService) -> int:
    """Execute a listing or persisted-context read without mutating sessions."""
    if isinstance(command, ListSessions):
        views = service.views()
        if command.json:
            payload: list[JsonObject] = []
            for view in views:
                item = view.stored.manifest.to_dict()
                item["live"] = view.live
                item["live_tab_ids"] = [tab.tab_id for tab in view.live_tabs]
                payload.append(item)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        for view in views:
            state = "live" if view.live else view.stored.manifest.status
            print(f"{view.stored.manifest.slug}\t{state}\t{view.stored.manifest.name}")
        return 0
    if isinstance(command, ShowContext):
        context = service.context(command.session)
        print(json.dumps(context or {}, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    if isinstance(command, PrintLastOutput):
        output = pane_last_command_output(
            service.context(command.session),
            command.tab_index,
            command.pane_index,
        )
        if output:
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
        return 0
    stored = service.store.get(command.session)
    run_restored_shell(
        stored,
        service.context(stored.manifest.id),
        command.tab_index,
        command.pane_index,
    )


def _run_membership(command: MembershipCommand, service: WorkbenchService) -> int:
    """Execute creation, tab membership, save, or open operations."""
    if isinstance(command, CreateSession):
        stored = service.create_from_active(command.name, command.root)
    elif isinstance(command, AddTab):
        stored = service.add_current_tab(command.session)
    elif isinstance(command, DetachTab):
        stored = service.detach_current_tab(command.session)
    elif isinstance(command, CopyTab):
        stored = service.copy_current_tab(command.session)
    elif isinstance(command, SaveSession):
        stored = service.save(command.session) if command.session else service.save_current()
    else:
        stored = service.open(command.session)
    print(stored.manifest.slug)
    return 0


def _run_lifecycle(command: LifecycleCommand, service: WorkbenchService) -> int:
    """Execute rename, archive, unarchive, and recoverable removal operations."""
    if isinstance(command, RenameSession):
        result = service.rename(command.session, command.name).manifest.slug
    elif isinstance(command, ArchiveSession):
        result = service.archive(command.session).manifest.slug
    elif isinstance(command, UnarchiveSession):
        result = service.unarchive(command.session).manifest.slug
    else:
        result = str(service.remove(command.session))
    print(result)
    return 0


def _normalized_events(value: object) -> list[CommandEvent]:
    """Validate a JSON event array and normalize each complete command record."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(INVALID_EVENTS_MESSAGE)
    return [event for item in value if (event := normalize_command_event(item)) is not None]


def _closing_capture(value: object) -> ClosingPaneCapture | None:
    """Validate an optional synchronous Kitty pre-close pane capture."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(INVALID_CLOSE_MESSAGE)
    raw_window = value.get("window")
    window_id = raw_window.get("id") if isinstance(raw_window, dict) else None
    tab_index = value.get("tab_index")
    pane_index = value.get("pane_index")
    terminal_history = value.get("terminal_history")
    last_output = value.get("last_command_output")
    valid_indices = all(
        isinstance(index, int) and not isinstance(index, bool)
        for index in (window_id, tab_index, pane_index)
    )
    if (
        not valid_indices
        or not isinstance(terminal_history, str)
        or not isinstance(last_output, str)
    ):
        raise ValueError(INVALID_CLOSE_MESSAGE)
    return {
        "tab_index": cast(int, tab_index),
        "pane_index": cast(int, pane_index),
        "window": cast(KittyWindow, raw_window),
        "terminal_history": terminal_history,
        "last_command_output": last_output,
        "command_events": _normalized_events(value.get("command_events", [])),
    }


def _autosave_payload_from_stdin(
    enabled: bool,
) -> tuple[list[CommandEvent], ClosingPaneCapture | None]:
    """Decode watcher events and optional synchronous closing-pane state."""
    if not enabled:
        return [], None
    payload: object = json.load(sys.stdin)
    if isinstance(payload, list):
        return _normalized_events(payload), None
    if not isinstance(payload, dict):
        raise ValueError(INVALID_PAYLOAD_MESSAGE)
    return (
        _normalized_events(payload.get("command_events", [])),
        _closing_capture(payload.get("closing_pane")),
    )


def _run_maintenance(command: MaintenanceCommand, service: WorkbenchService) -> int:
    """Execute watcher autosave or diagnostic maintenance."""
    if isinstance(command, AutosaveSession):
        events, closing_pane = _autosave_payload_from_stdin(command.payload_stdin)
        if closing_pane is None:
            service.save(command.session_id, events)
        else:
            service.save_closing_pane(command.session_id, closing_pane)
        return 0
    findings = service.doctor()
    print("\n".join(findings))
    return int(any(finding.startswith("ERROR") for finding in findings))


def _dispatch(config: CliConfig, service: WorkbenchService) -> int:
    """Route one typed command to its cohesive operation family."""
    command = config.command
    if isinstance(command, Manager):
        return _run_manager(service)
    if isinstance(command, (ListSessions, ShowContext, PrintLastOutput, RestoreShell)):
        return _run_read(command, service)
    if isinstance(command, (CreateSession, AddTab, DetachTab, CopyTab, SaveSession, OpenSession)):
        return _run_membership(command, service)
    if isinstance(
        command,
        (
            RenameSession,
            ArchiveSession,
            UnarchiveSession,
            RemoveSession,
        ),
    ):
        return _run_lifecycle(command, service)
    return _run_maintenance(command, service)


def main(argv: list[str] | None = None) -> int:
    """Parse, dispatch, and translate expected operational failures to status one."""
    config = parse_arguments(argv)
    try:
        return _dispatch(config, _service(config))
    except (
        KittyError,
        PanelError,
        StoreError,
        WorkbenchError,
        OSError,
        ValueError,
    ) as error:
        print(f"kitty-workbench: {error}", file=sys.stderr)
        return 1
