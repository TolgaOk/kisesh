"""Atomic, locked persistence for active, archived, and removed sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .domain import SessionContext
from .filesystem import atomic_write_text
from .model import SessionManifest, SessionStatus, SnapshotSummary, slugify, utc_now


class StoreError(RuntimeError):
    """Base error for invalid or inaccessible Workbench storage."""


class SessionNotFound(StoreError):
    """Raised when no active or archived session matches an identifier."""


class SessionConflict(StoreError):
    """Raised when a lifecycle operation would overwrite another session."""


@dataclass(slots=True, frozen=True)
class StoredSession:
    """A validated manifest paired with its current storage directory."""

    manifest: SessionManifest
    directory: Path

    @property
    def snapshot_path(self) -> Path:
        """Return the current safe Kitty session snapshot path."""
        return self.directory / self.manifest.snapshot_file

    @property
    def context_path(self) -> Path:
        """Return the current command and terminal context path."""
        return self.directory / "context.json"

    @property
    def snapshot_history_dir(self) -> Path:
        """Return the bounded history directory for layout snapshots."""
        return self.directory / "history"

    @property
    def context_history_dir(self) -> Path:
        """Return the bounded history directory for terminal contexts."""
        return self.directory / "context-history"


ContextTransform = Callable[[SessionContext | None], SessionContext]


class SessionStore:
    """Manage session persistence through atomic writes and process locks."""

    def __init__(self, root: Path, history_limit: int = 20) -> None:
        """Initialize lifecycle directories below a data root."""
        self.root = root
        self.sessions_dir = root / "sessions"
        self.archived_dir = root / "archived"
        self.trash_dir = root / "trash"
        self.lock_path = root / ".lock"
        self.history_limit = max(1, history_limit)

    def ensure(self) -> None:
        """Create every top-level storage directory idempotently."""
        for path in (self.root, self.sessions_dir, self.archived_dir, self.trash_dir):
            path.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize a complete persistence transaction across processes."""
        self.ensure()
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_manifest(self, directory: Path, manifest: SessionManifest) -> None:
        """Validate and atomically persist one session manifest."""
        manifest.validate()
        encoded = (
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        atomic_write_text(directory / "manifest.json", encoded)

    @staticmethod
    def _load_manifest(directory: Path) -> SessionManifest:
        """Load one manifest and translate parsing failures to store errors."""
        try:
            raw: object = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError(f"cannot read manifest in {directory}: {error}") from error
        if not isinstance(raw, dict):
            raise StoreError(f"manifest in {directory} is not an object")
        return SessionManifest.from_dict(raw)

    def _container_for_status(self, status: SessionStatus) -> Path:
        """Return the active or archived container for a lifecycle status."""
        return self.archived_dir if status == "archived" else self.sessions_dir

    def _all_directories(self) -> Iterator[Path]:
        """Yield directories containing manifests across visible lifecycles."""
        self.ensure()
        for container in (self.sessions_dir, self.archived_dir):
            for directory in sorted(container.iterdir()):
                if directory.is_dir() and (directory / "manifest.json").is_file():
                    yield directory

    def list(self, include_archived: bool = True) -> list[StoredSession]:
        """Return known sessions in descending last-used order."""
        sessions = [
            StoredSession(self._load_manifest(directory), directory)
            for directory in self._all_directories()
        ]
        visible = (
            sessions
            if include_archived
            else [stored for stored in sessions if stored.manifest.status != "archived"]
        )
        return sorted(visible, key=lambda item: item.manifest.last_used_at, reverse=True)

    def get(self, slug_or_id: str) -> StoredSession:
        """Resolve a session by stable UUID or current slug."""
        match = next(
            (
                stored
                for stored in self.list(include_archived=True)
                if stored.manifest.slug == slug_or_id or stored.manifest.id == slug_or_id
            ),
            None,
        )
        if match is None:
            raise SessionNotFound(f"unknown session: {slug_or_id}")
        return match

    def slug_available(self, slug: str, excluding_id: str | None = None) -> bool:
        """Report whether a slug is unused outside an optional current session."""
        return all(
            stored.manifest.slug != slug or stored.manifest.id == excluding_id
            for stored in self.list(include_archived=True)
        )

    def unique_slug(self, name: str) -> str:
        """Generate the first available slug for a display name."""
        base = slugify(name)
        candidate = base
        suffix = 2
        while not self.slug_available(candidate):
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def create(
        self,
        name: str,
        project_root: str,
        *,
        session_id: str | None = None,
        now: str | None = None,
    ) -> StoredSession:
        """Create an empty active session with a unique slug and stable UUID."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("session name cannot be empty")
        with self.locked():
            slug = self.unique_slug(clean_name)
            timestamp = now or utc_now()
            manifest = SessionManifest(
                name=clean_name,
                slug=slug,
                project_root=project_root,
                id=session_id or str(uuid.uuid4()),
                created_at=timestamp,
                updated_at=timestamp,
                last_used_at=timestamp,
            )
            manifest.validate()
            directory = self.sessions_dir / slug
            if directory.exists():
                raise SessionConflict(f"session directory already exists: {directory}")
            directory.mkdir(parents=True)
            StoredSession(manifest, directory).snapshot_history_dir.mkdir()
            self._write_manifest(directory, manifest)
            return StoredSession(manifest, directory)

    def write_snapshot(
        self,
        slug_or_id: str,
        content: str,
        summary: SnapshotSummary,
        *,
        now: str | None = None,
    ) -> StoredSession:
        """Persist a safe layout snapshot and retain changed prior revisions."""
        with self.locked():
            stored = self.get(slug_or_id)
            timestamp = now or utc_now()
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if stored.manifest.snapshot_sha256 == digest and stored.snapshot_path.is_file():
                stored.manifest.updated_at = timestamp
                stored.manifest.summary = summary
                self._write_manifest(stored.directory, stored.manifest)
                return stored

            stored.snapshot_history_dir.mkdir(exist_ok=True)
            if stored.snapshot_path.is_file():
                history_name = _history_name(timestamp, stored.manifest.revision)
                shutil.copy2(stored.snapshot_path, stored.snapshot_history_dir / history_name)
            atomic_write_text(stored.snapshot_path, content)
            stored.manifest.snapshot_sha256 = digest
            stored.manifest.summary = summary
            stored.manifest.revision += 1
            stored.manifest.updated_at = timestamp
            self._write_manifest(stored.directory, stored.manifest)
            self._trim_history(stored.snapshot_history_dir, "*.kitty-session")
            return stored

    def _trim_history(self, directory: Path, pattern: str) -> None:
        """Keep only the configured number of newest revision files."""
        entries = sorted(directory.glob(pattern), reverse=True)
        for stale in entries[self.history_limit :]:
            stale.unlink(missing_ok=True)

    def write_context(
        self,
        slug_or_id: str,
        context: SessionContext,
        *,
        now: str | None = None,
    ) -> StoredSession:
        """Persist terminal context and retain each changed prior revision."""
        with self.locked():
            stored = self.get(slug_or_id)
            return self._write_context(stored, context, now)

    def update_context(
        self,
        slug_or_id: str,
        transform: ContextTransform,
        *,
        now: str | None = None,
    ) -> StoredSession:
        """Read, transform, and persist context under one process lock."""
        with self.locked():
            stored = self.get(slug_or_id)
            context = transform(self._read_context(stored))
            return self._write_context(stored, context, now)

    def _write_context(
        self,
        stored: StoredSession,
        context: SessionContext,
        now: str | None,
    ) -> StoredSession:
        """Write one context while the caller holds the storage lock."""
        encoded = json.dumps(context, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        previous = (
            stored.context_path.read_text(encoding="utf-8")
            if stored.context_path.is_file()
            else None
        )
        if previous == encoded:
            return stored
        if previous is not None:
            stored.context_history_dir.mkdir(exist_ok=True)
            destination = _unique_history_path(
                stored.context_history_dir,
                _filename_timestamp(now or utc_now()),
                ".json",
            )
            atomic_write_text(destination, previous)
        atomic_write_text(stored.context_path, encoded)
        if stored.context_history_dir.is_dir():
            self._trim_history(stored.context_history_dir, "*.json")
        return stored

    def read_context(self, slug_or_id: str) -> SessionContext | None:
        """Load the current terminal context or return none when absent."""
        return self._read_context(self.get(slug_or_id))

    @staticmethod
    def _read_context(stored: StoredSession) -> SessionContext | None:
        """Decode context for a previously resolved stored session."""
        if not stored.context_path.is_file():
            return None
        try:
            raw: object = json.loads(stored.context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError(f"cannot read context in {stored.directory}: {error}") from error
        if not isinstance(raw, dict):
            raise StoreError(f"context in {stored.directory} is not an object")
        return cast(SessionContext, raw)

    def mark_used(self, slug_or_id: str, *, now: str | None = None) -> StoredSession:
        """Update the session ordering timestamp after focus or restoration."""
        with self.locked():
            stored = self.get(slug_or_id)
            stored.manifest.last_used_at = now or utc_now()
            self._write_manifest(stored.directory, stored.manifest)
            return stored

    def rename(self, slug_or_id: str, new_name: str, *, now: str | None = None) -> StoredSession:
        """Rename a session while preserving its UUID and lifecycle container."""
        clean_name = new_name.strip()
        if not clean_name:
            raise ValueError("session name cannot be empty")
        with self.locked():
            stored = self.get(slug_or_id)
            new_slug = slugify(clean_name)
            if not self.slug_available(new_slug, excluding_id=stored.manifest.id):
                raise SessionConflict(f"session already exists: {new_slug}")
            destination = self._container_for_status(stored.manifest.status) / new_slug
            if destination != stored.directory and destination.exists():
                raise SessionConflict(f"session directory already exists: {destination}")
            stored.manifest.name = clean_name
            stored.manifest.slug = new_slug
            stored.manifest.updated_at = now or utc_now()
            stored.manifest.validate()
            if destination != stored.directory:
                stored.directory.rename(destination)
            self._write_manifest(destination, stored.manifest)
            return StoredSession(stored.manifest, destination)

    def archive(self, slug_or_id: str, *, now: str | None = None) -> StoredSession:
        """Move an active session into the archived lifecycle container."""
        with self.locked():
            stored = self.get(slug_or_id)
            if stored.manifest.status == "archived":
                return stored
            destination = self.archived_dir / stored.manifest.slug
            if destination.exists():
                raise SessionConflict(f"archive destination exists: {destination}")
            stored.directory.rename(destination)
            stored.manifest.status = "archived"
            stored.manifest.archived_at = now or utc_now()
            stored.manifest.updated_at = stored.manifest.archived_at
            self._write_manifest(destination, stored.manifest)
            return StoredSession(stored.manifest, destination)

    def restore_archive(self, slug_or_id: str, *, now: str | None = None) -> StoredSession:
        """Return an archived session to the active lifecycle container."""
        with self.locked():
            stored = self.get(slug_or_id)
            if stored.manifest.status != "archived":
                return stored
            destination = self.sessions_dir / stored.manifest.slug
            if destination.exists():
                raise SessionConflict(f"session destination exists: {destination}")
            stored.directory.rename(destination)
            stored.manifest.status = "active"
            stored.manifest.archived_at = None
            stored.manifest.updated_at = now or utc_now()
            self._write_manifest(destination, stored.manifest)
            return StoredSession(stored.manifest, destination)

    def move_to_trash(self, slug_or_id: str, *, now: str | None = None) -> Path:
        """Move a complete session directory into timestamped recoverable trash."""
        with self.locked():
            stored = self.get(slug_or_id)
            prefix = _filename_timestamp(now or utc_now())
            base_name = f"{prefix}-{stored.manifest.slug}"
            destination = _unique_history_path(self.trash_dir, base_name, "")
            stored.directory.rename(destination)
            return destination


def _unique_history_path(directory: Path, base_name: str, suffix: str) -> Path:
    """Return a collision-free revision or trash path inside a directory."""
    destination = directory / f"{base_name}{suffix}"
    counter = 2
    while destination.exists():
        destination = directory / f"{base_name}-{counter}{suffix}"
        counter += 1
    return destination


def _filename_timestamp(value: str) -> str:
    """Convert persisted UTC timestamps to lexically sortable filenames."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        parsed = datetime.now(UTC)
    return parsed.strftime("%Y%m%dT%H%M%S.%fZ")


def _history_name(timestamp: str, revision: int) -> str:
    """Build the stable filename for a numbered layout revision."""
    return f"{_filename_timestamp(timestamp)}-r{revision:04d}.kitty-session"
