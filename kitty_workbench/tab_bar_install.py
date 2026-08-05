"""Install and restore Kitty's fixed custom tab-bar entrypoint safely."""

from __future__ import annotations

import filecmp
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .filesystem import atomic_write_text, temporary_path

STATE_VERSION = 1
BackupKind = Literal["absent", "file", "symlink"]


class TabBarInstallError(RuntimeError):
    """A tab-bar integration conflict that must not be overwritten."""


@dataclass(frozen=True, slots=True)
class TabBarPaths:
    """Resolved live, source, and recovery paths for the custom tab bar."""

    live: Path
    source: Path
    state: Path
    backup: Path


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """Original custom tab-bar kind and optional symlink target."""

    kind: BackupKind
    target: str | None = None

    def to_json(self) -> str:
        """Serialize the recovery record in a stable human-readable form."""
        return (
            json.dumps(
                {"version": STATE_VERSION, "kind": self.kind, "target": self.target},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


def tab_bar_paths(kitty_config: Path, install_root: Path, data_root: Path) -> TabBarPaths:
    """Resolve the entrypoint and recovery files from installer-owned roots."""
    recovery = data_root / ".integration"
    return TabBarPaths(
        live=kitty_config.parent / "tab_bar.py",
        source=install_root / "integration" / "tab_bar.py",
        state=recovery / "tab-bar.json",
        backup=recovery / "tab_bar.py.before-workbench",
    )


def _is_managed(paths: TabBarPaths) -> bool:
    """Report whether the live entrypoint is exactly Workbench's source link."""
    if not paths.live.is_symlink():
        return False
    try:
        return paths.live.resolve(strict=False) == paths.source.resolve(strict=False)
    except OSError:
        return False


def _load_record(paths: TabBarPaths) -> BackupRecord | None:
    """Load and validate an optional recovery record without guessing defaults."""
    if not paths.state.exists():
        return None
    try:
        payload: object = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TabBarInstallError(f"cannot read tab-bar recovery state: {paths.state}") from error
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise TabBarInstallError(f"invalid tab-bar recovery state: {paths.state}")
    kind = payload.get("kind")
    target = payload.get("target")
    if kind not in {"absent", "file", "symlink"}:
        raise TabBarInstallError(f"invalid tab-bar recovery state: {paths.state}")
    if kind == "symlink" and not isinstance(target, str):
        raise TabBarInstallError(f"invalid tab-bar recovery state: {paths.state}")
    if kind != "symlink" and target is not None:
        raise TabBarInstallError(f"invalid tab-bar recovery state: {paths.state}")
    return BackupRecord(cast(BackupKind, kind), target)


def _record_original(paths: TabBarPaths) -> BackupRecord:
    """Persist the original entrypoint before changing the live config tree."""
    if paths.state.exists():
        raise TabBarInstallError(f"tab-bar recovery state already exists: {paths.state}")
    paths.state.parent.mkdir(parents=True, exist_ok=True)
    if paths.live.is_symlink():
        record = BackupRecord("symlink", os.readlink(paths.live))
    elif paths.live.exists():
        if not paths.live.is_file():
            raise TabBarInstallError(f"refusing to replace non-file tab bar: {paths.live}")
        try:
            shutil.copy2(paths.live, paths.backup)
        except OSError as error:
            raise TabBarInstallError(f"cannot back up custom tab bar: {paths.live}") from error
        record = BackupRecord("file")
    else:
        record = BackupRecord("absent")
    try:
        atomic_write_text(paths.state, record.to_json(), mode=0o600)
    except OSError:
        paths.backup.unlink(missing_ok=True)
        raise
    return record


def _matches_original(paths: TabBarPaths, record: BackupRecord) -> bool:
    """Recognize a completed restore whose cleanup was interrupted."""
    if record.kind == "absent":
        return not paths.live.exists() and not paths.live.is_symlink()
    if record.kind == "symlink":
        return paths.live.is_symlink() and os.readlink(paths.live) == record.target
    return (
        paths.live.is_file()
        and not paths.live.is_symlink()
        and paths.backup.is_file()
        and filecmp.cmp(paths.live, paths.backup, shallow=False)
    )


def _cleanup_recovery(paths: TabBarPaths) -> None:
    """Remove only Workbench's consumed recovery files."""
    paths.state.unlink(missing_ok=True)
    paths.backup.unlink(missing_ok=True)


def restore_tab_bar(paths: TabBarPaths) -> bool:
    """Restore the exact previous custom bar and reject foreign live changes."""
    record = _load_record(paths)
    if record is None:
        if _is_managed(paths):
            raise TabBarInstallError(
                f"refusing to remove managed tab bar without recovery state: {paths.live}"
            )
        return False
    if _matches_original(paths, record):
        _cleanup_recovery(paths)
        return True
    if (paths.live.exists() or paths.live.is_symlink()) and not _is_managed(paths):
        raise TabBarInstallError(f"refusing to overwrite modified custom tab bar: {paths.live}")
    if record.kind == "file" and not paths.backup.is_file():
        raise TabBarInstallError(f"custom tab-bar backup is missing: {paths.backup}")
    if paths.live.is_symlink() or paths.live.exists():
        paths.live.unlink()
    paths.live.parent.mkdir(parents=True, exist_ok=True)
    if record.kind == "file":
        with temporary_path(
            paths.live.parent,
            prefix=".kitty-workbench-tab-bar.",
            suffix=".py",
        ) as temporary:
            shutil.copy2(paths.backup, temporary)
            os.replace(temporary, paths.live)
    elif record.kind == "symlink":
        assert record.target is not None
        paths.live.symlink_to(record.target)
    _cleanup_recovery(paths)
    return True


def install_tab_bar(paths: TabBarPaths) -> bool:
    """Replace Kitty's fixed custom entrypoint with a recoverable source link."""
    if not paths.source.is_file():
        raise TabBarInstallError(f"custom tab-bar source is missing: {paths.source}")
    record = _load_record(paths)
    if _is_managed(paths):
        if record is None:
            _record_original(paths)
        return False
    if record is not None:
        restore_tab_bar(paths)
    _record_original(paths)
    try:
        if paths.live.is_symlink() or paths.live.exists():
            paths.live.unlink()
        paths.live.parent.mkdir(parents=True, exist_ok=True)
        paths.live.symlink_to(paths.source)
    except OSError:
        restore_tab_bar(paths)
        raise
    return True
