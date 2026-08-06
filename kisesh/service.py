"""Application service coordinating Kitty and persisted session operations."""

from __future__ import annotations

import hashlib
import os
import secrets
import shlex
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import cast

from .app_profiles import DEFAULT_APP_PROFILES, AppProfiles
from .context import (
    CONTEXT_SCHEMA_VERSION,
    build_context,
    merge_context,
    pending_restore_commands,
    remap_context_windows,
    rename_context_tab,
    restore_session,
    update_context_for_closing_pane,
)
from .domain import ClosingPaneCapture, KittyOsWindowState, SessionContext
from .filesystem import temporary_path
from .kitty_client import KittyClient, KittyController, KittyError, LiveTab
from .model import SESSION_SCOPE_VAR, SessionManifest, slugify
from .session_file import clean_tab_title, rename_snapshot_tab, sanitize_session, snapshot_summary
from .store import SessionStore, StoredSession, StoreError


class KiSeshError(RuntimeError):
    """Raised when a requested session operation violates lifecycle rules."""


class UnownedTabsAction(StrEnum):
    """Explicit policy for tabs outside KiSesh when opening a session."""

    ATTACH = "attach"
    SAVE_SEPARATELY = "save-separately"
    DISCARD = "discard"


@dataclass(slots=True, frozen=True)
class UnownedTabsInfo:
    """Count and editable random name for current unowned tabs."""

    count: int
    suggested_name: str


@dataclass(slots=True, frozen=True)
class UnownedTabsDecision:
    """Chosen unowned-tab policy with an optional separate-session name."""

    action: UnownedTabsAction
    name: str | None = None


@dataclass(slots=True)
class SessionView:
    """Stored session data enriched with matching live Kitty tabs."""

    stored: StoredSession
    live_tabs: list[LiveTab]
    context: SessionContext | None = None

    @property
    def live(self) -> bool:
        """Report whether at least one owned Kitty tab is currently running."""
        return bool(self.live_tabs)


PaneTextReader = Callable[[int], str | None]
KittyFactory = Callable[[], KittyController]

_RANDOM_NAME_ADJECTIVES = (
    "Amber",
    "Brisk",
    "Calm",
    "Cedar",
    "Cobalt",
    "Coral",
    "Daring",
    "Ember",
    "Gentle",
    "Indigo",
    "Lunar",
    "Mellow",
    "Quiet",
    "Silver",
    "Swift",
    "Verdant",
)
_RANDOM_NAME_NOUNS = (
    "Badger",
    "Falcon",
    "Heron",
    "Lynx",
    "Marten",
    "Otter",
    "Panda",
    "Raven",
    "Seal",
    "Sparrow",
    "Tiger",
    "Wolf",
)


def _capture_pane_texts(
    tabs: Iterable[LiveTab],
    reader: PaneTextReader,
) -> dict[int, str]:
    """Capture available text independently so one failed pane cannot abort a save."""
    captured: dict[int, str] = {}
    for tab in tabs:
        for window in tab.windows:
            try:
                text = reader(window["id"])
            except KittyError:
                continue
            if isinstance(text, str):
                captured[window["id"]] = text
    return captured


def _random_session_name(store: SessionStore) -> str:
    """Build a readable random name that does not collide with a known session."""
    noun_count = len(_RANDOM_NAME_NOUNS)
    index = secrets.randbelow(len(_RANDOM_NAME_ADJECTIVES) * noun_count)
    base = (
        f"{_RANDOM_NAME_ADJECTIVES[index // noun_count]} {_RANDOM_NAME_NOUNS[index % noun_count]}"
    )
    candidate = base
    suffix = 2
    while not store.slug_available(slugify(candidate)):
        candidate = f"{base} {suffix}"
        suffix += 1
    return candidate


def _last_focused_at(tab: LiveTab) -> float:
    """Return the newest valid pane-focus timestamp within a tab."""
    timestamps = [
        value
        for window in tab.windows
        if isinstance((value := window.get("last_focused_at")), (int, float))
        and not isinstance(value, bool)
    ]
    return max(timestamps, default=0.0)


def _inherited_session_id(source: LiveTab, tabs: list[LiveTab]) -> str | None:
    """Infer ownership from Kitty's native session or the active KiSesh scope."""
    owned = [
        tab
        for tab in tabs
        if tab.os_window_id == source.os_window_id and tab.session_id() is not None
    ]
    native_name = source.native_session_name()
    if native_name is not None:
        native_ids = {
            session_id
            for tab in owned
            if tab.native_session_name() == native_name
            if (session_id := tab.session_id()) is not None
        }
        return next(iter(native_ids)) if len(native_ids) == 1 else None
    scope = str(source.os_window_id)
    scoped = [
        tab
        for tab in owned
        if any(
            window.get("user_vars", {}).get(SESSION_SCOPE_VAR) == scope for window in tab.windows
        )
    ]
    return max(scoped, key=_last_focused_at).session_id() if scoped else None


class KiSeshService:
    """Enforce session lifecycle rules across storage and live Kitty state."""

    def __init__(
        self,
        store: SessionStore,
        kitty: KittyController | None = None,
        kitty_factory: KittyFactory = KittyClient,
        profiles: AppProfiles = DEFAULT_APP_PROFILES,
    ) -> None:
        """Initialize service dependencies without connecting to Kitty eagerly."""
        self.store = store
        self.kitty = kitty
        self.kitty_factory = kitty_factory
        self.profiles = profiles

    def _kitty(self) -> KittyController:
        """Return the injected client or construct one lazily."""
        if self.kitty is None:
            self.kitty = self.kitty_factory()
        return self.kitty

    def _live_tabs_by_session(self) -> dict[str, list[LiveTab]]:
        """Group live tabs by session and degrade to saved-only state if Kitty fails."""
        try:
            tabs = self._kitty().tabs()
        except KittyError:
            self.kitty = None
            return {}
        grouped: dict[str, list[LiveTab]] = {}
        for tab in tabs:
            session_id = tab.session_id()
            if session_id:
                grouped.setdefault(session_id, []).append(tab)
        return grouped

    def views(self) -> list[SessionView]:
        """Return every active and archived session with best-effort live state."""
        live_by_id = self._live_tabs_by_session()
        views: list[SessionView] = []
        for stored in self.store.list(include_archived=True):
            try:
                context = self.store.read_context(stored.manifest.id)
            except StoreError:
                context = None
            views.append(
                SessionView(
                    stored=stored,
                    live_tabs=live_by_id.get(stored.manifest.id, []),
                    context=context,
                )
            )
        return views

    def create_from_active(self, name: str, project_root: str | None = None) -> StoredSession:
        """Create a session, using an unowned source or opening fresh from an owned one."""
        clean_name = name.strip()
        if not clean_name:
            raise KiSeshError("session name cannot be empty")
        client = self._kitty()
        tab = client.focused_tab(
            client.list_state(),
            exclude_window_id=_environment_window_id(),
        )
        root = project_root or tab.suggested_root()
        if tab.session_id():
            return self._create_and_open_blank_session(client, clean_name, root)
        stored = self._create_tabs_session(
            clean_name,
            root,
            [tab],
        )
        client.activate_session(stored.manifest.id, tab)
        return stored

    def create_from_unowned(
        self,
        name: str,
        decision: UnownedTabsDecision,
        project_root: str | None = None,
    ) -> StoredSession:
        """Create a session after explicitly resolving every unowned source tab."""
        clean_name = name.strip()
        if not clean_name:
            raise KiSeshError("session name cannot be empty")
        if decision.action is not UnownedTabsAction.SAVE_SEPARATELY and decision.name is not None:
            raise KiSeshError("only save-separately accepts an unowned session name")

        client = self._kitty()
        state = client.list_state()
        source = client.focused_tab(state, exclude_window_id=_environment_window_id())
        tabs = self._source_unowned_tabs(client, state)
        if not tabs:
            raise KiSeshError("the current Kitty window has no unowned tabs")
        root = project_root or source.suggested_root()

        if decision.action is UnownedTabsAction.ATTACH:
            stored = self._create_tabs_session(clean_name, root, tabs)
            active_tab = next((tab for tab in tabs if tab.tab_id == source.tab_id), tabs[0])
            client.activate_session(stored.manifest.id, active_tab)
            return stored

        return self._create_and_open_blank_session(client, clean_name, root, decision)

    def _create_and_open_blank_session(
        self,
        client: KittyController,
        name: str,
        project_root: str,
        decision: UnownedTabsDecision | None = None,
    ) -> StoredSession:
        """Open one fresh shell and remove its snapshot if opening fails before it is live."""
        blank = self._create_blank_session(name, project_root)
        try:
            return self.open(blank.manifest.id, decision)
        except Exception:
            self._remove_unopened_session(client, blank.manifest.id)
            raise

    def _create_blank_session(self, name: str, project_root: str) -> StoredSession:
        """Persist a one-shell snapshot that Kitty can open as a native session."""
        stored = self.store.create(name, project_root)
        try:
            raw = shlex.join(("launch", f"--cwd={project_root}"))
            safe = sanitize_session(raw, stored.manifest)
            return self.store.write_snapshot(
                stored.manifest.id,
                safe,
                snapshot_summary(safe),
            )
        except Exception:
            with suppress(StoreError):
                self.store.move_to_trash(stored.manifest.id)
            raise

    def _remove_unopened_session(self, client: KittyController, session_id: str) -> None:
        """Remove a failed blank session only when Kitty has no live tab for it."""
        try:
            live = client.tabs_for_session(session_id)
        except KittyError:
            return
        if not live:
            with suppress(StoreError):
                self.store.move_to_trash(session_id)

    def _create_tabs_session(
        self,
        name: str,
        project_root: str,
        tabs: list[LiveTab],
    ) -> StoredSession:
        """Create, stamp, and save a session with recoverable failure cleanup."""
        client = self._kitty()
        stored = self.store.create(name, project_root)
        try:
            for tab in tabs:
                client.stamp_tab(tab, stored.manifest)
            return self.save(stored.manifest.id)
        except Exception:
            for tab in tabs:
                self._rollback_membership(partial(client.clear_tab_session, tab))
            with suppress(StoreError):
                self.store.move_to_trash(stored.manifest.id)
            raise

    def _source_unowned_tabs(
        self,
        client: KittyController,
        state: list[KittyOsWindowState],
    ) -> list[LiveTab]:
        """Join inherited tabs, then return genuinely unowned source-window tabs."""
        source = client.focused_tab(
            state,
            exclude_window_id=_environment_window_id(),
        )
        if source.session_id():
            return []
        all_tabs = client.tabs(state)
        unowned = sorted(
            (
                tab
                for tab in all_tabs
                if tab.os_window_id == source.os_window_id and tab.session_id() is None
            ),
            key=lambda tab: tab.index,
        )
        session_id = _inherited_session_id(source, all_tabs)
        if session_id is None:
            return unowned
        try:
            stored = self.store.get(session_id)
        except StoreError:
            return unowned
        native_name = source.native_session_name()
        inherited = [
            tab
            for tab in unowned
            if native_name is None or tab.native_session_name() == native_name
        ]
        try:
            for tab in inherited:
                client.stamp_tab(tab, stored.manifest)
            self.save(stored.manifest.id)
        except Exception:
            for tab in inherited:
                self._rollback_membership(partial(client.clear_tab_session, tab))
            raise
        inherited_ids = {tab.tab_id for tab in inherited}
        return [tab for tab in unowned if tab.tab_id not in inherited_ids]

    def unowned_tabs_info(self) -> UnownedTabsInfo | None:
        """Describe source-window tabs requiring a choice before session opening."""
        client = self._kitty()
        tabs = self._source_unowned_tabs(client, client.list_state())
        if not tabs:
            return None
        return UnownedTabsInfo(len(tabs), _random_session_name(self.store))

    def add_current_tab(self, slug_or_id: str) -> StoredSession:
        """Attach the focused source tab to an already live selected session."""
        stored = self.store.get(slug_or_id)
        if stored.manifest.status == "archived":
            raise KiSeshError("unarchive the session before adding a tab")
        client = self._kitty()
        state = client.list_state()
        tab = client.focused_tab(state, exclude_window_id=_environment_window_id())
        current_id = tab.session_id()
        if current_id and current_id != stored.manifest.id:
            raise KiSeshError("the current tab belongs to another session")
        live_tabs = client.tabs_for_session(stored.manifest.id, state)
        if current_id != stored.manifest.id and stored.snapshot_path.is_file() and not live_tabs:
            raise KiSeshError("open the saved session before adding a live tab")
        if current_id == stored.manifest.id:
            return self.save(stored.manifest.id)
        client.stamp_tab(tab, stored.manifest)
        try:
            return self.save(stored.manifest.id)
        except Exception:
            self._rollback_membership(lambda: client.clear_tab_session(tab))
            raise

    def detach_current_tab(self, slug_or_id: str) -> StoredSession:
        """Detach the source tab without closing it or orphaning the session."""
        stored = self.store.get(slug_or_id)
        client = self._kitty()
        state = client.list_state()
        tab = client.focused_tab(state, exclude_window_id=_environment_window_id())
        if tab.session_id() != stored.manifest.id:
            raise KiSeshError("the current tab does not belong to the selected session")
        remaining = [
            candidate
            for candidate in client.tabs_for_session(stored.manifest.id, state)
            if candidate.tab_id != tab.tab_id
        ]
        if not remaining:
            raise KiSeshError("cannot detach the session's only live tab")
        client.clear_tab_session(tab)
        try:
            return self.save(stored.manifest.id)
        except Exception:
            self._rollback_membership(lambda: client.stamp_tab(tab, stored.manifest))
            raise

    @staticmethod
    def _rollback_membership(operation: Callable[[], None]) -> None:
        """Attempt a membership rollback without obscuring the original failure."""
        try:
            operation()
        except KittyError:
            return

    def copy_current_tab(self, slug_or_id: str) -> StoredSession:
        """Append a source tab's safe layout and context to an inactive target."""
        target = self.store.get(slug_or_id)
        if target.manifest.status == "archived":
            raise KiSeshError("unarchive the target session before copying a tab")
        client = self._kitty()
        state = client.list_state()
        source = client.focused_tab(state, exclude_window_id=_environment_window_id())
        if source.session_id() == target.manifest.id:
            raise KiSeshError("the current tab already belongs to the target session")
        if client.tabs_for_session(target.manifest.id, state):
            raise KiSeshError("copy tab requires a saved target session; close its live tabs first")
        copied_snapshot = self._capture_copied_tab(client, source, target.manifest)
        existing = (
            target.snapshot_path.read_text(encoding="utf-8")
            if target.snapshot_path.is_file()
            else ""
        )
        combined = sanitize_session(f"{existing.rstrip()}\n{copied_snapshot}", target.manifest)
        copied = self.store.write_snapshot(
            target.manifest.id,
            combined,
            snapshot_summary(combined),
        )
        addition = build_context(
            [source],
            command_outputs=_capture_pane_texts([source], client.last_command_output),
            terminal_histories=_capture_pane_texts([source], client.terminal_history),
            profiles=self.profiles,
        )
        context = merge_context(self.store.read_context(target.manifest.id), addition)
        context["snapshot_revision"] = copied.manifest.revision
        self.store.write_context(copied.manifest.id, context)
        return self.store.get(copied.manifest.id)

    def _capture_copied_tab(
        self,
        client: KittyController,
        source: LiveTab,
        manifest: SessionManifest,
    ) -> str:
        """Capture and sanitize one source tab through an isolated temporary file."""
        with temporary_path(
            self.store.root,
            prefix=".copy-tab.",
            suffix=".kitty-session",
        ) as capture_path:
            client.capture_tab(source, capture_path, str(uuid.uuid4()))
            raw = capture_path.read_text(encoding="utf-8")
        return sanitize_session(raw, manifest)

    def current_session(self) -> StoredSession:
        """Resolve the focused tab's current session identity."""
        client = self._kitty()
        tab = client.focused_tab(
            client.list_state(),
            exclude_window_id=_environment_window_id(),
        )
        session_id = tab.session_id()
        if not session_id:
            raise KiSeshError("the current tab does not belong to a session")
        return self.store.get(session_id)

    def save_current(self) -> StoredSession:
        """Save the session owning the focused source tab."""
        return self.save(self.current_session().manifest.id)

    def save(
        self,
        slug_or_id: str,
        command_events: Iterable[Mapping[str, object]] = (),
    ) -> StoredSession:
        """Capture a live session's safe layout, commands, and terminal buffers."""
        stored = self.store.get(slug_or_id)
        client = self._kitty()
        state = client.list_state()
        live_tabs = client.tabs_for_session(stored.manifest.id, state)
        if not live_tabs:
            raise KiSeshError(f"session is not live: {stored.manifest.name}")
        excluded_window_id = _environment_window_id()
        for tab in live_tabs:
            client.stamp_tab(
                tab,
                stored.manifest,
                exclude_window_id=excluded_window_id,
            )
        raw = self._capture_live_session(client, stored.manifest.id)
        safe = sanitize_session(raw, stored.manifest)
        updated = self.store.write_snapshot(
            stored.manifest.id,
            safe,
            snapshot_summary(safe),
        )
        events = list(command_events)
        command_outputs = _capture_pane_texts(live_tabs, client.last_command_output)
        terminal_histories = _capture_pane_texts(live_tabs, client.terminal_history)

        def merge_latest(existing: SessionContext | None) -> SessionContext:
            """Merge captures with any close update committed during remote reads."""
            context = build_context(
                live_tabs,
                existing,
                events,
                command_outputs,
                terminal_histories,
                profiles=self.profiles,
            )
            context["snapshot_revision"] = updated.manifest.revision
            return context

        return self.store.update_context(updated.manifest.id, merge_latest)

    def save_and_close(
        self,
        slug_or_id: str,
        promote_os_window_id: int | None = None,
    ) -> StoredSession:
        """Persist a live session, close it, then optionally focus its successor."""
        stored = self.save(slug_or_id)
        client = self._kitty()
        client.close_session_tabs(stored.manifest.id)
        if promote_os_window_id is not None:
            self._promote_live_session(client, promote_os_window_id)
        return stored

    def _promote_live_session(
        self,
        client: KittyController,
        os_window_id: int,
    ) -> None:
        """Best-effort focus the active stored session remaining in one OS window."""
        try:
            tabs = client.tabs()
        except KittyError:
            return
        candidates = sorted(
            (tab for tab in tabs if tab.os_window_id == os_window_id),
            key=lambda tab: (not tab.is_focused, not tab.is_active, tab.index),
        )
        for tab in candidates:
            session_id = tab.session_id()
            if session_id is None:
                continue
            try:
                stored = self.store.get(session_id)
            except StoreError:
                continue
            if stored.manifest.status != "active":
                continue
            try:
                client.activate_session(session_id, tab)
            except KittyError:
                return
            return

    def _capture_live_session(self, client: KittyController, session_id: str) -> str:
        """Capture a complete live session through an isolated temporary file."""
        with temporary_path(
            self.store.root,
            prefix=".capture.",
            suffix=".kitty-session",
        ) as capture_path:
            client.capture_session(session_id, capture_path)
            return capture_path.read_text(encoding="utf-8")

    def context(self, slug_or_id: str) -> SessionContext | None:
        """Return a session's persisted command and terminal context."""
        return self.store.read_context(slug_or_id)

    def _prepare_unowned_tabs(
        self,
        tabs: list[LiveTab],
        decision: UnownedTabsDecision | None,
    ) -> UnownedTabsAction | None:
        """Validate a switch decision and persist a separately named source session."""
        if decision is not None and (
            decision.action is not UnownedTabsAction.SAVE_SEPARATELY and decision.name is not None
        ):
            raise KiSeshError("only save-separately accepts an unowned session name")
        if tabs and decision is None:
            raise KiSeshError(
                f"{len(tabs)} unowned tab(s) require attach, save-separately, or discard"
            )
        action = decision.action if decision is not None else None
        if tabs and action is UnownedTabsAction.SAVE_SEPARATELY:
            requested_name = decision.name if decision is not None else None
            separate_name = (requested_name or _random_session_name(self.store)).strip()
            if not separate_name:
                raise KiSeshError("the separate session name cannot be empty")
            self._create_tabs_session(
                separate_name,
                tabs[0].suggested_root(),
                tabs,
            )
        return action

    def open(
        self,
        slug_or_id: str,
        unowned_decision: UnownedTabsDecision | None = None,
    ) -> StoredSession:
        """Resolve unowned tabs, then focus or restore an isolated session."""
        stored = self.store.get(slug_or_id)
        client = self._kitty()
        state = client.list_state()
        live_tabs = client.tabs_for_session(stored.manifest.id, state)
        if not live_tabs and not stored.snapshot_path.is_file():
            raise KiSeshError(f"session has no snapshot: {stored.manifest.name}")
        unowned_tabs = self._source_unowned_tabs(client, state)
        action = self._prepare_unowned_tabs(unowned_tabs, unowned_decision)
        if unowned_tabs and action is UnownedTabsAction.SAVE_SEPARATELY:
            state = client.list_state()
            live_tabs = client.tabs_for_session(stored.manifest.id, state)

        if not live_tabs:
            if stored.manifest.status == "archived":
                stored = self.store.restore_archive(stored.manifest.id)
            context = self.store.read_context(stored.manifest.id)
            self._open_inactive_snapshot(client, stored, context)
            live_tabs = self._opened_tabs_and_prefill(client, stored.manifest.id, context)
            if context is not None and live_tabs:
                self.store.write_context(
                    stored.manifest.id,
                    remap_context_windows(context, live_tabs),
                )

        if unowned_tabs and action is UnownedTabsAction.ATTACH:
            if not live_tabs:
                raise KiSeshError("opened session did not create any live tabs")
            for tab in unowned_tabs:
                client.stamp_tab(tab, stored.manifest)
            try:
                stored = self.save(stored.manifest.id)
            except Exception:
                for tab in unowned_tabs:
                    self._rollback_membership(partial(client.clear_tab_session, tab))
                raise
            live_tabs = client.tabs_for_session(stored.manifest.id)

        if live_tabs:
            source_os_window_id = unowned_tabs[0].os_window_id if unowned_tabs else None
            active_tab = next(
                (
                    tab
                    for tab in live_tabs
                    if source_os_window_id is not None and tab.os_window_id == source_os_window_id
                ),
                live_tabs[0],
            )
            client.activate_session(stored.manifest.id, active_tab)
            if unowned_tabs and action is UnownedTabsAction.DISCARD:
                client.close_tabs(tab.tab_id for tab in unowned_tabs)
        return self.store.mark_used(stored.manifest.id)

    def _open_inactive_snapshot(
        self,
        client: KittyController,
        stored: StoredSession,
        context: SessionContext | None,
    ) -> None:
        """Open the stored snapshot directly or through a generated restore file."""
        stored_snapshot = stored.snapshot_path.read_text(encoding="utf-8")
        snapshot = sanitize_session(stored_snapshot, stored.manifest)
        shell_restorer = (
            str(Path(__file__).resolve().parents[1] / "bin" / "kisesh"),
            "--data-dir",
            str(self.store.root),
            "restore-shell",
            stored.manifest.id,
        )
        resumable = restore_session(snapshot, context, shell_restore_argv=shell_restorer)
        if resumable == stored_snapshot:
            client.open_snapshot(stored.snapshot_path)
            return
        with temporary_path(
            self.store.root,
            prefix=f".{stored.manifest.slug}.restore.",
            suffix=".kitty-session",
        ) as restore_path:
            restore_path.write_text(resumable, encoding="utf-8")
            client.open_snapshot(restore_path)

    def _opened_tabs_and_prefill(
        self,
        client: KittyController,
        session_id: str,
        context: SessionContext | None,
    ) -> list[LiveTab]:
        """Prefill restored reminders and return current live tab identities."""
        opened_tabs = client.tabs_for_session(session_id)
        if not opened_tabs:
            return []
        for (tab_index, pane_index), command in pending_restore_commands(context).items():
            if not 0 <= tab_index < len(opened_tabs):
                continue
            windows = opened_tabs[tab_index].windows
            if 0 <= pane_index < len(windows):
                client.send_text(windows[pane_index]["id"], command)
        return opened_tabs

    def save_closing_pane(
        self,
        session_id: str,
        capture: ClosingPaneCapture,
    ) -> StoredSession:
        """Persist synchronous pre-close pane state without querying dead Kitty tabs."""
        return self.store.update_context(
            session_id,
            lambda existing: update_context_for_closing_pane(
                existing,
                capture,
                self.profiles,
            ),
        )

    def rename(self, slug_or_id: str, new_name: str) -> StoredSession:
        """Rename stored and live ownership markers without replaying processes."""
        old = self.store.get(slug_or_id)
        renamed = self.store.rename(old.manifest.id, new_name)
        if renamed.snapshot_path.is_file():
            raw = renamed.snapshot_path.read_text(encoding="utf-8")
            safe = sanitize_session(raw, renamed.manifest)
            renamed = self.store.write_snapshot(
                renamed.manifest.id,
                safe,
                snapshot_summary(safe),
            )
        try:
            self._kitty().restamp_session(
                renamed.manifest.id,
                renamed.manifest.slug,
                renamed.manifest.name,
            )
        except KittyError:
            return renamed
        return renamed

    def rename_tab(self, slug_or_id: str, tab_index: int, new_title: str) -> StoredSession:
        """Rename one live or saved tab and persist the resulting session state."""
        title = clean_tab_title(new_title)
        stored = self.store.get(slug_or_id)
        client = self._kitty()
        try:
            live_tabs = client.tabs_for_session(stored.manifest.id)
        except KittyError:
            live_tabs = []
        if live_tabs:
            if not 0 <= tab_index < len(live_tabs):
                raise KiSeshError("tab index is outside the live session")
            tab = live_tabs[tab_index]
            previous_title = tab.title
            client.rename_tab(tab.tab_id, title)
            try:
                return self.save(stored.manifest.id)
            except Exception:
                with suppress(KittyError):
                    client.rename_tab(tab.tab_id, previous_title)
                raise

        if not stored.snapshot_path.is_file():
            raise KiSeshError("session has no saved tab layout")
        original = stored.snapshot_path.read_text(encoding="utf-8")
        original_context = self.store.read_context(stored.manifest.id)
        try:
            renamed_snapshot = rename_snapshot_tab(original, tab_index, title)
            renamed_context = rename_context_tab(
                original_context,
                tab_index,
                title,
            )
        except IndexError as error:
            raise KiSeshError(str(error)) from error
        updated = self.store.write_snapshot(
            stored.manifest.id,
            renamed_snapshot,
            snapshot_summary(renamed_snapshot),
        )
        if renamed_context is None:
            return updated
        renamed_context["snapshot_revision"] = updated.manifest.revision
        try:
            self.store.write_context(updated.manifest.id, renamed_context)
        except (OSError, StoreError):
            with suppress(OSError, StoreError):
                restored = self.store.write_snapshot(
                    stored.manifest.id,
                    original,
                    snapshot_summary(original),
                )
                context_to_restore = cast(SessionContext, original_context)
                context_to_restore["snapshot_revision"] = restored.manifest.revision
                self.store.write_context(restored.manifest.id, context_to_restore)
            raise
        return updated

    def _require_inactive(self, stored: StoredSession, operation: str) -> None:
        """Reject destructive lifecycle changes when Kitty confirms live tabs."""
        try:
            live = bool(self._kitty().tabs_for_session(stored.manifest.id))
        except KittyError:
            live = False
        if live:
            raise KiSeshError(f"live sessions cannot be {operation}")

    def archive(self, slug_or_id: str) -> StoredSession:
        """Archive an inactive session so it leaves the primary list."""
        stored = self.store.get(slug_or_id)
        self._require_inactive(stored, "archived")
        return self.store.archive(stored.manifest.id)

    def unarchive(self, slug_or_id: str) -> StoredSession:
        """Return an archived session to the primary saved-session list."""
        stored = self.store.get(slug_or_id)
        if stored.manifest.status != "archived":
            raise KiSeshError(f"session is not archived: {stored.manifest.name}")
        return self.store.restore_archive(stored.manifest.id)

    def remove(self, slug_or_id: str) -> Path:
        """Move an inactive saved or archived session to recoverable trash."""
        stored = self.store.get(slug_or_id)
        self._require_inactive(stored, "removed")
        return self.store.move_to_trash(stored.manifest.id)

    def doctor(self) -> list[str]:
        """Inspect storage, snapshots, context schemas, and the Kitty connection."""
        try:
            sessions = self.store.list(include_archived=True)
        except Exception as error:
            return [f"ERROR store: {error}"]
        findings = [f"OK store: {len(sessions)} session(s)"]
        for stored in sessions:
            findings.extend(self._session_findings(stored))
        try:
            state = self._kitty().list_state()
            findings.append(f"OK kitty: {len(state)} OS window(s)")
        except KittyError as error:
            findings.append(f"WARN kitty: {error}")
        return findings

    def _session_findings(self, stored: StoredSession) -> list[str]:
        """Return integrity findings for one stored snapshot and context."""
        if not stored.snapshot_path.exists():
            return [f"WARN {stored.manifest.slug}: no snapshot"]
        findings: list[str] = []
        raw = stored.snapshot_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if stored.manifest.snapshot_sha256 and digest != stored.manifest.snapshot_sha256:
            findings.append(f"ERROR {stored.manifest.slug}: snapshot checksum mismatch")
        try:
            normalized = sanitize_session(raw, stored.manifest)
        except ValueError as error:
            findings.append(f"ERROR {stored.manifest.slug}: invalid snapshot: {error}")
        else:
            if normalized != raw:
                findings.append(f"ERROR {stored.manifest.slug}: snapshot is not safely normalized")
        try:
            context = self.store.read_context(stored.manifest.id)
        except StoreError as error:
            findings.append(f"ERROR {stored.manifest.slug}: {error}")
        else:
            if context is not None and context.get("schema_version") != CONTEXT_SCHEMA_VERSION:
                findings.append(f"ERROR {stored.manifest.slug}: unsupported context schema")
        return findings


def _environment_window_id() -> int | None:
    """Return the invoking overlay ID only when caller metadata confirms one."""
    if os.environ.get("KISESH_CALLER") not in {"overlay", "manager"}:
        return None
    value = os.environ.get("KITTY_WINDOW_ID")
    try:
        return int(value) if value else None
    except ValueError:
        return None
