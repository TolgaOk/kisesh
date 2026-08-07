"""Install a stable Kitty runtime without depending on a source checkout layout."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .filesystem import atomic_write_text

RUNTIME_SCHEMA = 1
RUNTIME_MANIFEST = ".kisesh-runtime.json"


class RuntimeInstallError(RuntimeError):
    """A runtime ownership or deployment problem that must fail closed."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Source resources, stable runtime target, and persistent CLI launcher."""

    source: Path
    package: Path
    integration: Path
    panel: Path
    launcher: Path
    target: Path


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Ownership and symlink targets for one deployed runtime directory."""

    deployment: str
    source: str
    package: str
    integration: str
    panel: str
    launcher: str

    def to_json(self) -> str:
        """Serialize the manifest in a deterministic, inspectable form."""
        return (
            json.dumps(
                {
                    "schema": RUNTIME_SCHEMA,
                    "product": "kisesh",
                    "deployment": self.deployment,
                    "source": self.source,
                    "package": self.package,
                    "integration": self.integration,
                    "panel": self.panel,
                    "launcher": self.launcher,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


@dataclass(frozen=True, slots=True)
class RuntimeTransaction:
    """A deployed runtime and enough state to commit or restore its predecessor."""

    target: Path
    deployment: str | None = None
    backup: Path | None = None
    previous_symlink: str | None = None

    @property
    def changed(self) -> bool:
        """Report whether this transaction replaced or created a runtime."""
        return self.deployment is not None


def runtime_paths(
    source: Path,
    launcher: Path,
    panel_launcher: Path,
    target: Path,
) -> RuntimePaths:
    """Resolve the packaged resources used to construct the stable runtime."""
    package = source / "kisesh"
    integration = package / "integration"
    return RuntimePaths(
        source=source,
        package=package,
        integration=integration,
        panel=panel_launcher,
        launcher=launcher,
        target=target,
    )


def validate_runtime_source(paths: RuntimePaths) -> None:
    """Require every file used by Kitty or the persistent launcher."""
    required = (
        paths.package / "__init__.py",
        paths.package / "watcher.py",
        paths.package / "close_guard.py",
        paths.package / "session_bar.py",
        paths.integration / "kisesh.conf",
        paths.integration / "actions.py",
        paths.integration / "tab_bar.py",
        paths.integration / "quick-access-terminal.conf",
        paths.panel,
        paths.launcher,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeInstallError(f"package is incomplete; missing: {', '.join(missing)}")
    for launcher in (paths.launcher, paths.panel):
        if not os.access(launcher, os.X_OK):
            raise RuntimeInstallError(f"launcher is not executable: {launcher}")


def _manifest_from_payload(payload: object, path: Path) -> RuntimeManifest:
    """Validate one decoded manifest without accepting partial ownership data."""
    if not isinstance(payload, dict):
        raise RuntimeInstallError(f"invalid runtime manifest: {path}")
    required = ("deployment", "source", "package", "integration", "panel", "launcher")
    if (
        payload.get("schema") != RUNTIME_SCHEMA
        or payload.get("product") != "kisesh"
        or not all(isinstance(payload.get(key), str) and payload.get(key) for key in required)
    ):
        raise RuntimeInstallError(f"invalid runtime manifest: {path}")
    return RuntimeManifest(
        deployment=cast(str, payload["deployment"]),
        source=cast(str, payload["source"]),
        package=cast(str, payload["package"]),
        integration=cast(str, payload["integration"]),
        panel=cast(str, payload["panel"]),
        launcher=cast(str, payload["launcher"]),
    )


def _read_manifest(target: Path) -> RuntimeManifest:
    """Read a managed runtime manifest or reject the directory as foreign."""
    path = target / RUNTIME_MANIFEST
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeInstallError(f"cannot verify existing runtime: {target}") from error
    return _manifest_from_payload(payload, path)


def _same_link(link: Path, expected: str) -> bool:
    """Compare one required symlink with its recorded absolute target."""
    if not link.is_symlink():
        return False
    try:
        return link.resolve(strict=True) == Path(expected).resolve(strict=True)
    except OSError:
        return False


def _verify_managed_runtime(target: Path) -> RuntimeManifest:
    """Verify the complete runtime tree before replacing or deleting it."""
    manifest = _read_manifest(target)
    if {path.name for path in target.iterdir()} != {
        RUNTIME_MANIFEST,
        "bin",
        "integration",
        "kisesh",
    }:
        raise RuntimeInstallError(f"runtime contains unrecognized files: {target}")
    binary = target / "bin"
    if not binary.is_dir() or binary.is_symlink():
        raise RuntimeInstallError(f"invalid runtime binary directory: {binary}")
    if {path.name for path in binary.iterdir()} != {"kisesh", "kisesh-panel"}:
        raise RuntimeInstallError(f"runtime contains unrecognized launchers: {binary}")
    links = (
        (target / "kisesh", manifest.package),
        (target / "integration", manifest.integration),
        (binary / "kisesh", manifest.launcher),
        (binary / "kisesh-panel", manifest.panel),
    )
    if not all(_same_link(link, expected) for link, expected in links):
        raise RuntimeInstallError(f"runtime links were modified: {target}")
    return manifest


def _is_previous_source_link(target: Path, source: Path) -> bool:
    """Recognize the source-root symlink created by pre-wheel installers."""
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        return False
    if resolved == source.resolve(strict=True):
        return True
    package = resolved / "kisesh"
    return (package / "__init__.py").is_file() and (
        (resolved / "integration" / "kisesh.conf").is_file()
        or (package / "integration" / "kisesh.conf").is_file()
    )


def _in_place_source(paths: RuntimePaths) -> bool:
    """Report whether the stable target is the source directory itself."""
    if paths.target.is_symlink():
        return False
    try:
        return paths.target.resolve(strict=False) == paths.source.resolve(strict=True)
    except OSError:
        return False


def check_runtime_target(paths: RuntimePaths) -> RuntimeManifest | None:
    """Validate an absent, in-place, previous, or managed runtime target."""
    target = paths.target
    if _in_place_source(paths):
        return None
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink():
        if _is_previous_source_link(target, paths.source):
            return None
        raise RuntimeInstallError(f"refusing to replace foreign runtime link: {target}")
    if not target.is_dir():
        raise RuntimeInstallError(f"refusing to replace foreign runtime path: {target}")
    return _verify_managed_runtime(target)


def _absolute(path: Path) -> str:
    """Resolve one required deployment source to a stable absolute string."""
    return str(path.resolve(strict=True))


def _desired_manifest(paths: RuntimePaths) -> RuntimeManifest:
    """Create ownership metadata for a fresh runtime deployment."""
    return RuntimeManifest(
        deployment=uuid.uuid4().hex,
        source=_absolute(paths.source),
        package=_absolute(paths.package),
        integration=_absolute(paths.integration),
        panel=_absolute(paths.panel),
        launcher=_absolute(paths.launcher),
    )


def _is_current(manifest: RuntimeManifest, paths: RuntimePaths) -> bool:
    """Report whether an existing runtime already points at every desired resource."""
    return (
        manifest.source == _absolute(paths.source)
        and manifest.package == _absolute(paths.package)
        and manifest.integration == _absolute(paths.integration)
        and manifest.panel == _absolute(paths.panel)
        and manifest.launcher == _absolute(paths.launcher)
    )


def _stage_runtime(paths: RuntimePaths, manifest: RuntimeManifest) -> Path:
    """Build a complete runtime beside its target before changing live paths."""
    paths.target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".kisesh-runtime.", dir=paths.target.parent))
    try:
        (stage / "bin").mkdir()
        (stage / "kisesh").symlink_to(manifest.package, target_is_directory=True)
        (stage / "integration").symlink_to(manifest.integration, target_is_directory=True)
        (stage / "bin" / "kisesh").symlink_to(manifest.launcher)
        (stage / "bin" / "kisesh-panel").symlink_to(manifest.panel)
        atomic_write_text(stage / RUNTIME_MANIFEST, manifest.to_json(), mode=0o600)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


def _backup_path(target: Path) -> Path:
    """Reserve a unique sibling name for a rollback-safe previous runtime."""
    backup = Path(tempfile.mkdtemp(prefix=".kisesh-runtime.previous.", dir=target.parent))
    backup.rmdir()
    return backup


def deploy_runtime(paths: RuntimePaths) -> RuntimeTransaction:
    """Install or update the runtime while retaining exact rollback state."""
    validate_runtime_source(paths)
    existing = check_runtime_target(paths)
    if _in_place_source(paths) or (existing is not None and _is_current(existing, paths)):
        return RuntimeTransaction(paths.target)
    manifest = _desired_manifest(paths)
    stage = _stage_runtime(paths, manifest)
    previous_symlink: str | None = None
    backup: Path | None = None
    try:
        if paths.target.is_symlink():
            previous_symlink = os.readlink(paths.target)
            paths.target.unlink()
        elif paths.target.exists():
            backup = _backup_path(paths.target)
            os.replace(paths.target, backup)
        os.replace(stage, paths.target)
    except Exception:
        if backup is not None and backup.exists() and not paths.target.exists():
            os.replace(backup, paths.target)
        elif previous_symlink is not None and not paths.target.exists():
            paths.target.symlink_to(previous_symlink, target_is_directory=True)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return RuntimeTransaction(
        target=paths.target,
        deployment=manifest.deployment,
        backup=backup,
        previous_symlink=previous_symlink,
    )


def rollback_runtime(transaction: RuntimeTransaction) -> None:
    """Remove only this transaction's deployment and restore its predecessor."""
    if not transaction.changed:
        return
    manifest = _verify_managed_runtime(transaction.target)
    if manifest.deployment != transaction.deployment:
        raise RuntimeInstallError(f"refusing to roll back changed runtime: {transaction.target}")
    shutil.rmtree(transaction.target)
    if transaction.backup is not None:
        os.replace(transaction.backup, transaction.target)
    elif transaction.previous_symlink is not None:
        transaction.target.symlink_to(transaction.previous_symlink, target_is_directory=True)


def finish_runtime(transaction: RuntimeTransaction) -> None:
    """Discard a committed transaction's verified previous runtime backup."""
    if transaction.backup is not None:
        _verify_managed_runtime(transaction.backup)
        shutil.rmtree(transaction.backup)


def remove_runtime(paths: RuntimePaths) -> bool:
    """Remove only a verified managed runtime or previous installer symlink."""
    check_runtime_target(paths)
    if _in_place_source(paths) or (not paths.target.exists() and not paths.target.is_symlink()):
        return False
    if paths.target.is_symlink():
        paths.target.unlink()
    else:
        _verify_managed_runtime(paths.target)
        shutil.rmtree(paths.target)
    return True


def ensure_command_link(link: Path, launcher: Path) -> bool:
    """Expose the CLI at Kitty's stable user-bin path without replacing foreign files."""
    resolved_launcher = launcher.resolve(strict=True)
    if link.exists() or link.is_symlink():
        try:
            if link.resolve(strict=True) == resolved_launcher:
                return False
        except OSError:
            pass
        raise RuntimeInstallError(f"refusing to replace existing command: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(resolved_launcher)
    return True


def remove_command_link(link: Path, launcher: Path) -> bool:
    """Remove only an installer-created symlink to the active CLI launcher."""
    if link.absolute() == launcher.absolute():
        return False
    if not link.is_symlink():
        return False
    try:
        matches = link.resolve(strict=True) == launcher.resolve(strict=True)
    except OSError:
        matches = False
    if not matches:
        raise RuntimeInstallError(f"refusing to remove changed command: {link}")
    link.unlink()
    return True
