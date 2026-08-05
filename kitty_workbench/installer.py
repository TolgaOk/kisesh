"""Reversible, single-command installation for kitty-workbench."""

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
from .tab_bar_install import (
    TabBarInstallError,
    install_tab_bar,
    restore_tab_bar,
    tab_bar_paths,
)

MANAGED_BEGIN = "# BEGIN kitty-workbench (managed by ./install)"
MANAGED_END = "# END kitty-workbench (managed by ./install)"
INTEGRATION_INCLUDE = "include ~/.local/lib/kitty-workbench/integration/kitty-workbench.conf"
DEFAULT_LISTEN_ON = "unix:/tmp/kitty-workbench-main"
MANAGED_KEYS = ("alt+s", "alt+shift+s", "cmd+w")

InstallAction = Literal["enable", "disable", "uninstall", "purge"]


class InstallError(RuntimeError):
    """An installation problem that should be reported without a traceback."""


@dataclass(frozen=True, slots=True)
class InstallPaths:
    """Resolved source, target, configuration, and session-data paths."""

    home: Path
    source: Path
    target: Path
    kitty_config: Path
    app_config: Path
    data: Path


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
    """Install and enable Workbench, which is also the default action."""

    disable: bool = False
    """Remove Kitty integration while retaining code and sessions."""

    uninstall: bool = False
    """Disable integration and remove the code link while retaining sessions."""

    purge: bool = False
    """Uninstall and permanently delete Workbench session data."""

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
    if configured := os.environ.get("KITTY_WORKBENCH_KITTY_CONFIG"):
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


def install_paths(*, kitty_config: Path | None = None) -> InstallPaths:
    """Resolve every path used by an install, disable, uninstall, or purge."""
    home = _home()
    source = Path(__file__).resolve().parents[1]
    config_base = _expand_home(os.environ.get("XDG_CONFIG_HOME", "~/.config"), home)
    data_base = _expand_home(os.environ.get("XDG_DATA_HOME", "~/.local/share"), home)
    return InstallPaths(
        home=home,
        source=source,
        target=home / ".local" / "lib" / "kitty-workbench",
        kitty_config=_kitty_config(home, kitty_config),
        app_config=config_base / "kitty-workbench" / "apps.toml",
        data=data_base / "kitty-workbench",
    )


def _validate_source(paths: InstallPaths) -> None:
    """Require all launchers, integration config, and watcher source files."""
    required = (
        paths.source / "bin" / "kitty-workbench",
        paths.source / "integration" / "kitty-workbench.conf",
        paths.source / "integration" / "reload_tab_bar.py",
        paths.source / "integration" / "safe_close.py",
        paths.source / "integration" / "tab_bar.py",
        paths.source / "kitty_workbench" / "close_guard.py",
        paths.source / "kitty_workbench" / "app_profiles.py",
        paths.source / "kitty_workbench" / "default_apps.toml",
        paths.source / "kitty_workbench" / "session_bar.py",
        paths.source / "kitty_workbench" / "watcher.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise InstallError(f"source checkout is incomplete; missing: {', '.join(missing)}")


def _same_target(link: Path, source: Path) -> bool:
    """Compare a symlink and source while treating resolution failures as unequal."""
    try:
        return link.resolve(strict=False) == source.resolve(strict=True)
    except OSError:
        return False


def _check_install_target(paths: InstallPaths, *, removing: bool = False) -> None:
    """Reject any install target that is not absent, in-place, or our symlink."""
    target = paths.target
    if (
        target.resolve(strict=False) == paths.source.resolve(strict=True)
        and not target.is_symlink()
    ):
        return
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink() and _same_target(target, paths.source):
        return
    action = "remove" if removing else "replace"
    raise InstallError(
        f"refusing to {action} existing install path: {target}\n"
        "Move it aside or remove it explicitly, then retry."
    )


def _ensure_install_link(paths: InstallPaths) -> bool:
    """Create the source symlink when needed and report whether it changed."""
    _check_install_target(paths)
    if paths.target.resolve(strict=False) == paths.source.resolve(strict=True):
        return False
    paths.target.parent.mkdir(parents=True, exist_ok=True)
    paths.target.symlink_to(paths.source, target_is_directory=True)
    return True


def _remove_install_link(paths: InstallPaths) -> bool:
    """Remove only a verified Workbench source symlink."""
    _check_install_target(paths, removing=True)
    if paths.target.is_symlink():
        paths.target.unlink()
        return True
    return False


def _strip_workbench_config(text: str, paths: InstallPaths) -> tuple[str, bool]:
    """Remove complete managed blocks and the old one-line manual include."""
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    inside = False
    changed = False
    absolute_include = f"include {paths.target / 'integration' / 'kitty-workbench.conf'}"
    manual_includes = {INTEGRATION_INCLUDE, absolute_include}
    for line in lines:
        marker = line.rstrip("\r\n")
        if marker == MANAGED_BEGIN:
            if inside:
                raise InstallError("kitty.conf contains a nested kitty-workbench managed block")
            inside = True
            changed = True
            continue
        if marker == MANAGED_END:
            if not inside:
                raise InstallError("kitty.conf contains an unmatched kitty-workbench end marker")
            inside = False
            continue
        if inside:
            continue
        if line.strip() in manual_includes:
            changed = True
            continue
        output.append(line)
    if inside:
        raise InstallError("kitty.conf contains an unterminated kitty-workbench managed block")
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
    atomic_write_text(path, content, mode=mode, prefix=".kitty-workbench-config.")


def _app_config_candidate(paths: InstallPaths) -> str | None:
    """Validate existing app profiles or return bundled content for first install."""
    bundled = paths.source / "kitty_workbench" / "default_apps.toml"
    try:
        content = bundled.read_text(encoding="utf-8")
        parse_app_profiles(content, source=str(bundled))
        if paths.app_config.exists():
            if not paths.app_config.is_file():
                raise InstallError(f"app config is not a file: {paths.app_config}")
            existing = paths.app_config.read_text(encoding="utf-8")
            parse_app_profiles(existing, source=str(paths.app_config))
            return None
    except (AppProfileError, OSError) as error:
        raise InstallError(f"cannot use app config: {error}") from error
    return content


def _write_app_config(path: Path, content: str) -> None:
    """Install a private first-use app config without replacing user choices."""
    atomic_write_text(path, content, mode=0o600, prefix=".kitty-workbench-apps.")


def _backup_once(path: Path) -> Path | None:
    """Create one metadata-preserving config backup without overwriting it."""
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.kitty-workbench.bak")
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
        prefix=".kitty-workbench-validate.",
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
    """Find existing key mappings shadowed by the Workbench include."""
    keys: set[str] = set()
    managed = set(MANAGED_KEYS)
    for line in base.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) >= 2 and fields[0] == "map" and fields[1] in managed:
            keys.add(fields[1])
    return tuple(sorted(keys))


def _enable(paths: InstallPaths) -> None:
    """Validate, install, configure, and report an enabled Workbench checkout."""
    _validate_source(paths)
    _check_install_target(paths)
    kitty = _find_executable(
        "KITTY_WORKBENCH_KITTY", "kitty", "/Applications/kitty.app/Contents/MacOS/kitty"
    )
    config = _editable_config(paths.kitty_config)
    original = _read_config(config)
    base, _ = _strip_workbench_config(original, paths)
    app_config_candidate = _app_config_candidate(paths)
    link_created = _ensure_install_link(paths)
    bar_paths = tab_bar_paths(config, paths.target, paths.data)
    tab_bar_changed = False
    app_config_created = False
    app_config_parent_existed = paths.app_config.parent.exists()
    try:
        base_probe = _probe_config(kitty, config, base)
        if base_probe.bad_lines:
            raise _format_bad_config("Existing kitty.conf", base_probe)
        add_remote = _remote_control_disabled(base_probe.allow_remote_control)
        add_socket = _socket_missing(base_probe.listen_on)
        block = _managed_block(add_remote_control=add_remote, add_socket=add_socket)
        desired = _enabled_config(base, block)
        final_probe = _probe_config(kitty, config, desired)
        if final_probe.bad_lines:
            raise _format_bad_config("Workbench-enabled kitty.conf", final_probe)
        if _remote_control_disabled(final_probe.allow_remote_control):
            raise InstallError("Kitty remote control is still disabled; no changes were made")
        if _socket_missing(final_probe.listen_on):
            raise InstallError("Kitty has no persistent listen_on socket; no changes were made")
        tab_bar_changed = install_tab_bar(bar_paths)
        if app_config_candidate is not None:
            _write_app_config(paths.app_config, app_config_candidate)
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
        if link_created and paths.target.is_symlink():
            paths.target.unlink()
        raise

    print(f"code:    {paths.target} -> {paths.source}")
    state = "created" if app_config_created else "preserved"
    print(f"apps:    {paths.app_config} ({state})")
    print(f"tab bar: {bar_paths.live} -> {bar_paths.source}")
    if conflicts := _mapping_conflicts(base):
        print(
            "warning: Workbench takes precedence over existing mappings for "
            + ", ".join(conflicts),
            file=sys.stderr,
        )
    print("restart Kitty once, then press Alt+S and n to create your first session")


def _disable(paths: InstallPaths) -> bool:
    """Remove only Workbench configuration while preserving code and sessions."""
    config = _editable_config(paths.kitty_config)
    original = _read_config(config)
    desired, changed = _strip_workbench_config(original, paths)
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
    """Delete only a verified kitty-workbench data directory during explicit purge."""
    expected = base / "kitty-workbench"
    if path != expected or path.name != "kitty-workbench":
        raise InstallError(f"refusing unsafe purge path: {path}")
    if path.is_symlink():
        path.unlink()
        return True
    if path.exists():
        shutil.rmtree(path)
        return True
    return False


def _uninstall(paths: InstallPaths, *, purge: bool) -> None:
    """Disable integration, remove the code link, and optionally purge data."""
    _check_install_target(paths, removing=True)
    _disable(paths)
    if _remove_install_link(paths):
        print(f"removed code link: {paths.target}")
    else:
        print(f"code link already absent: {paths.target}")
    if purge:
        data_base = paths.data.parent
        removed = [paths.data] if _remove_product_data(paths.data, data_base) else []
        for path in removed:
            print(f"purged: {path}")
        if not removed:
            print("session data already absent")
    else:
        print(f"sessions preserved: {paths.data}")
        print("use ./install --purge only when you intentionally want to delete them")
    print("restart Kitty once to finish disabling Workbench")


def parse_arguments(argv: Sequence[str] | None = None) -> InstallArguments:
    """Parse installer flags with Tyro into a fully typed configuration."""
    return tyro.cli(
        InstallArguments,
        prog="./install",
        description="Install, disable, or remove kitty-workbench safely.",
        args=list(argv) if argv is not None else None,
        config=(tyro.conf.HelptextFromCommentsOff,),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one installer action and translate expected failures to status one."""
    arguments = parse_arguments(argv)
    try:
        action = arguments.action()
        paths = install_paths(kitty_config=arguments.kitty_config)
        if action == "enable":
            _enable(paths)
        elif action == "disable":
            _disable(paths)
            print("restart Kitty once to unload the Workbench watcher and mappings")
        else:
            _uninstall(paths, purge=action == "purge")
    except (InstallError, TabBarInstallError) as error:
        print(f"kitty-workbench installer: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"kitty-workbench installer: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
