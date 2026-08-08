"""Resolve exact resumable agent sessions from their live process state."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .app_profiles import AppProfiles, ResumeAdapter
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
    if adapter == "claude":
        return ["claude", "--resume", session_id]
    return ["codex", "resume", session_id]


def exact_resume_argv(adapter: ResumeAdapter, value: Sequence[str]) -> list[str] | None:
    """Validate and canonicalize an exact adapter-specific resume command."""
    argv = list(value)
    if adapter == "claude" and len(argv) == 3 and argv[:2] == ["claude", "--resume"]:
        return resume_argv_for_session(adapter, argv[2])
    if adapter == "codex" and len(argv) == 3 and argv[:2] == ["codex", "resume"]:
        return resume_argv_for_session(adapter, argv[2])
    return None


def _explicit_session_id(adapter: ResumeAdapter, argv: Sequence[str]) -> str | None:
    """Extract a UUID already present in a live Claude or Codex invocation."""
    if adapter == "codex":
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
    for index, token in enumerate(argv[1:], start=1):
        if (
            token in {"--resume", "-r", "--session-id"}
            and index + 1 < len(argv)
            and (session_id := _uuid(argv[index + 1]))
        ):
            return session_id
        for prefix in ("--resume=", "--session-id="):
            if token.startswith(prefix) and (session_id := _uuid(token.removeprefix(prefix))):
                return session_id
    return None


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


def _session_from_open_files(adapter: ResumeAdapter, paths: Sequence[Path]) -> str | None:
    """Return one unambiguous root session referenced by an agent process."""
    extractor = _claude_root_session if adapter == "claude" else _codex_root_session
    sessions = {session_id for path in paths if (session_id := extractor(path)) is not None}
    return next(iter(sessions)) if len(sessions) == 1 else None


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
    current_pid: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            try:
                candidate = int(line[1:])
            except ValueError:
                current_pid = None
            else:
                current_pid = candidate if candidate in unresolved else None
                if current_pid is not None:
                    paths.setdefault(current_pid, [])
        elif line.startswith("n") and current_pid is not None:
            paths[current_pid].append(Path(line[1:]))
    return paths


def resolve_agent_resumes(
    tabs: Sequence[LiveTab],
    profiles: AppProfiles,
    open_files: OpenFileReader = process_open_files,
) -> ResumeCommands:
    """Resolve exact per-pane resumes without persisting process IDs or transcript paths."""
    unresolved: list[tuple[int, int, ResumeAdapter]] = []
    resumes: ResumeCommands = {}
    for tab in tabs:
        for window in tab.windows:
            for process in reversed(window.get("foreground_processes", [])):
                argv = process.get("cmdline", [])
                profile = profiles.match(argv[0] if argv else None)
                pid = process.get("pid")
                if (
                    profile is None
                    or profile.adapter is None
                    or not isinstance(pid, int)
                    or isinstance(pid, bool)
                ):
                    continue
                session_id = _explicit_session_id(profile.adapter, argv)
                if session_id is not None:
                    command = (
                        ["claude", "--resume", session_id]
                        if profile.adapter == "claude"
                        else ["codex", "resume", session_id]
                    )
                    resumes[window["id"]] = command
                else:
                    unresolved.append((window["id"], pid, profile.adapter))
                break
    if not unresolved:
        return resumes
    try:
        paths_by_pid = open_files(tuple(item[1] for item in unresolved))
    except (OSError, subprocess.SubprocessError):
        return resumes
    for window_id, pid, adapter in unresolved:
        session_id = _session_from_open_files(adapter, paths_by_pid.get(pid, ()))
        if session_id is None:
            continue
        resumes[window_id] = (
            ["claude", "--resume", session_id]
            if adapter == "claude"
            else ["codex", "resume", session_id]
        )
    return resumes
