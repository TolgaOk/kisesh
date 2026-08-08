"""Install agent-native session hooks and decode their session identities."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, TextIO, assert_never, cast

from .app_profiles import ResumeAdapter
from .filesystem import atomic_write_text

INVALID_SESSION_START_MESSAGE = "agent SessionStart input is incomplete"


class AgentHookState(StrEnum):
    """Describe whether one native agent integration is safely installed."""

    CONFIGURED = "configured"
    NOT_CONFIGURED = "not configured"
    CONFLICT = "conflicting unmanaged file"


@dataclass(frozen=True, slots=True)
class JsonAgentHook:
    """Describe a command hook merged into an agent-owned JSON file."""

    adapter: Literal["claude", "codex"]
    product: str
    path: Path
    command: str
    status_suffix: str = ""


@dataclass(frozen=True, slots=True)
class PiExtensionHook:
    """Describe the dedicated Pi extension used to report session starts."""

    adapter: Literal["pi"]
    product: str
    path: Path
    source: str
    status_suffix: str = ""


AgentHookSpec = JsonAgentHook | PiExtensionHook


@dataclass(frozen=True, slots=True)
class AgentSessionStart:
    """Pair a validated external session identity with its originating pane."""

    adapter: ResumeAdapter
    external_session_id: str
    window_id: int


@dataclass(frozen=True, slots=True)
class _MissingFileSnapshot:
    """Record that a hook-owned path did not exist before a transaction."""

    path: Path
    backup_existed: bool


@dataclass(frozen=True, slots=True)
class _ExistingFileSnapshot:
    """Retain the complete recoverable state of an existing hook-owned file."""

    path: Path
    content: str
    mode: int
    backup_existed: bool


_FileSnapshot = _MissingFileSnapshot | _ExistingFileSnapshot


def _pi_extension_source() -> str:
    """Render the minimal Pi lifecycle bridge installed into its extension directory."""
    return """import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

export default function kiseshSessionHook(pi: ExtensionAPI): void {
  pi.on("session_start", async (_event, context) => {
    if (!process.env.KITTY_WINDOW_ID || !context.sessionManager.getSessionFile()) return;

    const result = await pi.exec(
      "kisesh",
      ["agent-hook", "pi", "--session-id", context.sessionManager.getSessionId()],
      { timeout: 5000 },
    );
    if (result.code !== 0) {
      context.ui.notify("KiSesh could not record this Pi session.", "warning");
    }
  });
}
"""


def user_agent_hooks(environment: Mapping[str, str]) -> tuple[AgentHookSpec, ...]:
    """Resolve every supported user-level agent integration from an absolute home."""
    home_value = environment.get("HOME")
    if not home_value:
        raise ValueError("HOME is unavailable")
    home = Path(home_value)
    if not home.is_absolute():
        raise ValueError("HOME must be an absolute path")
    return (
        JsonAgentHook(
            "claude",
            "Claude",
            home / ".claude" / "settings.json",
            "kisesh agent-hook claude",
        ),
        JsonAgentHook(
            "codex",
            "Codex",
            home / ".codex" / "hooks.json",
            "kisesh agent-hook codex",
            " (review with /hooks)",
        ),
        PiExtensionHook(
            "pi",
            "Pi",
            home / ".pi" / "agent" / "extensions" / "kisesh.ts",
            _pi_extension_source(),
        ),
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
    """Preserve one pre-KiSesh copy before mutating existing JSON configuration."""
    if not path.exists():
        return
    backup = path.with_name(f"{path.name}.kisesh.bak")
    if not backup.exists():
        shutil.copy2(path, backup)


def _snapshot_file(path: Path) -> _FileSnapshot:
    """Capture one file and whether its one-time backup predates this action."""
    backup_existed = path.with_name(f"{path.name}.kisesh.bak").exists()
    if not path.exists():
        return _MissingFileSnapshot(path, backup_existed)
    return _ExistingFileSnapshot(
        path,
        path.read_text(encoding="utf-8"),
        path.stat().st_mode & 0o777,
        backup_existed,
    )


def _snapshot_hook(hook: AgentHookSpec) -> _FileSnapshot:
    """Capture the resolved file that one typed hook operation may mutate."""
    match hook:
        case JsonAgentHook(path=path):
            return _snapshot_file(_editable_json_path(path))
        case PiExtensionHook(path=path):
            return _snapshot_file(path)
        case _ as unknown:
            assert_never(unknown)


def _restore_file(snapshot: _FileSnapshot) -> None:
    """Restore a file and remove only a transaction-created backup."""
    match snapshot:
        case _MissingFileSnapshot(path=path):
            path.unlink(missing_ok=True)
        case _ExistingFileSnapshot(path=path, content=content, mode=mode) if (
            not path.exists() or path.read_text(encoding="utf-8") != content
        ):
            atomic_write_text(
                path,
                content,
                mode=mode,
                prefix=f".{path.name}.rollback.",
            )
        case _:
            pass
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
    match payload.get("hooks"), create:
        case None, False:
            return None
        case None, True:
            hooks: dict[str, object] = {}
            payload["hooks"] = hooks
        case dict() as hook_table, _:
            hooks = cast(dict[str, object], hook_table)
        case _:
            raise ValueError(f"{product} settings hooks must be an object")

    match hooks.get("SessionStart"), create:
        case None, False:
            return None
        case None, True:
            groups: list[object] = []
            hooks["SessionStart"] = groups
            return groups
        case list() as groups, _:
            return groups
        case _:
            raise ValueError(f"{product} SessionStart hooks must be a list")


def _group_handlers(group: object, product: str) -> list[object]:
    """Return one matcher group's validated hook-handler list."""
    if not isinstance(group, dict) or not isinstance((handlers := group.get("hooks")), list):
        raise ValueError(f"{product} SessionStart hook groups must contain a hooks list")
    return handlers


def _is_handler(value: object, command: str) -> bool:
    """Identify only one exact KiSesh command-hook handler."""
    return (
        isinstance(value, Mapping)
        and value.get("type") == "command"
        and value.get("command") == command
    )


def _has_handler(groups: Sequence[object], hook: JsonAgentHook) -> bool:
    """Search validated groups for one exact typed JSON hook."""
    return any(
        _is_handler(handler, hook.command)
        for group in groups
        for handler in _group_handlers(group, hook.product)
    )


def _json_hook_state(hook: JsonAgentHook) -> AgentHookState:
    """Inspect one JSON agent configuration for its exact KiSesh handler."""
    path = _editable_json_path(hook.path)
    groups = _session_groups(_read_json_object(path), create=False, product=hook.product)
    if groups is None or not _has_handler(groups, hook):
        return AgentHookState.NOT_CONFIGURED
    return AgentHookState.CONFIGURED


def _enable_json_hook(hook: JsonAgentHook) -> bool:
    """Merge one command handler without replacing neighboring agent settings."""
    path = _editable_json_path(hook.path)
    payload = _read_json_object(path)
    groups = _session_groups(payload, create=True, product=hook.product)
    assert groups is not None
    if _has_handler(groups, hook):
        return False
    groups.append({"hooks": [{"type": "command", "command": hook.command}]})
    _backup_json_once(path)
    _write_json_object(path, payload)
    return True


def _disable_json_hook(hook: JsonAgentHook) -> bool:
    """Remove only one exact command handler from an agent-owned JSON file."""
    path = _editable_json_path(hook.path)
    if not path.exists():
        return False
    payload = _read_json_object(path)
    groups = _session_groups(payload, create=False, product=hook.product)
    if groups is None:
        return False
    retained_groups: list[object] = []
    changed = False
    for group in groups:
        handlers = _group_handlers(group, hook.product)
        retained_handlers = [
            handler for handler in handlers if not _is_handler(handler, hook.command)
        ]
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
    _backup_json_once(path)
    _write_json_object(path, payload)
    return True


def _pi_hook_state(hook: PiExtensionHook) -> AgentHookState:
    """Classify the dedicated Pi extension without trusting foreign content."""
    if not hook.path.exists() and not hook.path.is_symlink():
        return AgentHookState.NOT_CONFIGURED
    if hook.path.is_symlink():
        return AgentHookState.CONFLICT
    try:
        content = hook.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read Pi extension {hook.path}: {error}") from error
    return AgentHookState.CONFIGURED if content == hook.source else AgentHookState.CONFLICT


def agent_hook_state(hook: AgentHookSpec) -> AgentHookState:
    """Inspect one hook according to its product-owned configuration type."""
    match hook:
        case JsonAgentHook():
            return _json_hook_state(hook)
        case PiExtensionHook():
            return _pi_hook_state(hook)
        case _ as unknown:
            assert_never(unknown)


def enable_agent_hook(hook: AgentHookSpec) -> bool:
    """Install one typed hook and report whether persistent state changed."""
    match hook:
        case JsonAgentHook():
            return _enable_json_hook(hook)
        case PiExtensionHook():
            match _pi_hook_state(hook):
                case AgentHookState.CONFIGURED:
                    return False
                case AgentHookState.NOT_CONFIGURED:
                    atomic_write_text(
                        hook.path,
                        hook.source,
                        mode=0o600,
                        prefix=".kisesh-pi.",
                    )
                    return True
                case AgentHookState.CONFLICT:
                    raise ValueError(f"Pi extension is not managed by KiSesh: {hook.path}")
                case _ as unknown:
                    assert_never(unknown)
        case _ as unknown:
            assert_never(unknown)


def disable_agent_hook(hook: AgentHookSpec) -> bool:
    """Remove one exact typed hook without deleting foreign configuration."""
    match hook:
        case JsonAgentHook():
            return _disable_json_hook(hook)
        case PiExtensionHook():
            match _pi_hook_state(hook):
                case AgentHookState.CONFIGURED:
                    hook.path.unlink()
                    return True
                case AgentHookState.NOT_CONFIGURED:
                    return False
                case AgentHookState.CONFLICT:
                    raise ValueError(f"Pi extension is not managed by KiSesh: {hook.path}")
                case _ as unknown:
                    assert_never(unknown)
        case _ as unknown:
            assert_never(unknown)


def configure_user_agent_hooks(hooks: Sequence[AgentHookSpec], *, enabled: bool) -> None:
    """Enable or disable every supplied agent hook as one recoverable transaction."""
    snapshots = tuple(_snapshot_hook(hook) for hook in hooks)
    operation = enable_agent_hook if enabled else disable_agent_hook
    try:
        for hook in hooks:
            operation(hook)
    except Exception:
        try:
            for snapshot in reversed(snapshots):
                _restore_file(snapshot)
        except OSError as rollback_error:
            raise OSError("cannot roll back agent hook configuration") from rollback_error
        raise


def agent_session_start(
    adapter: ResumeAdapter,
    external_session_id: object,
    environment: Mapping[str, str],
) -> AgentSessionStart:
    """Validate one external agent identity and its inherited Kitty pane ID."""
    match adapter:
        case "claude" | "codex" | "pi":
            pass
        case _ as unknown:
            assert_never(unknown)
    window_value = environment.get("KITTY_WINDOW_ID")
    try:
        window_id = int(window_value) if window_value is not None else 0
    except ValueError as error:
        raise ValueError(INVALID_SESSION_START_MESSAGE) from error
    if not isinstance(external_session_id, str) or not external_session_id or window_id <= 0:
        raise ValueError(INVALID_SESSION_START_MESSAGE)
    return AgentSessionStart(adapter, external_session_id, window_id)


def read_session_start(
    adapter: ResumeAdapter,
    stream: TextIO,
    environment: Mapping[str, str],
) -> AgentSessionStart:
    """Decode a native JSON SessionStart event and its inherited Kitty pane ID."""
    try:
        payload: object = json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(INVALID_SESSION_START_MESSAGE) from error
    match payload:
        case {"hook_event_name": "SessionStart", "session_id": str() as session_id}:
            return agent_session_start(adapter, session_id, environment)
        case _:
            raise ValueError(INVALID_SESSION_START_MESSAGE)
