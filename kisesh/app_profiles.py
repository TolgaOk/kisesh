"""Load validated application display and restoration profiles from TOML."""

from __future__ import annotations

import os
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, cast

APP_PROFILE_SCHEMA_VERSION = 1
BUNDLED_APP_CONFIG = Path(__file__).with_name("default_apps.toml")
RestoreMode = Literal["resume", "captured", "configured", "prefill", "ignore"]
ResumeAdapter = Literal["claude", "codex"]
DefaultRestoreMode = Literal["prefill", "ignore"]
ProfileSignature = tuple[Path, int, int]

_RESTORE_MODES = frozenset(("resume", "captured", "configured", "prefill", "ignore"))
_RESUME_ADAPTERS = frozenset(("claude", "codex"))
_DEFAULT_RESTORE_MODES = frozenset(("prefill", "ignore"))
_DEFAULT_FIELDS = frozenset(("restore", "label", "icon"))
_APP_FIELDS = frozenset(("match", "restore", "adapter", "argv", "label", "icon", "agent"))


class AppProfileError(ValueError):
    """Report a malformed or unreadable application-profile configuration."""


@dataclass(frozen=True, slots=True)
class DefaultAppProfile:
    """Safe presentation and restore behavior for unmatched applications."""

    restore: DefaultRestoreMode
    label: str
    icon: str


@dataclass(frozen=True, slots=True)
class AppProfile:
    """One matched application's display metadata and restore strategy."""

    name: str
    match: tuple[str, ...]
    restore: RestoreMode
    label: str
    icon: str
    agent: bool = False
    adapter: ResumeAdapter | None = None
    argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AppProfiles:
    """Ordered application rules paired with one safe unmatched default."""

    defaults: DefaultAppProfile
    apps: tuple[AppProfile, ...]

    def match(self, command: str | None) -> AppProfile | None:
        """Return the first profile matching an executable basename."""
        if not command:
            return None
        executable = Path(command).name.lstrip("-").casefold()
        return next(
            (
                profile
                for profile in self.apps
                if any(fnmatchcase(executable, pattern.casefold()) for pattern in profile.match)
            ),
            None,
        )

    def named(self, name: str | None) -> AppProfile | None:
        """Return a profile by its stable TOML table name."""
        normalized = str(name or "").casefold()
        return next((profile for profile in self.apps if profile.name == normalized), None)


def app_config_path(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit, environment, or standard XDG application config path."""
    if override is not None:
        return Path(override).expanduser()
    if configured := os.environ.get("KISESH_APP_CONFIG"):
        return Path(configured).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "kisesh" / "apps.toml"


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
    """Validate one application restore-mode enum value."""
    if not isinstance(value, str) or value not in _RESTORE_MODES:
        raise AppProfileError(f"{location} must be one of {', '.join(sorted(_RESTORE_MODES))}")
    return cast(RestoreMode, value)


def _default_profile(raw: object) -> DefaultAppProfile:
    """Parse the safe behavior used for applications without a matching table."""
    table = _table(raw, "defaults")
    _known_fields(table, _DEFAULT_FIELDS, "defaults")
    restore = table.get("restore")
    if not isinstance(restore, str) or restore not in _DEFAULT_RESTORE_MODES:
        raise AppProfileError("defaults.restore must be prefill or ignore")
    return DefaultAppProfile(
        cast(DefaultRestoreMode, restore),
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


def _app_profile(name: str, raw: object, defaults: DefaultAppProfile) -> AppProfile:
    """Parse one named profile and enforce its mode-specific fields."""
    if (
        not name
        or name != name.casefold()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name)
    ):
        raise AppProfileError(f"invalid app profile name: {name!r}")
    table = _table(raw, f"apps.{name}")
    _known_fields(table, _APP_FIELDS, f"apps.{name}")
    restore = _restore_mode(table.get("restore"), f"apps.{name}.restore")
    adapter_value = table.get("adapter")
    if adapter_value is not None and (
        not isinstance(adapter_value, str) or adapter_value not in _RESUME_ADAPTERS
    ):
        raise AppProfileError(f"apps.{name}.adapter must be claude or codex")
    adapter = cast(ResumeAdapter | None, adapter_value)
    argv = (
        _string_sequence(table["argv"], f"apps.{name}.argv", maximum_items=64, maximum_length=2048)
        if "argv" in table
        else ()
    )
    if restore == "resume" and adapter is None:
        raise AppProfileError(f"apps.{name}.adapter is required for resume")
    if restore != "resume" and adapter is not None:
        raise AppProfileError(f"apps.{name}.adapter is only valid with resume")
    if restore == "configured" and not argv:
        raise AppProfileError(f"apps.{name}.argv is required for configured restore")
    if restore != "configured" and argv:
        raise AppProfileError(f"apps.{name}.argv is only valid with configured restore")
    agent = table.get("agent", False)
    if not isinstance(agent, bool):
        raise AppProfileError(f"apps.{name}.agent must be true or false")
    return AppProfile(
        name,
        _match_patterns(table.get("match"), f"apps.{name}.match"),
        restore,
        _display_text(table.get("label", name), f"apps.{name}.label", maximum=64),
        _display_text(table.get("icon", defaults.icon), f"apps.{name}.icon", maximum=8),
        agent,
        adapter,
        argv,
    )


def parse_app_profiles(text: str, *, source: str = "apps.toml") -> AppProfiles:
    """Parse and validate one complete application-profile TOML document."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AppProfileError(f"{source}: invalid TOML: {error}") from error
    version = document.get("version")
    if isinstance(version, bool) or version != APP_PROFILE_SCHEMA_VERSION:
        raise AppProfileError(f"{source}: version must be {APP_PROFILE_SCHEMA_VERSION}")
    _known_fields(document, frozenset(("version", "defaults", "apps")), source)
    defaults = _default_profile(document.get("defaults"))
    raw_apps = _table(document.get("apps"), "apps")
    apps = tuple(_app_profile(name, raw, defaults) for name, raw in raw_apps.items())
    patterns: set[str] = set()
    for profile in apps:
        for pattern in profile.match:
            normalized = pattern.casefold()
            if normalized in patterns:
                raise AppProfileError(f"duplicate app match pattern: {pattern}")
            patterns.add(normalized)
    return AppProfiles(defaults, apps)


def load_app_profiles(path: str | os.PathLike[str] | None = None) -> AppProfiles:
    """Load an explicit profile file or fall back to bundled defaults when absent."""
    configured = app_config_path(path)
    explicit = path is not None or bool(os.environ.get("KISESH_APP_CONFIG"))
    if configured.exists() and not configured.is_file():
        raise AppProfileError(f"app config is not a file: {configured}")
    source = configured if configured.is_file() else BUNDLED_APP_CONFIG
    if explicit and not configured.is_file():
        raise AppProfileError(f"app config does not exist: {configured}")
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise AppProfileError(f"cannot read app config {source}: {error}") from error
    return parse_app_profiles(text, source=str(source))


DEFAULT_APP_PROFILES = load_app_profiles(BUNDLED_APP_CONFIG)
_current_profiles: AppProfiles | None = None
_current_signature: ProfileSignature | None = None


def _profile_signature() -> ProfileSignature | None:
    """Describe the active user or bundled file without reading its contents."""
    configured = app_config_path()
    try:
        source = configured if configured.is_file() else BUNDLED_APP_CONFIG
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
