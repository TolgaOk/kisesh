"""Parse native agent hook events without coupling them to CLI or storage."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from .app_profiles import ResumeAdapter
from .filesystem import atomic_write_text

INVALID_SESSION_START_MESSAGE = "agent SessionStart input is incomplete"
CLAUDE_HOOK_COMMAND = "kisesh agent-hook claude"
CODEX_HOOK_COMMAND = "kisesh agent-hook codex"


@dataclass(frozen=True, slots=True)
class AgentSessionStart:
    """Validated external session identity paired with its originating pane."""

    adapter: ResumeAdapter
    external_session_id: str
    window_id: int


@dataclass(frozen=True, slots=True)
class AgentHookPaths:
    """Documented user-level Claude and Codex hook configuration paths."""

    claude: Path
    codex: Path


@dataclass(frozen=True, slots=True)
class _JsonSnapshot:
    """Recoverable pre-transaction state for one resolved JSON configuration."""

    path: Path
    content: str | None
    mode: int | None
    backup_existed: bool


def user_agent_hook_paths(environment: Mapping[str, str]) -> AgentHookPaths:
    """Resolve agent hook files from an explicit, absolute HOME value."""
    home_value = environment.get("HOME")
    if not home_value:
        raise ValueError("HOME is unavailable")
    home = Path(home_value)
    if not home.is_absolute():
        raise ValueError("HOME must be an absolute path")
    return AgentHookPaths(
        home / ".claude" / "settings.json",
        home / ".codex" / "hooks.json",
    )


def _read_json_object(path: Path) -> dict[str, object]:
    """Read one optional JSON configuration without accepting scalar roots."""
    if not path.exists():
        return {}
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read agent hook configuration {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"agent hook configuration is not an object: {path}")
    return payload


def _editable_json_path(path: Path) -> Path:
    """Resolve a configuration symlink so atomic replacement preserves the link."""
    if not path.is_symlink():
        return path
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"cannot resolve agent hook configuration {path}: {error}") from error


def _backup_json_once(path: Path) -> None:
    """Preserve one pre-KiSesh copy before mutating an existing configuration."""
    if not path.exists():
        return
    backup = path.with_name(f"{path.name}.kisesh.bak")
    if not backup.exists():
        shutil.copy2(path, backup)


def _snapshot_json(path: Path) -> _JsonSnapshot:
    """Capture one resolved config and whether its one-time backup predates this action."""
    editable = _editable_json_path(path)
    backup_existed = editable.with_name(f"{editable.name}.kisesh.bak").exists()
    if not editable.exists():
        return _JsonSnapshot(editable, None, None, backup_existed)
    return _JsonSnapshot(
        editable,
        editable.read_text(encoding="utf-8"),
        editable.stat().st_mode & 0o777,
        backup_existed,
    )


def _restore_json(snapshot: _JsonSnapshot) -> None:
    """Restore one config and remove only a backup created by the failed action."""
    if snapshot.content is None:
        snapshot.path.unlink(missing_ok=True)
    elif (
        not snapshot.path.exists() or snapshot.path.read_text(encoding="utf-8") != snapshot.content
    ):
        atomic_write_text(
            snapshot.path,
            snapshot.content,
            mode=snapshot.mode,
            prefix=f".{snapshot.path.name}.rollback.",
        )
    backup = snapshot.path.with_name(f"{snapshot.path.name}.kisesh.bak")
    if not snapshot.backup_existed:
        backup.unlink(missing_ok=True)


def _write_json_object(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically write a private JSON configuration with stable formatting."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, content, mode=mode, prefix=f".{path.name}.kisesh.")


def _session_groups(
    payload: dict[str, object],
    *,
    create: bool,
    product: str,
) -> list[object] | None:
    """Resolve one product's SessionStart groups with structural validation."""
    hooks = payload.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = {}
        payload["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError(f"{product} settings hooks must be an object")
    groups = hooks.get("SessionStart")
    if groups is None:
        if not create:
            return None
        groups = []
        hooks["SessionStart"] = groups
    if not isinstance(groups, list):
        raise ValueError(f"{product} SessionStart hooks must be a list")
    return groups


def _group_handlers(group: object, product: str) -> list[object]:
    """Return one matcher group's validated hook-handler list."""
    if not isinstance(group, dict) or not isinstance((handlers := group.get("hooks")), list):
        raise ValueError(f"{product} SessionStart hook groups must contain a hooks list")
    return handlers


def _is_handler(value: object, command: str) -> bool:
    """Identify only KiSesh's exact command hook handler."""
    return (
        isinstance(value, Mapping)
        and value.get("type") == "command"
        and value.get("command") == command
    )


def _has_handler(groups: list[object], command: str, product: str) -> bool:
    """Search validated groups for one exact KiSesh handler."""
    return any(
        _is_handler(handler, command)
        for group in groups
        for handler in _group_handlers(group, product)
    )


def _hook_enabled(path: Path, command: str, product: str) -> bool:
    """Report whether one agent configuration contains its KiSesh handler."""
    editable = _editable_json_path(path)
    groups = _session_groups(_read_json_object(editable), create=False, product=product)
    return groups is not None and _has_handler(groups, command, product)


def _enable_hook(path: Path, command: str, product: str) -> bool:
    """Merge one native SessionStart handler and report whether it changed."""
    editable = _editable_json_path(path)
    payload = _read_json_object(editable)
    groups = _session_groups(payload, create=True, product=product)
    assert groups is not None
    if _has_handler(groups, command, product):
        return False
    groups.append({"hooks": [{"type": "command", "command": command}]})
    _backup_json_once(editable)
    _write_json_object(editable, payload)
    return True


def _disable_hook(path: Path, command: str, product: str) -> bool:
    """Remove only one exact KiSesh SessionStart handler if present."""
    editable = _editable_json_path(path)
    if not editable.exists():
        return False
    payload = _read_json_object(editable)
    groups = _session_groups(payload, create=False, product=product)
    if groups is None:
        return False
    retained_groups: list[object] = []
    changed = False
    for group in groups:
        handlers = _group_handlers(group, product)
        retained_handlers = [handler for handler in handlers if not _is_handler(handler, command)]
        changed = changed or len(retained_handlers) != len(handlers)
        if retained_handlers:
            copied = dict(cast(dict[str, object], group))
            copied["hooks"] = retained_handlers
            retained_groups.append(copied)
    if not changed:
        return False
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    if retained_groups:
        hooks["SessionStart"] = retained_groups
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        payload.pop("hooks")
    _backup_json_once(editable)
    _write_json_object(editable, payload)
    return True


def claude_hook_enabled(path: Path) -> bool:
    """Report whether Claude's user settings contain the KiSesh handler."""
    return _hook_enabled(path, CLAUDE_HOOK_COMMAND, "Claude")


def enable_claude_hook(path: Path) -> bool:
    """Merge the KiSesh handler into Claude's user settings."""
    return _enable_hook(path, CLAUDE_HOOK_COMMAND, "Claude")


def disable_claude_hook(path: Path) -> bool:
    """Remove only the KiSesh handler from Claude's user settings."""
    return _disable_hook(path, CLAUDE_HOOK_COMMAND, "Claude")


def codex_hook_enabled(path: Path) -> bool:
    """Report whether Codex's user hooks contain the KiSesh handler."""
    return _hook_enabled(path, CODEX_HOOK_COMMAND, "Codex")


def enable_codex_hook(path: Path) -> bool:
    """Merge the KiSesh handler into Codex's user hooks."""
    return _enable_hook(path, CODEX_HOOK_COMMAND, "Codex")


def disable_codex_hook(path: Path) -> bool:
    """Remove only the KiSesh handler from Codex's user hooks."""
    return _disable_hook(path, CODEX_HOOK_COMMAND, "Codex")


def configure_user_agent_hooks(paths: AgentHookPaths, *, enabled: bool) -> None:
    """Enable or disable both user hook files as one recoverable transaction."""
    snapshots = (_snapshot_json(paths.claude), _snapshot_json(paths.codex))
    try:
        if enabled:
            enable_claude_hook(paths.claude)
            enable_codex_hook(paths.codex)
        else:
            disable_claude_hook(paths.claude)
            disable_codex_hook(paths.codex)
    except Exception:
        try:
            for snapshot in reversed(snapshots):
                _restore_json(snapshot)
        except OSError as rollback_error:
            raise OSError("cannot roll back agent hook configuration") from rollback_error
        raise


def read_session_start(
    adapter: ResumeAdapter,
    stream: TextIO,
    environment: Mapping[str, str],
) -> AgentSessionStart:
    """Decode one native SessionStart event and its inherited Kitty pane ID."""
    try:
        payload: object = json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(INVALID_SESSION_START_MESSAGE) from error
    window_value = environment.get("KITTY_WINDOW_ID")
    try:
        window_id = int(window_value) if window_value is not None else 0
    except ValueError as error:
        raise ValueError(INVALID_SESSION_START_MESSAGE) from error
    if not isinstance(payload, Mapping):
        raise ValueError(INVALID_SESSION_START_MESSAGE)
    external_session_id = payload.get("session_id")
    if (
        payload.get("hook_event_name") != "SessionStart"
        or not isinstance(external_session_id, str)
        or not external_session_id
        or window_id <= 0
    ):
        raise ValueError(INVALID_SESSION_START_MESSAGE)
    return AgentSessionStart(adapter, external_session_id, window_id)
