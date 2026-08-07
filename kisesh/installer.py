"""Reversible, single-command installation for KiSesh."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro

from .app_profiles import AppProfileError, parse_app_profiles
from .filesystem import atomic_write_text, temporary_path
from .legacy import (
    INTEGRATION_FILE as LEGACY_INTEGRATION_FILE,
)
from .legacy import (
    INTEGRATION_INCLUDE as LEGACY_INTEGRATION_INCLUDE,
)
from .legacy import (
    MANAGED_BEGIN as LEGACY_MANAGED_BEGIN,
)
from .legacy import (
    MANAGED_END as LEGACY_MANAGED_END,
)
from .legacy import (
    PRODUCT_DIRECTORY as LEGACY_PRODUCT_DIRECTORY,
)
from .legacy import (
    TAB_BAR_BACKUP as LEGACY_TAB_BAR_BACKUP,
)
from .runtime_install import (
    RuntimeInstallError,
    RuntimePaths,
    check_runtime_target,
    deploy_runtime,
    ensure_command_link,
    finish_runtime,
    remove_command_link,
    remove_runtime,
    rollback_runtime,
    runtime_paths,
    validate_runtime_source,
)
from .tab_bar_install import (
    TabBarInstallError,
    TabBarPaths,
    install_tab_bar,
    restore_tab_bar,
    tab_bar_paths,
)

MANAGED_BEGIN = "# BEGIN kisesh (managed by kisesh install)"
MANAGED_END = "# END kisesh (managed by kisesh install)"
COMPAT_MANAGED_BEGIN = "# BEGIN kisesh (managed by ./install)"
COMPAT_MANAGED_END = "# END kisesh (managed by ./install)"
INTEGRATION_INCLUDE = "include ~/.local/lib/kisesh/integration/kisesh.conf"
DEFAULT_LISTEN_ON = "unix:/tmp/kisesh-main"
MANAGED_KEYS = ("alt+s", "alt+shift+s", "cmd+w")

InstallAction = Literal["enable", "disable", "uninstall", "purge"]


class InstallError(RuntimeError):
    """An installation problem that should be reported without a traceback."""


@dataclass(frozen=True, slots=True)
class InstallPaths:
    """Resolved source, target, configuration, and session-data paths."""

    home: Path
    source: Path
    launcher: Path
    target: Path
    kitty_config: Path
    app_config: Path
    data: Path


@dataclass(frozen=True, slots=True)
class LegacyInstallPaths:
    """Locations owned by the product identity that preceded KiSesh."""

    target: Path
    app_config: Path
    data: Path


@dataclass(frozen=True, slots=True)
class AppConfigPlan:
    """Validated app-profile content and its optional previous source."""

    content: str | None
    legacy_source: Path | None = None


@dataclass(frozen=True, slots=True)
class LegacyUpgradeState:
    """Reversible filesystem changes made while enabling a renamed checkout."""

    data_moved: bool = False
    restored_tab_bar: TabBarPaths | None = None


@dataclass(frozen=True, slots=True)
class ConfigProbe:
    """Relevant values returned after Kitty parses a candidate config."""

    bad_lines: tuple[str, ...]
    allow_remote_control: str
    listen_on: str


@dataclass(frozen=True, slots=True)
class InstallArguments:
    """Typed installer flags with a validated mutually exclusive action."""

    enable: bool = False
    """Install and enable KiSesh, which is also the default action."""

    disable: bool = False
    """Remove Kitty integration while retaining code and sessions."""

    uninstall: bool = False
    """Disable integration and remove the code link while retaining sessions."""

    purge: bool = False
    """Uninstall and permanently delete KiSesh session data."""

    kitty_config: Path | None = None
    """Override the automatically detected kitty.conf path."""

    def action(self) -> InstallAction:
        """Resolve one selected action and reject ambiguous destructive flags."""
        options: tuple[tuple[InstallAction, bool], ...] = (
            ("enable", self.enable),
            ("disable", self.disable),
            ("uninstall", self.uninstall),
            ("purge", self.purge),
        )
        selected: list[InstallAction] = [action for action, enabled in options if enabled]
        if len(selected) > 1:
            raise InstallError("choose only one of --enable, --disable, --uninstall, or --purge")
        return selected[0] if selected else "enable"


def _expand_home(value: str | os.PathLike[str], home: Path) -> Path:
    """Expand a leading tilde against an explicit, testable home directory."""
    text = os.fspath(value)
    if text == "~":
        return home
    if text.startswith("~/"):
        return home / text[2:]
    return Path(text)


def _home() -> Path:
    """Resolve HOME or fail before considering any filesystem mutation."""
    configured = os.environ.get("HOME")
    if not configured:
        raise InstallError("HOME is unavailable")
    return Path(configured).expanduser().resolve()


def _kitty_config(home: Path, override: Path | None = None) -> Path:
    """Resolve kitty.conf through explicit, environment, XDG, then platform paths."""
    if override is not None:
        return _expand_home(override, home)
    if configured := os.environ.get("KISESH_KITTY_CONFIG"):
        return _expand_home(configured, home)
    if configured := os.environ.get("KITTY_CONFIG_DIRECTORY"):
        return _expand_home(configured, home) / "kitty.conf"
    if configured := os.environ.get("XDG_CONFIG_HOME"):
        return _expand_home(configured, home) / "kitty" / "kitty.conf"
    conventional = home / ".config" / "kitty" / "kitty.conf"
    macos = home / "Library" / "Preferences" / "kitty" / "kitty.conf"
    if not conventional.exists() and macos.exists():
        return macos
    return conventional


def _cli_launcher(source: Path) -> Path:
    """Resolve the persistent console command used by Kitty launch actions."""
    configured = os.environ.get("KISESH_CLI")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    invoked = Path(sys.argv[0]).expanduser()
    if invoked.name == "kisesh":
        invoked_path = invoked if invoked.is_absolute() else Path(shutil.which("kisesh") or invoked)
        candidates.append(invoked_path)
    candidates.extend(
        (
            Path(sys.executable).with_name("kisesh"),
            source / ".venv" / "bin" / "kisesh",
            source / "bin" / "kisesh",
        )
    )
    for candidate in candidates:
        absolute = candidate.absolute()
        if absolute.is_file() and os.access(absolute, os.X_OK):
            return absolute
    raise InstallError("the kisesh CLI launcher was not found")


def install_paths(*, kitty_config: Path | None = None) -> InstallPaths:
    """Resolve every path used by an install, disable, uninstall, or purge."""
    home = _home()
    source = Path(__file__).resolve().parents[1]
    config_base = _expand_home(os.environ.get("XDG_CONFIG_HOME", "~/.config"), home)
    data_base = _expand_home(os.environ.get("XDG_DATA_HOME", "~/.local/share"), home)
    return InstallPaths(
        home=home,
        source=source,
        launcher=_cli_launcher(source),
        target=home / ".local" / "lib" / "kisesh",
        kitty_config=_kitty_config(home, kitty_config),
        app_config=config_base / "kisesh" / "apps.toml",
        data=data_base / "kisesh",
    )


def _legacy_paths(paths: InstallPaths) -> LegacyInstallPaths:
    """Resolve previous product locations beside their KiSesh replacements."""
    return LegacyInstallPaths(
        target=paths.target.with_name(LEGACY_PRODUCT_DIRECTORY),
        app_config=paths.app_config.parent.with_name(LEGACY_PRODUCT_DIRECTORY) / "apps.toml",
        data=paths.data.with_name(LEGACY_PRODUCT_DIRECTORY),
    )


def _legacy_tab_bar_paths(config: Path, legacy: LegacyInstallPaths) -> TabBarPaths:
    """Resolve previous tab-bar recovery files without changing their names."""
    recovery = legacy.data / ".integration"
    return TabBarPaths(
        live=config.parent / "tab_bar.py",
        source=legacy.target / "integration" / "tab_bar.py",
        state=recovery / "tab-bar.json",
        backup=recovery / LEGACY_TAB_BAR_BACKUP,
    )


def _validate_source(paths: InstallPaths) -> None:
    """Require all packaged runtime and application-profile resources."""
    try:
        validate_runtime_source(_runtime_paths(paths))
    except RuntimeInstallError as error:
        raise InstallError(str(error)) from error
    profile = paths.source / "kisesh" / "default_apps.toml"
    if not profile.is_file():
        raise InstallError(f"package is incomplete; missing: {profile}")


def _same_target(link: Path, source: Path) -> bool:
    """Compare a symlink and source while treating resolution failures as unequal."""
    try:
        return link.resolve(strict=False) == source.resolve(strict=True)
    except OSError:
        return False


def _runtime_paths(paths: InstallPaths) -> RuntimePaths:
    """Translate installer locations into one runtime deployment contract."""
    return runtime_paths(paths.source, paths.launcher, paths.target)


def _check_install_target(paths: InstallPaths, *, removing: bool = False) -> None:
    """Reject foreign runtime targets before an install or removal transaction."""
    del removing
    try:
        check_runtime_target(_runtime_paths(paths))
    except RuntimeInstallError as error:
        raise InstallError(str(error)) from error


def _strip_kisesh_config(text: str, paths: InstallPaths) -> tuple[str, bool]:
    """Remove current or previous managed blocks and manual includes."""
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    active_end: str | None = None
    changed = False
    legacy = _legacy_paths(paths)
    absolute_include = f"include {paths.target / 'integration' / 'kisesh.conf'}"
    legacy_absolute_include = f"include {legacy.target / 'integration' / LEGACY_INTEGRATION_FILE}"
    manual_includes = {
        INTEGRATION_INCLUDE,
        absolute_include,
        LEGACY_INTEGRATION_INCLUDE,
        legacy_absolute_include,
    }
    managed_blocks = {
        MANAGED_BEGIN: MANAGED_END,
        COMPAT_MANAGED_BEGIN: COMPAT_MANAGED_END,
        LEGACY_MANAGED_BEGIN: LEGACY_MANAGED_END,
    }
    managed_ends = frozenset(managed_blocks.values())
    for line in lines:
        marker = line.rstrip("\r\n")
        if marker in managed_blocks:
            if active_end is not None:
                raise InstallError("kitty.conf contains a nested KiSesh managed block")
            active_end = managed_blocks[marker]
            changed = True
            continue
        if marker in managed_ends:
            if marker != active_end:
                raise InstallError("kitty.conf contains an unmatched KiSesh end marker")
            active_end = None
            continue
        if active_end is not None:
            continue
        if line.strip() in manual_includes:
            changed = True
            continue
        output.append(line)
    if active_end is not None:
        raise InstallError("kitty.conf contains an unterminated KiSesh managed block")
    stripped = "".join(output).rstrip()
    return (f"{stripped}\n" if stripped else ""), changed


def _managed_block(*, add_remote_control: bool, add_socket: bool) -> str:
    """Render the smallest complete managed Kitty configuration block."""
    lines = [MANAGED_BEGIN]
    if add_remote_control:
        lines.append("allow_remote_control socket-only")
    if add_socket:
        lines.append(f"listen_on {DEFAULT_LISTEN_ON}")
    lines.extend((INTEGRATION_INCLUDE, MANAGED_END))
    return "\n".join(lines) + "\n"


def _enabled_config(base: str, block: str) -> str:
    """Append a managed block with stable surrounding whitespace."""
    prefix = base.rstrip()
    return f"{prefix}\n\n{block}" if prefix else block


def _editable_config(path: Path) -> Path:
    """Resolve a Kitty config symlink so atomic replacement preserves the link."""
    if path.is_symlink():
        try:
            return path.resolve(strict=True)
        except OSError as error:
            raise InstallError(f"cannot resolve Kitty config symlink {path}: {error}") from error
    return path


def _read_config(path: Path) -> str:
    """Read existing configuration or return an empty config for a new file."""
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        raise InstallError(f"cannot read Kitty config {path}: {error}") from error


def _atomic_write(path: Path, content: str) -> None:
    """Replace configuration atomically while retaining its current mode."""
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o600
    atomic_write_text(path, content, mode=mode, prefix=".kisesh-config.")


def _app_config_plan(paths: InstallPaths, legacy: LegacyInstallPaths) -> AppConfigPlan:
    """Validate current, previous, or bundled profiles in precedence order."""
    bundled = paths.source / "kisesh" / "default_apps.toml"
    try:
        bundled_content = bundled.read_text(encoding="utf-8")
        parse_app_profiles(bundled_content, source=str(bundled))
        if paths.app_config.exists() or paths.app_config.is_symlink():
            if not paths.app_config.is_file():
                raise InstallError(f"app config is not a file: {paths.app_config}")
            existing = paths.app_config.read_text(encoding="utf-8")
            parse_app_profiles(existing, source=str(paths.app_config))
            return AppConfigPlan(None)
        if legacy.app_config.exists() or legacy.app_config.is_symlink():
            if not legacy.app_config.is_file():
                raise InstallError(f"previous app config is not a file: {legacy.app_config}")
            previous = legacy.app_config.read_text(encoding="utf-8")
            parse_app_profiles(previous, source=str(legacy.app_config))
            return AppConfigPlan(previous, legacy.app_config)
    except (AppProfileError, OSError) as error:
        raise InstallError(f"cannot use app config: {error}") from error
    return AppConfigPlan(bundled_content)


def _prepare_legacy_upgrade(
    paths: InstallPaths,
    legacy: LegacyInstallPaths,
    config: Path,
) -> LegacyUpgradeState:
    """Restore previous integration state and move session data as one step."""
    legacy_data_exists = legacy.data.exists() or legacy.data.is_symlink()
    current_data_exists = paths.data.exists() or paths.data.is_symlink()
    if legacy_data_exists and current_data_exists:
        raise InstallError(
            "both KiSesh and previous session-data directories exist; move one aside and retry"
        )
    legacy_bar = _legacy_tab_bar_paths(config, legacy)
    restored_bar = restore_tab_bar(legacy_bar)
    try:
        if legacy_data_exists:
            paths.data.parent.mkdir(parents=True, exist_ok=True)
            legacy.data.rename(paths.data)
    except OSError:
        if restored_bar:
            install_tab_bar(legacy_bar)
        raise
    return LegacyUpgradeState(
        data_moved=legacy_data_exists,
        restored_tab_bar=legacy_bar if restored_bar else None,
    )


def _rollback_legacy_upgrade(
    paths: InstallPaths,
    legacy: LegacyInstallPaths,
    state: LegacyUpgradeState,
) -> None:
    """Return previous data and tab-bar state after a failed KiSesh enable."""
    if state.data_moved:
        paths.data.rename(legacy.data)
    if state.restored_tab_bar is not None:
        install_tab_bar(state.restored_tab_bar)


def _remove_legacy_link(paths: InstallPaths, legacy: LegacyInstallPaths) -> bool:
    """Remove only a previous code symlink that resolves to this checkout."""
    if not legacy.target.is_symlink():
        return False
    raw_target = Path(os.readlink(legacy.target))
    linked_path = raw_target if raw_target.is_absolute() else legacy.target.parent / raw_target
    previous_source = paths.source.with_name(LEGACY_PRODUCT_DIRECTORY)
    if linked_path != previous_source and not _same_target(legacy.target, paths.source):
        return False
    legacy.target.unlink()
    return True


def _write_app_config(path: Path, content: str) -> None:
    """Install a private first-use app config without replacing user choices."""
    atomic_write_text(path, content, mode=0o600, prefix=".kisesh-apps.")


def _backup_once(path: Path) -> Path | None:
    """Create one metadata-preserving config backup without overwriting it."""
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.kisesh.bak")
    if not backup.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as error:
            raise InstallError(f"cannot back up Kitty config to {backup}: {error}") from error
    return backup


def _find_executable(environment_name: str, command: str, app_path: str) -> str:
    """Resolve an executable through explicit config, PATH, then a platform path."""
    if configured := os.environ.get(environment_name):
        candidate = Path(configured)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise InstallError(f"{environment_name} is not executable: {configured}")
    if found := shutil.which(command):
        return found
    candidate = Path(app_path)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    raise InstallError(f"{command} was not found")


_CONFIG_PROBE = (
    "import json,sys; "
    "from kitty.config import load_config; "
    "bad=[]; o=load_config(sys.argv[1],accumulate_bad_lines=bad); "
    "print(json.dumps({'bad':[str(x) for x in bad],"
    "'allow':str(getattr(o,'allow_remote_control','') or ''),"
    "'listen':str(getattr(o,'listen_on','') or '')}))"
)


def _probe_config(kitty: str, config_path: Path, content: str) -> ConfigProbe:
    """Ask Kitty itself to parse a temporary candidate configuration."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with temporary_path(
        config_path.parent,
        prefix=".kisesh-validate.",
        suffix=".conf",
    ) as temporary:
        temporary.write_text(content, encoding="utf-8")
        try:
            result = subprocess.run(
                [kitty, "+runpy", _CONFIG_PROBE, str(temporary)],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallError(f"cannot validate Kitty config: {error}") from error
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise InstallError(
                f"Kitty could not validate its config: {detail or result.returncode}"
            )
        payload_line = next(
            (line for line in reversed(result.stdout.splitlines()) if line.strip()), ""
        )
        try:
            payload = json.loads(payload_line)
        except (json.JSONDecodeError, TypeError) as error:
            raise InstallError("Kitty returned an unreadable config-validation result") from error
        bad = payload.get("bad")
        if not isinstance(bad, list):
            raise InstallError("Kitty returned an invalid config-validation result")
        return ConfigProbe(
            tuple(str(item) for item in bad),
            str(payload.get("allow") or ""),
            str(payload.get("listen") or ""),
        )


def _format_bad_config(label: str, probe: ConfigProbe) -> InstallError:
    """Format at most ten Kitty parser errors as one safe installer failure."""
    details = "\n".join(f"  - {line}" for line in probe.bad_lines[:10])
    return InstallError(
        f"{label} contains Kitty configuration errors; no changes were made:\n{details}"
    )


def _remote_control_disabled(value: str) -> bool:
    """Interpret Kitty's disabled remote-control spellings."""
    return value.strip().casefold() in {"", "no", "none", "false", "0"}


def _socket_missing(value: str) -> bool:
    """Interpret Kitty's absent listen-socket spellings."""
    return value.strip().casefold() in {"", "no", "none", "false", "0"}


def _mapping_conflicts(base: str) -> tuple[str, ...]:
    """Find existing key mappings shadowed by the KiSesh include."""
    keys: set[str] = set()
    managed = set(MANAGED_KEYS)
    for line in base.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) >= 2 and fields[0] == "map" and fields[1] in managed:
            keys.add(fields[1])
    return tuple(sorted(keys))


def _validated_enabled_config(kitty: str, config: Path, base: str) -> str:
    """Build and validate the complete enabled config before any durable edit."""
    base_probe = _probe_config(kitty, config, base)
    if base_probe.bad_lines:
        raise _format_bad_config("Existing kitty.conf", base_probe)
    block = _managed_block(
        add_remote_control=_remote_control_disabled(base_probe.allow_remote_control),
        add_socket=_socket_missing(base_probe.listen_on),
    )
    desired = _enabled_config(base, block)
    final_probe = _probe_config(kitty, config, desired)
    if final_probe.bad_lines:
        raise _format_bad_config("KiSesh-enabled kitty.conf", final_probe)
    if _remote_control_disabled(final_probe.allow_remote_control):
        raise InstallError("Kitty remote control is still disabled; no changes were made")
    if _socket_missing(final_probe.listen_on):
        raise InstallError("Kitty has no persistent listen_on socket; no changes were made")
    return desired


def _finish_legacy_upgrade(
    paths: InstallPaths,
    legacy: LegacyInstallPaths,
    app_config_plan: AppConfigPlan,
    app_config_created: bool,
) -> bool:
    """Retire verified previous paths after the KiSesh transaction succeeds."""
    if app_config_created and app_config_plan.legacy_source is not None:
        try:
            app_config_plan.legacy_source.unlink()
        except OSError as error:
            print(f"warning: previous app config remains: {error}", file=sys.stderr)
        else:
            with suppress(OSError):
                app_config_plan.legacy_source.parent.rmdir()
    try:
        return _remove_legacy_link(paths, legacy)
    except OSError as error:
        print(f"warning: previous code link remains: {error}", file=sys.stderr)
        return False


def _report_enabled(
    paths: InstallPaths,
    legacy: LegacyInstallPaths,
    app_config_plan: AppConfigPlan,
    app_config_created: bool,
    legacy_state: LegacyUpgradeState,
    legacy_link_removed: bool,
    bar_paths: TabBarPaths,
    base: str,
) -> None:
    """Report the installed resources, preserved state, and mapping conflicts."""
    print(f"runtime: {paths.target}")
    print(f"command: {paths.home / '.local' / 'bin' / 'kisesh'}")
    if legacy_link_removed:
        print(f"removed previous code link: {legacy.target}")
    state = (
        "upgraded"
        if app_config_plan.legacy_source is not None
        else "created"
        if app_config_created
        else "preserved"
    )
    print(f"apps:    {paths.app_config} ({state})")
    if legacy_state.data_moved:
        print(f"sessions: {paths.data} (upgraded)")
    print(f"tab bar: {bar_paths.live} -> {bar_paths.source}")
    if conflicts := _mapping_conflicts(base):
        print(
            "warning: KiSesh takes precedence over existing mappings for " + ", ".join(conflicts),
            file=sys.stderr,
        )
    print(
        "Kitty was left running; reload its configuration when convenient, "
        "then press Alt+S and n to create your first session"
    )


def _enable(paths: InstallPaths) -> None:
    """Validate, deploy, configure, and report an enabled KiSesh package."""
    _validate_source(paths)
    _check_install_target(paths)
    kitty = _find_executable(
        "KISESH_KITTY", "kitty", "/Applications/kitty.app/Contents/MacOS/kitty"
    )
    config = _editable_config(paths.kitty_config)
    legacy = _legacy_paths(paths)
    original = _read_config(config)
    base, _ = _strip_kisesh_config(original, paths)
    app_config_plan = _app_config_plan(paths, legacy)
    runtime = _runtime_paths(paths)
    transaction = deploy_runtime(runtime)
    command_link = paths.home / ".local" / "bin" / "kisesh"
    command_link_created = False
    bar_paths = tab_bar_paths(config, paths.target, paths.data)
    tab_bar_changed = False
    app_config_created = False
    legacy_state = LegacyUpgradeState()
    app_config_parent_existed = paths.app_config.parent.exists()
    try:
        command_link_created = ensure_command_link(command_link, paths.launcher)
        desired = _validated_enabled_config(kitty, config, base)
        legacy_state = _prepare_legacy_upgrade(paths, legacy, config)
        tab_bar_changed = install_tab_bar(bar_paths)
        if app_config_plan.content is not None:
            _write_app_config(paths.app_config, app_config_plan.content)
            app_config_created = True
        if desired != original:
            backup = _backup_once(config)
            _atomic_write(config, desired)
            print(f"enabled: {config}")
            if backup is not None:
                print(f"backup:  {backup}")
        else:
            print(f"already enabled: {config}")
    except Exception:
        if app_config_created:
            paths.app_config.unlink(missing_ok=True)
            if not app_config_parent_existed:
                with suppress(OSError):
                    paths.app_config.parent.rmdir()
        if tab_bar_changed:
            restore_tab_bar(bar_paths)
        _rollback_legacy_upgrade(paths, legacy, legacy_state)
        if command_link_created:
            remove_command_link(command_link, paths.launcher)
        rollback_runtime(transaction)
        raise

    try:
        finish_runtime(transaction)
    except (OSError, RuntimeInstallError) as error:
        print(f"warning: previous runtime remains: {error}", file=sys.stderr)

    legacy_link_removed = _finish_legacy_upgrade(
        paths,
        legacy,
        app_config_plan,
        app_config_created,
    )
    _report_enabled(
        paths,
        legacy,
        app_config_plan,
        app_config_created,
        legacy_state,
        legacy_link_removed,
        bar_paths,
        base,
    )


def _disable(paths: InstallPaths) -> bool:
    """Remove only KiSesh configuration while preserving code and sessions."""
    config = _editable_config(paths.kitty_config)
    original = _read_config(config)
    desired, changed = _strip_kisesh_config(original, paths)
    bar_paths = tab_bar_paths(config, paths.target, paths.data)
    bar_restored = restore_tab_bar(bar_paths)
    try:
        if changed:
            backup = _backup_once(config)
            _atomic_write(config, desired)
            print(f"disabled: {config}")
            if backup is not None:
                print(f"backup:   {backup}")
        else:
            print(f"already disabled: {config}")
    except Exception:
        if bar_restored:
            install_tab_bar(bar_paths)
        raise
    if bar_restored:
        print(f"restored custom tab bar: {bar_paths.live}")
    return changed or bar_restored


def _remove_product_data(path: Path, base: Path) -> bool:
    """Delete only a verified KiSesh data directory during explicit purge."""
    expected = base / "kisesh"
    if path != expected or path.name != "kisesh":
        raise InstallError(f"refusing unsafe purge path: {path}")
    if path.is_symlink():
        path.unlink()
        return True
    if path.exists():
        shutil.rmtree(path)
        return True
    return False


def _uninstall(paths: InstallPaths, *, purge: bool) -> None:
    """Disable integration, remove managed launch paths, and optionally purge data."""
    _check_install_target(paths, removing=True)
    _disable(paths)
    command_link = paths.home / ".local" / "bin" / "kisesh"
    if remove_command_link(command_link, paths.launcher):
        print(f"removed command link: {command_link}")
    if remove_runtime(_runtime_paths(paths)):
        print(f"removed runtime: {paths.target}")
    else:
        print(f"runtime already absent: {paths.target}")
    if purge:
        data_base = paths.data.parent
        removed = [paths.data] if _remove_product_data(paths.data, data_base) else []
        for path in removed:
            print(f"purged: {path}")
        if not removed:
            print("session data already absent")
    else:
        print(f"sessions preserved: {paths.data}")
        print("use kisesh uninstall --purge only when you intentionally want to delete them")
    print("Kitty was left running; reload its configuration when convenient")


def parse_arguments(argv: Sequence[str] | None = None) -> InstallArguments:
    """Parse installer flags with Tyro into a fully typed configuration."""
    return tyro.cli(
        InstallArguments,
        prog="kisesh install",
        description="Install, disable, or remove KiSesh safely.",
        args=list(argv) if argv is not None else None,
        config=(tyro.conf.HelptextFromCommentsOff,),
    )


def run(arguments: InstallArguments) -> int:
    """Execute one typed installer action and translate expected failures."""
    try:
        action = arguments.action()
        paths = install_paths(kitty_config=arguments.kitty_config)
        if action == "enable":
            _enable(paths)
        elif action == "disable":
            _disable(paths)
            print("Kitty was left running; reload its configuration when convenient")
        else:
            _uninstall(paths, purge=action == "purge")
    except (InstallError, RuntimeInstallError, TabBarInstallError) as error:
        print(f"kisesh installer: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"kisesh installer: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse installer arguments and execute the selected reversible action."""
    return run(parse_arguments(argv))


if __name__ == "__main__":
    raise SystemExit(main())
