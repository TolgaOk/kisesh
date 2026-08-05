"""Sanitize Kitty session grammar into inert, restorable layout snapshots."""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from .model import (
    AGENT_VAR,
    APP_VAR,
    CAPTURE_VAR,
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    WORKBENCH_UI_VAR,
    SessionManifest,
    SnapshotSummary,
    session_marker_name,
)


class _OptionPolicy(Enum):
    """Describe whether a launch option and its value are safe to preserve."""

    KEEP_VALUE = auto()
    KEEP_FLAG = auto()
    DROP_VALUE = auto()
    DROP_FLAG = auto()


@dataclass(frozen=True, slots=True)
class _TransientUiLocations:
    """Transient lines and tab sections that cannot enter a durable snapshot."""

    launch_lines: frozenset[int]
    contaminated_tabs: frozenset[int]
    transient_only_tabs: frozenset[int]


_OPTION_POLICIES = {
    "--bias": _OptionPolicy.KEEP_VALUE,
    "--color": _OptionPolicy.KEEP_VALUE,
    "--cwd": _OptionPolicy.KEEP_VALUE,
    "--location": _OptionPolicy.KEEP_VALUE,
    "--logo": _OptionPolicy.KEEP_VALUE,
    "--logo-alpha": _OptionPolicy.KEEP_VALUE,
    "--logo-position": _OptionPolicy.KEEP_VALUE,
    "--marker": _OptionPolicy.KEEP_VALUE,
    "--spacing": _OptionPolicy.KEEP_VALUE,
    "--tab-title": _OptionPolicy.KEEP_VALUE,
    "--title": _OptionPolicy.KEEP_VALUE,
    "--window-title": _OptionPolicy.KEEP_VALUE,
    "--copy-colors": _OptionPolicy.KEEP_FLAG,
    "--dont-take-focus": _OptionPolicy.KEEP_FLAG,
    "--keep-focus": _OptionPolicy.KEEP_FLAG,
    "--env": _OptionPolicy.DROP_VALUE,
    "-e": _OptionPolicy.DROP_VALUE,
    "--os-panel": _OptionPolicy.DROP_VALUE,
    "--remote-control-password": _OptionPolicy.DROP_VALUE,
    "--stdin-source": _OptionPolicy.DROP_VALUE,
    "--type": _OptionPolicy.DROP_VALUE,
    "--watcher": _OptionPolicy.DROP_VALUE,
    "-w": _OptionPolicy.DROP_VALUE,
    "--allow-remote-control": _OptionPolicy.DROP_FLAG,
    "--copy-cmdline": _OptionPolicy.DROP_FLAG,
    "--copy-env": _OptionPolicy.DROP_FLAG,
    "--hold-after-ssh": _OptionPolicy.DROP_FLAG,
    "--stdin-add-formatting": _OptionPolicy.DROP_FLAG,
    "--stdin-add-line-wrap-markers": _OptionPolicy.DROP_FLAG,
}

_MANAGED_VARIABLES = {
    AGENT_VAR,
    APP_VAR,
    SESSION_ID_VAR,
    SESSION_NAME_VAR,
    SESSION_SCOPE_VAR,
    SESSION_SLUG_VAR,
    CAPTURE_VAR,
    WORKBENCH_UI_VAR,
}


def _variable_name(value: str) -> str:
    """Return the name portion of a Kitty user-variable assignment."""
    return value.split("=", 1)[0]


def _is_managed_variable(value: str) -> bool:
    """Report whether Workbench owns a Kitty user variable."""
    return _variable_name(value) in _MANAGED_VARIABLES


def _tab_title(manifest: SessionManifest) -> str:
    """Flatten a display name for Kitty's literal new_tab grammar."""
    return " ".join(manifest.name.splitlines()).strip() or manifest.slug


def _sanitize_blob(token: str) -> str:
    """Retain only the inert window ID from Kitty's private launch metadata."""
    prefix = "kitty-unserialize-data="
    if not token.startswith(prefix):
        return token
    try:
        payload: object = json.loads(token[len(prefix) :])
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict) or "id" not in payload:
        return ""
    safe_payload = {"id": payload["id"]}
    return prefix + json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))


def _ownership_arguments(manifest: SessionManifest) -> tuple[str, str, str]:
    """Return current ownership markers for a launch line."""
    return (
        f"--var={SESSION_ID_VAR}={manifest.id}",
        f"--var={SESSION_SLUG_VAR}={manifest.slug}",
        f"--var={SESSION_NAME_VAR}={session_marker_name(manifest.name, manifest.slug)}",
    )


def _parse_launch(line: str) -> list[str]:
    """Parse launch grammar and fall back to an inert default after bad quoting."""
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        return ["launch"]
    if not tokens or tokens[0] != "launch":
        raise ValueError("expected a Kitty launch line")
    return tokens


def _is_workbench_ui_launch(line: str) -> bool:
    """Identify a serialized transient manager window from its user variable."""
    tokens = _parse_launch(line)
    marker: str | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        assignment = ""
        if token.startswith("--var="):
            assignment = token.removeprefix("--var=")
            index += 1
        elif token == "--var":
            assignment = tokens[index + 1] if index + 1 < len(tokens) else ""
            index += 2
        else:
            index += 1
        name, separator, value = assignment.partition("=")
        if name == WORKBENCH_UI_VAR:
            marker = value if separator else ""
    return marker is not None and marker.casefold() not in {"", "0", "false", "no"}


def _transient_ui_locations(lines: Sequence[str]) -> _TransientUiLocations:
    """Locate transient launch lines and the tab layouts contaminated by them."""
    launch_lines: set[int] = set()
    launch_counts: dict[int, int] = {}
    transient_counts: dict[int, int] = {}
    tab_index = -1
    for line_index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("new_tab"):
            tab_index += 1
            continue
        if not stripped.startswith("launch"):
            continue
        launch_counts[tab_index] = launch_counts.get(tab_index, 0) + 1
        if _is_workbench_ui_launch(stripped):
            launch_lines.add(line_index)
            transient_counts[tab_index] = transient_counts.get(tab_index, 0) + 1
    contaminated_tabs = frozenset(transient_counts)
    return _TransientUiLocations(
        launch_lines=frozenset(launch_lines),
        contaminated_tabs=contaminated_tabs,
        transient_only_tabs=frozenset(
            index for index in contaminated_tabs if transient_counts[index] == launch_counts[index]
        ),
    )


def sanitize_launch_line(
    line: str,
    manifest: SessionManifest,
    *,
    stamp_ownership: bool = True,
) -> str:
    """Preserve layout options while removing environments and child commands."""
    tokens = _parse_launch(line)
    safe = ["launch"]
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("kitty-unserialize-data="):
            blob = _sanitize_blob(token)
            if blob:
                safe.append(blob)
            index += 1
            continue
        if token.startswith("--var="):
            if not _is_managed_variable(token.removeprefix("--var=")):
                safe.append(token)
            index += 1
            continue
        if token == "--var":
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
            if value and not _is_managed_variable(value):
                safe.extend((token, value))
            index += 2
            continue

        option_name = token.split("=", 1)[0]
        policy = _OPTION_POLICIES.get(option_name)
        has_inline_value = "=" in token
        if policy is _OptionPolicy.KEEP_VALUE:
            safe.append(token)
            if not has_inline_value and index + 1 < len(tokens):
                safe.append(tokens[index + 1])
            index += 1 if has_inline_value else 2
            continue
        if policy is _OptionPolicy.KEEP_FLAG:
            safe.append(token)
            index += 1
            continue
        if policy is _OptionPolicy.DROP_VALUE:
            index += 1 if has_inline_value else 2
            continue
        if policy is _OptionPolicy.DROP_FLAG or token.startswith("-"):
            index += 1
            continue
        break

    if stamp_ownership:
        safe.extend(_ownership_arguments(manifest))
    return shlex.join(safe)


def _is_os_window_directive(line: str) -> bool:
    """Identify top-level window grammar that must not be replayed."""
    return line in {"new_os_window", "focus_os_window"} or line.startswith("os_window_")


def sanitize_session(
    text: str,
    manifest: SessionManifest,
    *,
    stamp_ownership: bool = True,
) -> str:
    """Normalize generated or legacy grammar into a safe multi-tab snapshot."""
    lines = text.splitlines()
    transient = _transient_ui_locations(lines)
    output: list[str] = []
    saw_tab = False
    saw_launch = False
    tab_index = -1
    for line_index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("new_tab"):
            tab_index += 1
            if tab_index in transient.transient_only_tabs:
                continue
            saw_tab = True
            output.append(raw_line)
            continue
        if tab_index in transient.transient_only_tabs or _is_os_window_directive(stripped):
            continue
        if line_index in transient.launch_lines:
            continue
        if stripped.startswith("set_layout_state") and tab_index in transient.contaminated_tabs:
            continue
        if stripped.startswith("launch"):
            if not saw_tab:
                output.append(f"new_tab {_tab_title(manifest)}")
                saw_tab = True
            output.append(
                sanitize_launch_line(
                    stripped,
                    manifest,
                    stamp_ownership=stamp_ownership,
                )
            )
            saw_launch = True
            continue
        output.append(raw_line)
    if not saw_tab:
        output.insert(0, f"new_tab {_tab_title(manifest)}")
    if not saw_launch:
        output.append(
            sanitize_launch_line(
                "launch",
                manifest,
                stamp_ownership=stamp_ownership,
            )
        )
    while output and not output[0].strip():
        output.pop(0)
    while output and not output[-1].strip():
        output.pop()
    return "\n".join(output) + "\n"


def _launch_working_directories(line: str) -> list[str]:
    """Extract explicit working-directory options from a launch directive."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        return []
    directories: list[str] = []
    for index, token in enumerate(tokens):
        if token.startswith("--cwd="):
            directories.append(token.split("=", 1)[1])
        elif token == "--cwd" and index + 1 < len(tokens):
            directories.append(tokens[index + 1])
    return directories


def _cd_working_directory(line: str) -> str:
    """Extract Kitty's literal path from a tab-level cd directive."""
    cwd = line[3:].strip()
    try:
        parsed = shlex.split(cwd)
    except ValueError:
        return cwd
    return parsed[0] if len(parsed) == 1 else cwd


def snapshot_summary(text: str) -> SnapshotSummary:
    """Summarize tabs, panes, titles, and working directories from safe grammar."""
    tab_titles: list[str] = []
    working_directories: list[str] = []
    pane_count = 0
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("new_tab"):
            title = stripped.removeprefix("new_tab").strip()
            tab_titles.append(title or "untitled")
        elif stripped.startswith("launch"):
            pane_count += 1
            working_directories.extend(_launch_working_directories(stripped))
        elif stripped.startswith("cd "):
            working_directories.append(_cd_working_directory(stripped))
    unique_cwds = list(dict.fromkeys(path for path in working_directories if path))
    return SnapshotSummary(
        tab_count=max(1, len(tab_titles)),
        pane_count=pane_count,
        tab_titles=tab_titles or ["untitled"],
        working_directories=unique_cwds,
    )


def read_session(path: Path) -> str:
    """Read a UTF-8 Kitty session file."""
    return path.read_text(encoding="utf-8")
