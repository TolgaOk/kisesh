"""Validated domain models for persisted KiSesh sessions."""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

from .domain import JsonObject

SCHEMA_VERSION = 1
SESSION_ID_VAR = "kisesh_session"
SESSION_SLUG_VAR = "kisesh_slug"
SESSION_NAME_VAR = "kisesh_name"
SESSION_SCOPE_VAR = "kisesh_scope"
CAPTURE_VAR = "kisesh_capture"
AGENT_VAR = "kisesh_agent"
APP_VAR = "kisesh_app"
KISESH_UI_VAR = "kisesh_ui"
RESTORE_LAYOUT_VAR = "kisesh_restore_layout"

SessionStatus = Literal["active", "archived"]


def utc_now() -> str:
    """Return the current UTC time in the persisted KiSesh format."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def slugify(value: str) -> str:
    """Convert a display name into a stable, filesystem-safe session slug."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized.lower().strip())
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    return normalized or "session"


def session_marker_name(name: str, fallback: str) -> str:
    """Flatten a display name into one safe Kitty user-variable value."""
    visible = "".join(
        character for character in name if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(visible.split()) or fallback


def _integer(value: object, default: int = 0) -> int:
    """Coerce a JSON value to an integer or use the supplied default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, str)):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _strings(value: object) -> list[str]:
    """Coerce a JSON array to strings while rejecting scalar lookalikes."""
    return [str(item) for item in value] if isinstance(value, list) else []


@dataclass(slots=True)
class SnapshotSummary:
    """Small layout summary rendered in the session manager."""

    tab_count: int = 0
    pane_count: int = 0
    tab_titles: list[str] = field(default_factory=list)
    working_directories: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonObject:
        """Serialize the summary as a JSON-compatible object."""
        return {
            "tab_count": self.tab_count,
            "pane_count": self.pane_count,
            "tab_titles": list(self.tab_titles),
            "working_directories": list(self.working_directories),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> SnapshotSummary:
        """Parse a summary from untrusted persisted JSON values."""
        values = data or {}
        return cls(
            tab_count=_integer(values.get("tab_count")),
            pane_count=_integer(values.get("pane_count")),
            tab_titles=_strings(values.get("tab_titles")),
            working_directories=_strings(values.get("working_directories")),
        )


@dataclass(slots=True)
class SessionManifest:
    """Validated identity, lifecycle, and snapshot metadata for one session."""

    name: str
    slug: str
    project_root: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = SCHEMA_VERSION
    status: SessionStatus = "active"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_used_at: str = field(default_factory=utc_now)
    archived_at: str | None = None
    snapshot_file: str = "current.kitty-session"
    snapshot_sha256: str | None = None
    revision: int = 0
    summary: SnapshotSummary = field(default_factory=SnapshotSummary)

    def validate(self) -> None:
        """Reject unsupported, malformed, or internally inconsistent fields."""
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema: {self.schema_version}")
        if not self.name.strip():
            raise ValueError("session name cannot be empty")
        if self.slug != slugify(self.slug):
            raise ValueError(f"invalid session slug: {self.slug!r}")
        try:
            uuid.UUID(self.id)
        except ValueError as error:
            raise ValueError(f"invalid session id: {self.id!r}") from error
        if self.status not in ("active", "archived"):
            raise ValueError(f"invalid session status: {self.status!r}")

    def to_dict(self) -> JsonObject:
        """Serialize the manifest as a JSON-compatible object."""
        return {
            "name": self.name,
            "slug": self.slug,
            "project_root": self.project_root,
            "id": self.id,
            "schema_version": self.schema_version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "archived_at": self.archived_at,
            "snapshot_file": self.snapshot_file,
            "snapshot_sha256": self.snapshot_sha256,
            "revision": self.revision,
            "summary": self.summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SessionManifest:
        """Parse and validate a manifest from persisted JSON values."""
        raw_status = str(data.get("status", "active"))
        status = cast(SessionStatus, raw_status)
        raw_summary = data.get("summary")
        summary = SnapshotSummary.from_dict(
            raw_summary if isinstance(raw_summary, Mapping) else None
        )
        manifest = cls(
            name=str(data.get("name", "")),
            slug=str(data.get("slug", "")),
            project_root=str(data.get("project_root", "")),
            id=str(data.get("id", "")),
            schema_version=_integer(data.get("schema_version"), SCHEMA_VERSION),
            status=status,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            last_used_at=str(data.get("last_used_at", "")),
            archived_at=str(data["archived_at"]) if data.get("archived_at") is not None else None,
            snapshot_file=str(data.get("snapshot_file", "current.kitty-session")),
            snapshot_sha256=(
                str(data["snapshot_sha256"]) if data.get("snapshot_sha256") is not None else None
            ),
            revision=_integer(data.get("revision")),
            summary=summary,
        )
        manifest.validate()
        return manifest
