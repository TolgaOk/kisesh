"""Load typed application and agent profiles from TOML."""

from __future__ import annotations

import os
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, assert_never, cast

APP_PROFILE_SCHEMA_VERSION = 2
ResumeAdapter = Literal["claude", "codex", "pi"]
ProfileKind = Literal["app", "agent"]
RestoreMode = Literal["resume", "captured", "configured", "prefill", "ignore"]
ProfileSignature = tuple[Path, int, int]

_SUPPORTED_SCHEMA_VERSIONS = frozenset((1, APP_PROFILE_SCHEMA_VERSION))
_RESTORE_MODES = frozenset(("resume", "captured", "configured", "prefill", "ignore"))
_RESUME_ADAPTERS = frozenset(("claude", "codex", "pi"))
_DEFAULT_FIELDS = frozenset(("restore", "label", "icon"))
_PROFILE_FIELDS = frozenset(("match", "restore", "label", "icon"))
_APP_FIELDS = _PROFILE_FIELDS | frozenset(("argv",))
_AGENT_FIELDS = _PROFILE_FIELDS | frozenset(("adapter",))
_V1_APP_FIELDS = _PROFILE_FIELDS | frozenset(("adapter", "argv", "agent"))


class AppProfileError(ValueError):
    """Report a malformed or unreadable profile configuration."""


@dataclass(frozen=True, slots=True)
class ResumeRestore:
    """Resume one exact external agent session through a built-in adapter."""

    adapter: ResumeAdapter


@dataclass(frozen=True, slots=True)
class CapturedRestore:
    """Run the command arguments captured from a recognized application."""


@dataclass(frozen=True, slots=True)
class ConfiguredRestore:
    """Run one deterministic command declared by the user."""

    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrefillRestore:
    """Type captured command arguments without submitting them."""


@dataclass(frozen=True, slots=True)
class IgnoreRestore:
    """Restore no foreground application command."""


RestorePolicy = ResumeRestore | CapturedRestore | ConfiguredRestore | PrefillRestore | IgnoreRestore
DefaultRestorePolicy = PrefillRestore | IgnoreRestore


@dataclass(frozen=True, slots=True)
class DefaultAppProfile:
    """Safe presentation and restore behavior for unmatched applications."""

    restore: DefaultRestorePolicy
    label: str
    icon: str


@dataclass(frozen=True, slots=True)
class AppProfile:
    """One matched foreground program and its validated behavior."""

    name: str
    kind: ProfileKind
    match: tuple[str, ...]
    restore: RestorePolicy
    label: str
    icon: str


@dataclass(frozen=True, slots=True)
class AppProfiles:
    """Ordered program rules paired with one safe unmatched default."""

    defaults: DefaultAppProfile
    profiles: tuple[AppProfile, ...]

    def match(self, command: str | None) -> AppProfile | None:
        """Return the first profile matching an executable basename."""
        if not command:
            return None
        executable = Path(command).name.lstrip("-").casefold()
        return next(
            (
                profile
                for profile in self.profiles
                if any(fnmatchcase(executable, pattern.casefold()) for pattern in profile.match)
            ),
            None,
        )

    def named(self, name: str | None) -> AppProfile | None:
        """Return a profile by its stable TOML table name."""
        normalized = str(name or "").casefold()
        return next((profile for profile in self.profiles if profile.name == normalized), None)


@dataclass(frozen=True, slots=True)
class _SchemaV1:
    """Represent the installed version-one document without losing user choices."""

    defaults: Mapping[str, object]
    apps: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _SchemaV2:
    """Represent the semantically separated application and agent document."""

    defaults: Mapping[str, object]
    apps: Mapping[str, object]
    agents: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _ProfileIdentity:
    """Hold fields shared by every validated app and agent profile."""

    name: str
    match: tuple[str, ...]
    label: str
    icon: str


def app_config_path(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit, environment, or standard XDG profile path."""
    if override is not None:
        return Path(override).expanduser()
    if configured := os.environ.get("KISESH_APP_CONFIG"):
        return Path(configured).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "kisesh" / "apps.toml"


def bundled_app_config_path() -> Path:
    """Resolve the package-owned default profile."""
    return Path(__file__).with_name("default_apps.toml")


def _table(value: object, location: str) -> Mapping[str, object]:
    """Require one TOML table and retain its string-keyed values."""
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise AppProfileError(f"{location} must be a TOML table")
    return cast(Mapping[str, object], value)


def _known_fields(table: Mapping[str, object], allowed: frozenset[str], location: str) -> None:
    """Reject misspelled or unsupported fields before they can be ignored."""
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise AppProfileError(f"{location} has unknown field: {unknown[0]}")


def _display_text(value: object, location: str, *, maximum: int) -> str:
    """Validate bounded visible display text without terminal controls."""
    if not isinstance(value, str) or not value.strip():
        raise AppProfileError(f"{location} must be a nonempty string")
    text = value.strip()
    if len(text) > maximum or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in text
    ):
        raise AppProfileError(f"{location} contains unsupported display text")
    return text


def _string_sequence(
    value: object,
    location: str,
    *,
    maximum_items: int,
    maximum_length: int,
) -> tuple[str, ...]:
    """Validate a bounded TOML string array without empty or control values."""
    if not isinstance(value, list) or not value or len(value) > maximum_items:
        raise AppProfileError(f"{location} must be a nonempty bounded string array")
    output: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > maximum_length
            or any(character in item for character in ("\x00", "\r", "\n"))
        ):
            raise AppProfileError(f"{location} contains an invalid string")
        output.append(item)
    return tuple(output)


def _restore_mode(value: object, location: str) -> RestoreMode:
    """Validate one restore-mode enum value."""
    if not isinstance(value, str) or value not in _RESTORE_MODES:
        raise AppProfileError(f"{location} must be one of {', '.join(sorted(_RESTORE_MODES))}")
    return cast(RestoreMode, value)


def _resume_adapter(value: object, location: str) -> ResumeAdapter:
    """Validate one built-in session-resume adapter name."""
    if not isinstance(value, str) or value not in _RESUME_ADAPTERS:
        raise AppProfileError(f"{location} must be claude, codex, or pi")
    return cast(ResumeAdapter, value)


def _default_profile(raw: object) -> DefaultAppProfile:
    """Parse the safe behavior used for applications without a matching table."""
    table = _table(raw, "defaults")
    _known_fields(table, _DEFAULT_FIELDS, "defaults")
    match table.get("restore"):
        case "prefill":
            restore: DefaultRestorePolicy = PrefillRestore()
        case "ignore":
            restore = IgnoreRestore()
        case _:
            raise AppProfileError("defaults.restore must be prefill or ignore")
    return DefaultAppProfile(
        restore,
        _display_text(table.get("label"), "defaults.label", maximum=64),
        _display_text(table.get("icon"), "defaults.icon", maximum=8),
    )


def _match_patterns(value: object, location: str) -> tuple[str, ...]:
    """Validate case-insensitive executable-basename glob patterns."""
    patterns = _string_sequence(value, location, maximum_items=32, maximum_length=128)
    if any(
        pattern != pattern.strip()
        or "/" in pattern
        or "\\" in pattern
        or any(character.isspace() for character in pattern)
        for pattern in patterns
    ):
        raise AppProfileError(f"{location} must contain executable basenames only")
    return patterns


def _profile_identity(
    name: str,
    table: Mapping[str, object],
    defaults: DefaultAppProfile,
    *,
    kind: ProfileKind,
    location: str,
) -> _ProfileIdentity:
    """Parse the name, matching, and presentation shared by apps and agents."""
    if (
        not name
        or name != name.casefold()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name)
    ):
        raise AppProfileError(f"invalid {kind} profile name: {name!r}")
    return _ProfileIdentity(
        name,
        _match_patterns(table.get("match"), f"{location}.match"),
        _display_text(table.get("label", name), f"{location}.label", maximum=64),
        _display_text(table.get("icon", defaults.icon), f"{location}.icon", maximum=8),
    )


def _app_restore(table: Mapping[str, object], location: str) -> RestorePolicy:
    """Build a restore policy that cannot contain agent-only state."""
    mode = _restore_mode(table.get("restore"), f"{location}.restore")
    match mode:
        case "configured" if "argv" not in table:
            raise AppProfileError(f"{location}.argv is required for configured restore")
        case "configured":
            return ConfiguredRestore(
                _string_sequence(
                    table.get("argv"),
                    f"{location}.argv",
                    maximum_items=64,
                    maximum_length=2048,
                )
            )
        case _ if "argv" in table:
            raise AppProfileError(f"{location}.argv is only valid with configured restore")
        case "captured":
            return CapturedRestore()
        case "prefill":
            return PrefillRestore()
        case "ignore":
            return IgnoreRestore()
        case "resume":
            raise AppProfileError(f"{location}.restore cannot be resume")
        case _ as unknown:
            assert_never(unknown)


def _agent_restore(table: Mapping[str, object], location: str) -> RestorePolicy:
    """Build an agent restore policy with adapter state only for exact resume."""
    mode = _restore_mode(table.get("restore"), f"{location}.restore")
    match mode:
        case "resume" if "adapter" not in table:
            raise AppProfileError(f"{location}.adapter is required for resume")
        case "resume":
            return ResumeRestore(_resume_adapter(table.get("adapter"), f"{location}.adapter"))
        case _ if "adapter" in table:
            raise AppProfileError(f"{location}.adapter is only valid with resume")
        case "captured":
            return CapturedRestore()
        case "prefill":
            return PrefillRestore()
        case "ignore":
            return IgnoreRestore()
        case "configured":
            raise AppProfileError(f"{location}.restore cannot be configured")
        case _ as unknown:
            assert_never(unknown)


def _v1_restore(table: Mapping[str, object], location: str) -> RestorePolicy:
    """Normalize a version-one restore tuple into one typed policy."""
    mode = _restore_mode(table.get("restore"), f"{location}.restore")
    match mode:
        case "resume" if "argv" in table:
            raise AppProfileError(f"{location}.argv is only valid with configured restore")
        case "resume" if "adapter" not in table:
            raise AppProfileError(f"{location}.adapter is required for resume")
        case "resume":
            return ResumeRestore(_resume_adapter(table.get("adapter"), f"{location}.adapter"))
        case "configured" if "adapter" in table:
            raise AppProfileError(f"{location}.adapter is only valid with resume")
        case "configured" if "argv" not in table:
            raise AppProfileError(f"{location}.argv is required for configured restore")
        case "configured":
            return ConfiguredRestore(
                _string_sequence(
                    table.get("argv"),
                    f"{location}.argv",
                    maximum_items=64,
                    maximum_length=2048,
                )
            )
        case _ if "adapter" in table:
            raise AppProfileError(f"{location}.adapter is only valid with resume")
        case _ if "argv" in table:
            raise AppProfileError(f"{location}.argv is only valid with configured restore")
        case "captured":
            return CapturedRestore()
        case "prefill":
            return PrefillRestore()
        case "ignore":
            return IgnoreRestore()
        case _ as unknown:
            assert_never(unknown)


def _v1_profile(name: str, raw: object, defaults: DefaultAppProfile) -> AppProfile:
    """Normalize one version-one app table into the typed profile model."""
    location = f"apps.{name}"
    table = _table(raw, location)
    _known_fields(table, _V1_APP_FIELDS, location)
    match table.get("agent", False):
        case bool() as is_agent:
            kind: ProfileKind = "agent" if is_agent else "app"
        case _:
            raise AppProfileError(f"{location}.agent must be true or false")
    identity = _profile_identity(name, table, defaults, kind=kind, location=location)
    return AppProfile(
        identity.name,
        kind,
        identity.match,
        _v1_restore(table, location),
        identity.label,
        identity.icon,
    )


def _v2_app_profile(name: str, raw: object, defaults: DefaultAppProfile) -> AppProfile:
    """Parse one regular application table from a version-two document."""
    location = f"apps.{name}"
    table = _table(raw, location)
    _known_fields(table, _APP_FIELDS, location)
    identity = _profile_identity(name, table, defaults, kind="app", location=location)
    return AppProfile(
        identity.name,
        "app",
        identity.match,
        _app_restore(table, location),
        identity.label,
        identity.icon,
    )


def _v2_agent_profile(name: str, raw: object, defaults: DefaultAppProfile) -> AppProfile:
    """Parse one AI-agent table from a version-two document."""
    location = f"agents.{name}"
    table = _table(raw, location)
    _known_fields(table, _AGENT_FIELDS, location)
    identity = _profile_identity(name, table, defaults, kind="agent", location=location)
    return AppProfile(
        identity.name,
        "agent",
        identity.match,
        _agent_restore(table, location),
        identity.label,
        identity.icon,
    )


def _schema_document(text: str, source: str) -> _SchemaV1 | _SchemaV2:
    """Decode TOML into one version-discriminated document model."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AppProfileError(f"{source}: invalid TOML: {error}") from error
    version = document.get("version")
    match version:
        case 1 if not isinstance(version, bool):
            _known_fields(document, frozenset(("version", "defaults", "apps")), source)
            return _SchemaV1(
                _table(document.get("defaults"), "defaults"),
                _table(document.get("apps", {}), "apps"),
            )
        case 2 if not isinstance(version, bool):
            _known_fields(
                document,
                frozenset(("version", "defaults", "apps", "agents")),
                source,
            )
            return _SchemaV2(
                _table(document.get("defaults"), "defaults"),
                _table(document.get("apps", {}), "apps"),
                _table(document.get("agents", {}), "agents"),
            )
        case _:
            supported = " or ".join(str(item) for item in sorted(_SUPPORTED_SCHEMA_VERSIONS))
            raise AppProfileError(f"{source}: version must be {supported}")


def _validate_unique_profiles(profiles: tuple[AppProfile, ...]) -> None:
    """Reject names or executable patterns that would make matching ambiguous."""
    names: set[str] = set()
    patterns: set[str] = set()
    for profile in profiles:
        if profile.name in names:
            raise AppProfileError(f"duplicate profile name: {profile.name}")
        names.add(profile.name)
        for pattern in profile.match:
            normalized = pattern.casefold()
            if normalized in patterns:
                raise AppProfileError(f"duplicate match pattern: {pattern}")
            patterns.add(normalized)


def parse_app_profiles(text: str, *, source: str = "apps.toml") -> AppProfiles:
    """Parse and validate one complete versioned profile document."""
    document = _schema_document(text, source)
    defaults = _default_profile(document.defaults)
    match document:
        case _SchemaV1(apps=apps):
            profiles = tuple(_v1_profile(name, raw, defaults) for name, raw in apps.items())
        case _SchemaV2(apps=apps, agents=agents):
            profiles = (
                *(_v2_agent_profile(name, raw, defaults) for name, raw in agents.items()),
                *(_v2_app_profile(name, raw, defaults) for name, raw in apps.items()),
            )
        case _ as unknown:
            assert_never(unknown)
    _validate_unique_profiles(profiles)
    return AppProfiles(defaults, profiles)


def load_app_profiles(path: str | os.PathLike[str] | None = None) -> AppProfiles:
    """Load an explicit profile file or fall back to bundled defaults when absent."""
    configured = app_config_path(path)
    explicit = path is not None or bool(os.environ.get("KISESH_APP_CONFIG"))
    if configured.exists() and not configured.is_file():
        raise AppProfileError(f"app config is not a file: {configured}")
    source = configured if configured.is_file() else bundled_app_config_path()
    if explicit and not configured.is_file():
        raise AppProfileError(f"app config does not exist: {configured}")
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise AppProfileError(f"cannot read app config {source}: {error}") from error
    return parse_app_profiles(text, source=str(source))


DEFAULT_APP_PROFILES = load_app_profiles(bundled_app_config_path())
_current_profiles: AppProfiles | None = None
_current_signature: ProfileSignature | None = None


def _profile_signature() -> ProfileSignature | None:
    """Describe the active user or bundled file without reading its contents."""
    configured = app_config_path()
    try:
        source = configured if configured.is_file() else bundled_app_config_path()
        metadata = source.stat()
    except OSError:
        return None
    return source, metadata.st_mtime_ns, metadata.st_size


def current_app_profiles() -> AppProfiles:
    """Return process-cached user profiles without filesystem work on render paths."""
    global _current_profiles, _current_signature
    if _current_profiles is None:
        try:
            _current_profiles = load_app_profiles()
        except AppProfileError:
            _current_profiles = DEFAULT_APP_PROFILES
        _current_signature = _profile_signature()
    return _current_profiles


def refresh_app_profiles() -> AppProfiles:
    """Reload user profiles after an explicit command or watcher event."""
    global _current_profiles, _current_signature
    signature = _profile_signature()
    if _current_profiles is not None and signature == _current_signature:
        return _current_profiles
    try:
        _current_profiles = load_app_profiles()
    except AppProfileError:
        _current_profiles = DEFAULT_APP_PROFILES
    _current_signature = signature
    return _current_profiles
