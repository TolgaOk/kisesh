"""Resolve exact resumable agent sessions from their live process state."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import assert_never

from .app_profiles import AppProfile, AppProfiles, ResumeAdapter, ResumeRestore
from .kitty_client import LiveTab

ResumeCommands = dict[int, list[str]]
OpenFiles = Mapping[int, Sequence[Path]]
OpenFileReader = Callable[[Sequence[int]], OpenFiles]
AgentResumeResolver = Callable[[Sequence[LiveTab], AppProfiles], ResumeCommands]

_PROCESS_LOOKUP_TIMEOUT_SECONDS = 2
_CODEX_TRANSCRIPT = re.compile(
    r"^rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.jsonl$",
    re.IGNORECASE,
)
_CLAUDE_TRANSCRIPT = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _LiveAgentProcess:
    """Identify one resumable foreground agent and its owning Kitty pane."""

    window_id: int
    pid: int
    adapter: ResumeAdapter
    argv: tuple[str, ...]


def _uuid(value: object) -> str | None:
    """Return one canonical UUID string or reject malformed session metadata."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def resume_argv_for_session(adapter: ResumeAdapter, value: object) -> list[str] | None:
    """Build one exact resume command from a validated external session UUID."""
    session_id = _uuid(value)
    if session_id is None:
        return None
    return _resume_argv(adapter, session_id)


def _resume_argv(adapter: ResumeAdapter, session_id: str) -> list[str]:
    """Build the adapter-specific command for an already validated session UUID."""
    match adapter:
        case "claude":
            return ["claude", "--resume", session_id]
        case "codex":
            return ["codex", "resume", session_id]
        case "pi":
            return ["pi", "--session", session_id]
        case _ as unknown:
            assert_never(unknown)


def exact_resume_argv(adapter: ResumeAdapter, value: Sequence[str]) -> list[str] | None:
    """Validate and canonicalize an exact adapter-specific resume command."""
    argv = list(value)
    match adapter:
        case "claude":
            match argv:
                case ["claude", "--resume", session_id]:
                    return resume_argv_for_session(adapter, session_id)
                case _:
                    return None
        case "codex":
            match argv:
                case ["codex", "resume", session_id]:
                    return resume_argv_for_session(adapter, session_id)
                case _:
                    return None
        case "pi":
            match argv:
                case ["pi", "--session", session_id]:
                    return resume_argv_for_session(adapter, session_id)
                case _:
                    return None
        case _ as unknown:
            assert_never(unknown)


def _flagged_session_id(argv: Sequence[str], flags: frozenset[str]) -> str | None:
    """Extract one full UUID from a supported option or its equals form."""
    for index, token in enumerate(argv[1:], start=1):
        if (
            token in flags
            and index + 1 < len(argv)
            and (session_id := _uuid(argv[index + 1])) is not None
        ):
            return session_id
        for flag in flags:
            prefix = f"{flag}="
            if (
                token.startswith(prefix)
                and (session_id := _uuid(token.removeprefix(prefix))) is not None
            ):
                return session_id
    return None


def _codex_explicit_session(argv: Sequence[str]) -> str | None:
    """Extract one full UUID following Codex's resume subcommand."""
    try:
        resume_index = argv.index("resume", 1)
    except ValueError:
        return None
    return next(
        (
            session_id
            for token in argv[resume_index + 1 :]
            if not token.startswith("-") and (session_id := _uuid(token)) is not None
        ),
        None,
    )


def _explicit_session_id(adapter: ResumeAdapter, argv: Sequence[str]) -> str | None:
    """Extract a full UUID already present in a live agent invocation."""
    match adapter:
        case "claude":
            return _flagged_session_id(argv, frozenset(("--resume", "-r", "--session-id")))
        case "codex":
            return _codex_explicit_session(argv)
        case "pi":
            return _flagged_session_id(argv, frozenset(("--session",)))
        case _ as unknown:
            assert_never(unknown)


def _codex_root_session(path: Path) -> str | None:
    """Read only Codex session metadata and exclude open subagent transcripts."""
    match = _CODEX_TRANSCRIPT.fullmatch(path.name)
    if match is None or "sessions" not in path.parts:
        return None
    session_id = _uuid(match.group(1))
    try:
        with path.open(encoding="utf-8") as transcript:
            first_line = transcript.readline()
        record = json.loads(first_line)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, Mapping) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or _uuid(payload.get("id")) != session_id:
        return None
    return None if isinstance(payload.get("source"), Mapping) else session_id


def _claude_root_session(path: Path) -> str | None:
    """Extract the UUID only from a root Claude project transcript path."""
    match = _CLAUDE_TRANSCRIPT.fullmatch(path.name)
    if match is None or len(path.parents) < 2 or path.parent.parent.name != "projects":
        return None
    return _uuid(match.group(1))


def _pi_root_session(path: Path) -> str | None:
    """Read the UUID from one open persisted Pi session header."""
    if path.suffix != ".jsonl" or "sessions" not in path.parts:
        return None
    try:
        with path.open(encoding="utf-8") as transcript:
            record = json.loads(transcript.readline())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    match record:
        case {"type": "session", "id": session_id}:
            return _uuid(session_id)
        case _:
            return None


def _session_from_open_files(adapter: ResumeAdapter, paths: Sequence[Path]) -> str | None:
    """Return one unambiguous root session referenced by an agent process."""
    match adapter:
        case "claude":
            extractor = _claude_root_session
        case "codex":
            extractor = _codex_root_session
        case "pi":
            extractor = _pi_root_session
        case _ as unknown:
            assert_never(unknown)
    sessions = {session_id for path in paths if (session_id := extractor(path)) is not None}
    return next(iter(sessions)) if len(sessions) == 1 else None


def _live_agent_process(
    window: Mapping[str, object], profiles: AppProfiles
) -> _LiveAgentProcess | None:
    """Return the first supervising resumable agent process in one Kitty pane."""
    match window:
        case {"id": int() as window_id, "foreground_processes": list() as processes} if not (
            isinstance(window_id, bool)
        ):
            pass
        case _:
            return None
    for process in reversed(processes):
        match process:
            case {"cmdline": list() as argv, "pid": int() as pid} if not isinstance(
                pid, bool
            ) and all(isinstance(item, str) for item in argv):
                profile = profiles.match(argv[0] if argv else None)
            case _:
                continue
        match profile:
            case AppProfile(restore=ResumeRestore(adapter=adapter)):
                return _LiveAgentProcess(window_id, pid, adapter, tuple(argv))
            case _:
                continue
    return None


def _proc_open_files(pid: int) -> list[Path] | None:
    """Read Linux process descriptors when procfs exposes the requested process."""
    directory = Path("/proc") / str(pid) / "fd"
    if not directory.is_dir():
        return None
    paths: list[Path] = []
    try:
        descriptors = tuple(directory.iterdir())
    except OSError:
        return paths
    for descriptor in descriptors:
        try:
            paths.append(Path(os.readlink(descriptor)))
        except OSError:
            continue
    return paths


def _requested_lsof_pid(value: str, requested: frozenset[int]) -> int | None:
    """Parse one lsof process record only when its PID was requested."""
    try:
        candidate = int(value)
    except ValueError:
        return None
    return candidate if candidate in requested else None


def process_open_files(pids: Sequence[int]) -> OpenFiles:
    """Read open paths through procfs or one bounded batched lsof invocation."""
    selected = tuple(dict.fromkeys(pid for pid in pids if pid > 0))
    paths: dict[int, list[Path]] = {}
    unresolved: list[int] = []
    for pid in selected:
        proc_paths = _proc_open_files(pid)
        if proc_paths is None:
            unresolved.append(pid)
        else:
            paths[pid] = proc_paths
    executable = shutil.which("lsof")
    if not unresolved or executable is None:
        return paths
    try:
        result = subprocess.run(
            [executable, "-a", "-p", ",".join(map(str, unresolved)), "-Fpn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROCESS_LOOKUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return paths
    requested = frozenset(unresolved)
    current_pid: int | None = None
    for line in result.stdout.splitlines():
        match line[:1]:
            case "p":
                current_pid = _requested_lsof_pid(line[1:], requested)
                if current_pid is not None:
                    paths.setdefault(current_pid, [])
            case "n" if current_pid is not None:
                paths[current_pid].append(Path(line[1:]))
    return paths


def resolve_agent_resumes(
    tabs: Sequence[LiveTab],
    profiles: AppProfiles,
    open_files: OpenFileReader = process_open_files,
) -> ResumeCommands:
    """Resolve exact per-pane resumes without persisting process IDs or transcript paths."""
    unresolved: list[_LiveAgentProcess] = []
    resumes: ResumeCommands = {}
    windows = chain.from_iterable(tab.windows for tab in tabs)
    for window in windows:
        candidate = _live_agent_process(window, profiles)
        if candidate is None:
            continue
        session_id = _explicit_session_id(candidate.adapter, candidate.argv)
        if session_id is None:
            unresolved.append(candidate)
            continue
        resumes[candidate.window_id] = _resume_argv(candidate.adapter, session_id)
    if not unresolved:
        return resumes
    try:
        paths_by_pid = open_files(tuple(candidate.pid for candidate in unresolved))
    except (OSError, subprocess.SubprocessError):
        return resumes
    for candidate in unresolved:
        discovered_session_id = _session_from_open_files(
            candidate.adapter,
            paths_by_pid.get(candidate.pid, ()),
        )
        if discovered_session_id is None:
            continue
        resumes[candidate.window_id] = _resume_argv(candidate.adapter, discovered_session_id)
    return resumes
