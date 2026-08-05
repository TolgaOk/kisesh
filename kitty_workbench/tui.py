"""Theme-aware, Vim-keyed terminal interface for Workbench sessions."""

from __future__ import annotations

import curses
import textwrap
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from .preview import PanePreview, TabPreview, build_session_preview, is_shell_program
from .service import SessionView, UnownedTabsAction
from .store import StoredSession

ESCAPE_DELAY_MS = 25
MIN_HEIGHT = 8
MIN_WIDTH = 48
HELP_CENTER_HEIGHT = 12
FULL_TABLE_WIDTH = 88
COMPACT_TABLE_WIDTH = 64
HALF_PAGE_ROWS = 5

Hint = tuple[str, str]
Hints = tuple[Hint, ...]
TextEntryKind = Literal["empty", "spacer", "heading", "preview_empty"]
PreviewLineKind = Literal["tab", "panes"]
HelpSection = tuple[str, tuple[Hint, ...]]
CursesGlyph = str | bytes | int


class Screen(Protocol):
    """Curses-compatible screen operations used by rendering and input code."""

    def erase(self) -> None:
        """Clear all cells."""

    def getmaxyx(self) -> tuple[int, int]:
        """Return height and width."""

    def addstr(self, y: int, x: int, text: str, style: int = 0) -> None:
        """Render styled text at an absolute cell position."""

    def hline(self, y: int, x: int, glyph: CursesGlyph, length: int) -> None:
        """Render a horizontal line."""

    def vline(self, y: int, x: int, glyph: CursesGlyph, length: int) -> None:
        """Render a vertical line."""

    def refresh(self) -> None:
        """Flush pending cell changes."""

    def keypad(self, enabled: bool) -> None:
        """Enable or disable decoded special keys."""

    def timeout(self, delay: int) -> None:
        """Configure blocking input timeout."""

    def get_wch(self) -> object:
        """Read one decoded key."""

    def move(self, y: int, x: int) -> None:
        """Move the cursor."""

    def clrtoeol(self) -> None:
        """Clear from the cursor through the current line."""


class SessionOperations(Protocol):
    """Service operations required by the interactive manager."""

    def views(self) -> list[SessionView]:
        """Return current session views."""

    def create_from_active(self, name: str, project_root: str | None = None) -> StoredSession:
        """Create a session from the source tab."""

    def unarchive(self, slug_or_id: str) -> StoredSession:
        """Return an archived session to the active list."""

    def unowned_tab_count(self) -> int:
        """Count source tabs requiring an explicit opening policy."""

    def open(
        self,
        slug_or_id: str,
        unowned_action: UnownedTabsAction | None = None,
    ) -> StoredSession:
        """Focus or restore a session."""

    def add_current_tab(self, slug_or_id: str) -> StoredSession:
        """Attach the source tab."""

    def detach_current_tab(self, slug_or_id: str) -> StoredSession:
        """Detach the source tab."""

    def copy_current_tab(self, slug_or_id: str) -> StoredSession:
        """Copy the source tab."""

    def save(self, slug_or_id: str) -> StoredSession:
        """Save a live session."""

    def save_and_close(self, slug_or_id: str) -> StoredSession:
        """Save a live session before closing its tabs."""

    def rename(self, slug_or_id: str, new_name: str) -> StoredSession:
        """Rename a session."""

    def archive(self, slug_or_id: str) -> StoredSession:
        """Archive an inactive session."""

    def remove(self, slug_or_id: str) -> Path:
        """Move an inactive session to trash."""


_HELP_SECTIONS: tuple[HelpSection, ...] = (
    (
        "CLOSE & NAVIGATION",
        (
            ("q / Esc / h", "Close manager."),
            ("j / k", "Move selection."),
            ("C-d / C-u", "Move five rows."),
            ("g / G", "First / last session."),
            ("l / ↵ / Space", "Focus live / restore saved."),
            ("/", "Incremental search."),
        ),
    ),
    (
        "SESSION STATES",
        (
            ("● live", "Owned tabs are running."),
            ("○ saved", "Restorable snapshot; no live tab."),
            ("○ archived", "Dormant snapshot; u returns it."),
            ("switch", "Other sessions stay live but leave the tab bar."),
        ),
    ),
    (
        "SESSION CONTENTS",
        (
            ("├─ / └─", "Tabs inside the selected session."),
            ("✻ / ◇", "Claude / Codex pane."),
            ("•", "Focused pane."),
            ("↻", "Saved command can be restored or prefilled."),
        ),
    ),
    (
        "TAB MEMBERSHIP",
        (
            ("a", "Add the source tab to a live session."),
            ("d", "Detach the source tab; it keeps running."),
            ("c", "Copy the source tab into a saved session."),
        ),
    ),
    (
        "SESSION ACTIONS",
        (
            ("n", "Create from the source tab."),
            ("s", "Snapshot all owned live tabs."),
            ("x", "Save successfully, then close all live tabs."),
            ("r", "Rename session."),
            ("e / u", "Archive / unarchive."),
            ("Shift+D", "Remove inactive session to trash."),
        ),
    ),
    (
        "AUTOSAVE & PRIVACY",
        (
            ("watcher", "Saves commands, layout, and scrollback."),
            ("saved", "Up to 2,000 commands and text lines."),
            ("restore", "Only approved apps run automatically."),
        ),
    ),
)


@dataclass(slots=True)
class Palette:
    """Curses styles used consistently throughout the manager."""

    normal: int = 0
    selected: int = 0
    accent: int = 0
    good: int = 0
    muted: int = 0
    warning: int = 0


@dataclass(slots=True, frozen=True)
class SessionColumns:
    """Calculated table widths and offsets for one terminal size."""

    name_width: int
    tabs_offset: int
    panes_offset: int
    created_width: int
    modified_width: int
    created_offset: int
    modified_offset: int
    compact_dates: bool = False
    date_only: bool = False


@dataclass(slots=True, frozen=True)
class SessionEntry:
    """One selectable session row in the combined list."""

    view: SessionView
    selection_index: int


@dataclass(slots=True, frozen=True)
class TextEntry:
    """One non-selectable heading, spacer, or empty-state row."""

    kind: TextEntryKind
    text: str


@dataclass(slots=True, frozen=True)
class PreviewEntry:
    """One tab or pane-detail line beneath the selected session."""

    tab: TabPreview
    tab_index: int
    tab_count: int
    line: PreviewLineKind


ListEntry = SessionEntry | TextEntry | PreviewEntry


class SessionManager:
    """Render and control the Vim-keyed Workbench session manager."""

    def __init__(
        self,
        service: SessionOperations,
        *,
        on_dismiss: Callable[[], None] | None = None,
    ) -> None:
        """Initialize manager state around a session service."""
        self.service = service
        self.on_dismiss = on_dismiss
        self.rows: list[SessionView] = []
        self.active_rows: list[SessionView] = []
        self.archived_rows: list[SessionView] = []
        self.filtered: list[SessionView] = []
        self.selected = 0
        self.query = ""
        self.message = ""
        self.help_open = False
        self.help_scroll = 0
        self.palette = Palette()

    def run(self) -> int:
        """Run curses with an Escape delay suitable for Kitty key sequences."""
        curses.set_escdelay(ESCAPE_DELAY_MS)
        return curses.wrapper(self._main)

    def _main(self, screen: curses.window, /) -> int:
        """Process input until an action requests manager termination."""
        _set_cursor(0)
        screen.keypad(True)
        screen.timeout(-1)
        self.palette = _configure_palette()
        self._refresh()

        while True:
            self._draw(screen)
            key = screen.get_wch()
            try:
                result = self._handle_key(screen, key)
            except (OSError, RuntimeError, ValueError) as error:
                self.message = str(error)
                result = None
            if result is not None:
                return result

    def _refresh(self) -> None:
        """Reload rows while retaining the selected session when possible."""
        selected_id = None
        if self.filtered and 0 <= self.selected < len(self.filtered):
            selected_id = self.filtered[self.selected].stored.manifest.id
        self.rows = self.service.views()
        needle = self.query.casefold()
        matched = [
            row
            for row in self.rows
            if not needle
            or needle in row.stored.manifest.name.casefold()
            or needle in row.stored.manifest.project_root.casefold()
        ]
        self.active_rows = [row for row in matched if row.stored.manifest.status != "archived"]
        self.archived_rows = [row for row in matched if row.stored.manifest.status == "archived"]
        self.filtered = [*self.active_rows, *self.archived_rows]
        self.selected = 0
        if selected_id:
            for index, row in enumerate(self.filtered):
                if row.stored.manifest.id == selected_id:
                    self.selected = index
                    break
        if self.filtered:
            self.selected = min(self.selected, len(self.filtered) - 1)

    def _draw(self, screen: Screen) -> None:
        """Render the session table, footer, and optional help modal."""
        screen.erase()
        height, width = screen.getmaxyx()
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            _safe_addstr(
                screen,
                0,
                0,
                "kitty-workbench needs at least 48x8 cells",
                self.palette.warning,
            )
            screen.refresh()
            return

        title = " kitty workbench · sessions "
        _safe_addstr(screen, 0, 2, title, self.palette.accent | curses.A_BOLD)
        _safe_hline(screen, 1, 1, width - 2, self.palette.muted)

        query = f" / {self.query}" if self.query else ""
        session_heading = f"SESSIONS{query}  ·  {len(self.active_rows)} shown"
        _draw_session_heading(screen, 2, width, session_heading, self.palette)
        self._draw_entries(screen, height, width, self._list_entries())
        self._draw_footer(screen, height, width)
        if self.help_open:
            self._draw_help(screen)
        screen.refresh()

    def _list_entries(self) -> list[ListEntry]:
        """Build table rows and expand only the selected session's contents."""
        entries: list[ListEntry] = [
            SessionEntry(row, index) for index, row in enumerate(self.active_rows)
        ]
        if not self.filtered:
            empty = (
                "No matches. Keep typing or press Esc."
                if self.query
                else "No sessions. Press n to create one."
            )
            entries.append(TextEntry("empty", empty))
        entries.append(TextEntry("spacer", ""))
        entries.append(TextEntry("heading", f"ARCHIVED  ·  {len(self.archived_rows)} shown"))
        entries.extend(
            SessionEntry(row, len(self.active_rows) + index)
            for index, row in enumerate(self.archived_rows)
        )
        selected_match = next(
            (
                (index, entry)
                for index, entry in enumerate(entries)
                if isinstance(entry, SessionEntry) and entry.selection_index == self.selected
            ),
            None,
        )
        if selected_match is None:
            return entries
        selected_entry, selected_session = selected_match
        preview = build_session_preview(selected_session.view)
        details: list[ListEntry] = []
        for tab_index, tab in enumerate(preview.tabs):
            details.extend(
                (
                    PreviewEntry(tab, tab_index, len(preview.tabs), "tab"),
                    PreviewEntry(tab, tab_index, len(preview.tabs), "panes"),
                )
            )
        if not details:
            details.append(TextEntry("preview_empty", "└─ no tabs captured"))
        entries[selected_entry + 1 : selected_entry + 1] = details
        return entries

    def _draw_entries(
        self,
        screen: Screen,
        height: int,
        width: int,
        entries: list[ListEntry],
    ) -> None:
        """Render the visible slice of session and structural rows."""
        content_height = max(1, height - 7)
        selected_entry = next(
            (
                index
                for index, entry in enumerate(entries)
                if isinstance(entry, SessionEntry) and entry.selection_index == self.selected
            ),
            0,
        )
        preview_count = 0
        for entry in entries[selected_entry + 1 :]:
            if isinstance(entry, PreviewEntry) or (
                isinstance(entry, TextEntry) and entry.kind == "preview_empty"
            ):
                preview_count += 1
                continue
            break
        visible_preview = min(preview_count, max(0, content_height - 1))
        start = max(0, selected_entry + 1 + visible_preview - content_height)
        for offset, entry in enumerate(entries[start : start + content_height]):
            y = 3 + offset
            if isinstance(entry, SessionEntry):
                self._draw_session(
                    screen,
                    y,
                    width,
                    entry.view,
                    entry.selection_index,
                )
            elif isinstance(entry, PreviewEntry):
                self._draw_preview(screen, y, width, entry)
            elif entry.kind == "heading":
                _safe_addstr(
                    screen,
                    y,
                    2,
                    entry.text[: width - 4],
                    self.palette.muted | curses.A_BOLD,
                )
            elif entry.kind in {"empty", "preview_empty"}:
                _safe_addstr(screen, y, 4, entry.text[: width - 8], self.palette.muted)

    def _draw_footer(self, screen: Screen, height: int, width: int) -> None:
        """Render the selected path, contextual keys, and current message."""
        footer_y = height - 3
        selected = self._selected()
        if selected is not None:
            detail = _compact_path(selected.stored.manifest.project_root)
            _safe_addstr(screen, footer_y - 1, 2, detail[: width - 4], self.palette.muted)
        _safe_hline(screen, footer_y, 1, width - 2, self.palette.muted)
        if selected is not None and selected.stored.manifest.status == "archived":
            navigation: Hints = (
                ("j/k", "move"),
                ("g/G", "ends"),
                ("l/↵/Space", "open"),
                ("/", "search"),
                ("n", "new"),
                ("u", "unarchive"),
            )
        elif selected is not None and selected.live:
            navigation = (
                ("j/k", "move"),
                ("g/G", "ends"),
                ("l/↵/Space", "focus"),
                ("/", "search"),
                ("s", "save"),
                ("x", "save+close"),
            )
        else:
            navigation = (
                ("j/k", "move"),
                ("g/G", "ends"),
                ("l/↵/Space", "open"),
                ("/", "search"),
                ("n", "new"),
            )
        _draw_hints(screen, footer_y + 1, width, navigation, self.palette)
        if self.message:
            _safe_addstr(screen, footer_y + 2, 2, self.message[: width - 4], self.palette.warning)
            return
        if selected is not None and selected.stored.manifest.status == "archived":
            actions: Hints = (
                ("r", "rename"),
                ("Shift+D", "remove"),
                ("?", "help"),
                ("q", self._dismiss_label()),
            )
        elif selected is not None and selected.live:
            actions = (
                ("a", "add tab"),
                ("d", "detach tab"),
                ("r", "rename"),
                ("?", "help"),
                ("q", self._dismiss_label()),
            )
        else:
            actions = (
                ("c", "copy tab"),
                ("r", "rename"),
                ("e", "archive"),
                ("Shift+D", "remove"),
                ("?", "help"),
                ("q", self._dismiss_label()),
            )
        _draw_hints(screen, footer_y + 2, width, actions, self.palette)

    def _help_layout(
        self,
        screen: Screen,
    ) -> tuple[int, int, int, int, list[str], int]:
        """Size a complete help frame above the persistent manager footer."""
        height, width = screen.getmaxyx()
        box_width = max(4, min(78, width - 4, max(40, int(width * 0.78))))
        lines = _help_lines(max(1, box_width - 4), panel=self.on_dismiss is not None)
        footer_y = height - 3
        region_top = 0 if height < HELP_CENTER_HEIGHT else 1
        region_height = max(4, footer_y - region_top)
        preferred_height = max(5, int(height * 0.65))
        box_height = max(4, min(region_height, len(lines) + 3, preferred_height))
        top = region_top + max(0, (region_height - box_height) // 2)
        left = max(0, (width - box_width) // 2)
        body_capacity = max(1, box_height - 4)
        return top, left, box_height, box_width, lines, body_capacity

    def _draw_help(self, screen: Screen) -> None:
        """Render a scrolling, fully occluding help modal."""
        top, left, box_height, box_width, lines, body_capacity = self._help_layout(screen)
        bottom = top + box_height - 1
        max_scroll = max(0, len(lines) - body_capacity)
        self.help_scroll = min(max(0, self.help_scroll), max_scroll)

        _safe_addstr(screen, top, left, "╭", self.palette.muted)
        _safe_hline(screen, top, left + 1, box_width - 2, self.palette.muted)
        _safe_addstr(screen, top, left + box_width - 1, "╮", self.palette.muted)
        for y in range(top + 1, bottom):
            _safe_addstr(screen, y, left + 1, " " * max(0, box_width - 2), self.palette.normal)
        _safe_vline(screen, top + 1, left, box_height - 2, self.palette.normal)
        _safe_vline(screen, top + 1, left + box_width - 1, box_height - 2, self.palette.normal)
        _safe_addstr(screen, bottom, left, "╰", self.palette.muted)
        _safe_hline(screen, bottom, left + 1, box_width - 2, self.palette.muted)
        _safe_addstr(screen, bottom, left + box_width - 1, "╯", self.palette.muted)

        end = min(len(lines), self.help_scroll + body_capacity)
        title = f"Help · {self.help_scroll + 1}-{end}/{len(lines)}"
        _safe_addstr(
            screen,
            top + 1,
            left + 2,
            title[: max(0, box_width - 4)],
            self.palette.accent | curses.A_BOLD,
        )
        for offset, line in enumerate(lines[self.help_scroll : end]):
            style = (
                self.palette.accent | curses.A_BOLD
                if line and not line.startswith(" ")
                else self.palette.normal
            )
            _safe_addstr(screen, top + 2 + offset, left + 2, line[: max(0, box_width - 4)], style)

        final_action = "Q quit panel" if self.on_dismiss is not None else "Q close manager"
        footer = f"j/k scroll · g/G ends · q/Esc/? back · {final_action}"
        _safe_addstr(
            screen,
            bottom - 1,
            left + 2,
            footer[: max(0, box_width - 4)],
            self.palette.muted,
        )

    def _draw_preview(
        self,
        screen: Screen,
        y: int,
        width: int,
        entry: PreviewEntry,
    ) -> None:
        """Render one themed tree line for a selected session tab or its panes."""
        tab = entry.tab
        last_tab = entry.tab_index == entry.tab_count - 1
        connector = "└─ " if last_tab else "├─ "
        continuation = "   " if last_tab else "│  "
        left = 4
        right = max(left, width - 2)
        available = max(0, right - left)
        if entry.line == "tab":
            pane_count = len(tab.panes)
            pane_label = "pane" if pane_count == 1 else "panes"
            metadata = (
                " · snapshot" if not tab.details_available else f" · {pane_count} {pane_label}"
            )
            if tab.layout:
                metadata += f" · {tab.layout}"
            if tab.focused:
                metadata += " · focused"
            title_width = max(1, available - len(connector) - len(metadata))
            title = _ellipsize(tab.title, title_width)
            _safe_addstr(screen, y, left, connector, self.palette.muted)
            _safe_addstr(
                screen,
                y,
                left + len(connector),
                title,
                self.palette.accent | curses.A_BOLD,
            )
            metadata_x = left + len(connector) + len(title)
            _safe_addstr(
                screen,
                y,
                metadata_x,
                metadata[: max(0, right - metadata_x)],
                self.palette.muted,
            )
            return

        _safe_addstr(screen, y, left, continuation, self.palette.muted)
        pane_x = left + len(continuation)
        if not tab.details_available:
            _safe_addstr(
                screen,
                y,
                pane_x,
                "pane details unavailable"[: max(0, right - pane_x)],
                self.palette.muted,
            )
            return
        if not tab.panes:
            _safe_addstr(
                screen,
                y,
                pane_x,
                "empty tab"[: max(0, right - pane_x)],
                self.palette.muted,
            )
            return

        labels = [_pane_preview_label(pane) for pane in tab.panes]
        for index, (pane, label) in enumerate(zip(tab.panes, labels, strict=True)):
            if index:
                separator = ", "[: max(0, right - pane_x)]
                _safe_addstr(screen, y, pane_x, separator, self.palette.muted)
                pane_x += len(separator)
            remaining = len(labels) - index
            separators = max(0, remaining - 1) * 2
            label_width = max(1, (max(0, right - pane_x - separators)) // remaining)
            rendered = _ellipsize(label, label_width)
            if pane.needs_attention:
                style = self.palette.warning
            elif pane.agent == "claude":
                style = self.palette.accent | curses.A_BOLD
            elif pane.agent == "codex" or pane.active:
                style = self.palette.good | curses.A_BOLD
            else:
                style = self.palette.normal
            _safe_addstr(screen, y, pane_x, rendered, style)
            pane_x += len(rendered)

    def _draw_session(
        self,
        screen: Screen,
        y: int,
        width: int,
        view: SessionView,
        selection_index: int | None,
    ) -> None:
        """Render one session with state, counts, and timestamp columns."""
        manifest = view.stored.manifest
        if view.live:
            symbol, status_style = "●", self.palette.good
        elif manifest.status == "archived":
            symbol, status_style = "○", self.palette.muted
        else:
            symbol, status_style = "○", self.palette.accent
        tabs = len(view.live_tabs) if view.live else manifest.summary.tab_count
        panes = (
            sum(len(tab.windows) for tab in view.live_tabs)
            if view.live
            else manifest.summary.pane_count
        )
        selected = selection_index == self.selected
        style = self.palette.selected if selected else self.palette.normal
        prefix = "›" if selected else " "
        columns = _session_columns(width)
        prefix_text = f"{prefix} {symbol} "
        created = _format_row_time(
            manifest.created_at,
            compact=columns.compact_dates,
            date_only=columns.date_only,
        )
        modified = _format_row_time(
            manifest.updated_at,
            include_time=not columns.date_only,
            compact=columns.compact_dates,
            date_only=columns.date_only,
        )
        dates = f"  {created:<{columns.created_width}}  {modified:<{columns.modified_width}}"
        counts = f"  {tabs:>4}  {panes:>5}"
        row = (
            f"{prefix_text}{manifest.name[: columns.name_width]:<{columns.name_width}}"
            f"{counts}{dates}"
        )
        _safe_addstr(screen, y, 2, row[: width - 4], style)
        if not selected:
            _safe_addstr(screen, y, 4, symbol, status_style)

    def _handle_key(self, screen: Screen, key: object) -> int | None:
        """Route one key through modal, global, navigation, and row actions."""
        self.message = ""
        if self.help_open:
            return self._handle_help_key(screen, key)
        handled, result = self._handle_global_key(screen, key)
        if handled:
            return result
        if self._handle_navigation_key(key):
            return None
        current = self._selected()
        return None if current is None else self._handle_selected_key(screen, key, current)

    def _handle_global_key(self, screen: Screen, key: object) -> tuple[bool, int | None]:
        """Handle keys that do not depend on a selected session."""
        result: int | None = None
        if key == "\x07":
            self._refresh()
        elif key == "Q":
            result = 0
        elif key in ("q", "h", "\x1b"):
            result = self._dismiss()
        elif key == "/":
            self._prompt(screen, "search", self.query, on_change=self._update_query)
        elif key == "?":
            self.help_open = True
            self.help_scroll = 0
        elif key == "n":
            self._create_session(screen)
        else:
            return False, None
        return True, result

    def _create_session(self, screen: Screen) -> None:
        """Prompt for and create a non-empty session name."""
        name = self._prompt(screen, "new session")
        if name:
            created = self.service.create_from_active(name)
            self.message = f"created {created.manifest.name}"
            self._refresh()

    def _handle_navigation_key(self, key: object) -> bool:
        """Move selection for Vim, arrow, and half-page navigation keys."""
        handled = True
        if key in ("j", curses.KEY_DOWN):
            if self.filtered:
                self.selected = (self.selected + 1) % len(self.filtered)
        elif key in ("k", curses.KEY_UP):
            if self.filtered:
                self.selected = (self.selected - 1) % len(self.filtered)
        elif key == "g":
            self.selected = 0
        elif key == "G":
            self.selected = max(0, len(self.filtered) - 1)
        elif key == "\x04":
            self.selected = min(
                max(0, len(self.filtered) - 1),
                self.selected + HALF_PAGE_ROWS,
            )
        elif key == "\x15":
            self.selected = max(0, self.selected - HALF_PAGE_ROWS)
        else:
            handled = False
        return handled

    def _handle_selected_key(
        self,
        screen: Screen,
        key: object,
        current: SessionView,
    ) -> int | None:
        """Handle open, membership, lifecycle, and removal for one row."""
        identifier = current.stored.manifest.id
        if key == "u":
            if current.stored.manifest.status != "archived":
                self.message = "select a session in the archived list to unarchive it"
                return None
            unarchived = self.service.unarchive(identifier)
            self.message = f"unarchived {unarchived.manifest.name}"
            self._refresh()
            return None
        if key in ("l", " ", "\n", "\r", curses.KEY_ENTER):
            return self._open_selected(screen, current)
        if key == "x":
            if not current.live:
                self.message = "only a live session can be saved and closed"
                return None
            name = current.stored.manifest.name
            if self._confirm(screen, f"save and close {name}?"):
                closed = self.service.save_and_close(identifier)
                self._refresh()
                self.message = f"saved and closed {closed.manifest.name}"
            return None
        if key == "d" and not current.live:
            self.message = "remove uses Shift+D; detach is only for live sessions"
            return None
        if key == "D" and current.live:
            self.message = "press x to save and close this live session before removing it"
            return None
        actions: dict[str, tuple[Callable[[str], StoredSession], str]] = {
            "a": (self.service.add_current_tab, "added source tab to {name}"),
            "d": (self.service.detach_current_tab, "detached source tab from {name}"),
            "c": (self.service.copy_current_tab, "copied safe tab layout into {name}"),
            "s": (self.service.save, "saved {name}"),
            "e": (self.service.archive, "archived {name}"),
        }
        action = actions.get(key) if isinstance(key, str) else None
        if action is not None:
            operation, message = action
            stored = operation(identifier)
            self.message = message.format(name=stored.manifest.name)
            self._refresh()
            return None
        if key == "r":
            name = self._prompt(screen, "rename", current.stored.manifest.name)
            if name:
                renamed = self.service.rename(identifier, name)
                self.message = f"renamed to {renamed.manifest.name}"
                self._refresh()
            return None
        if key == "D":
            name = current.stored.manifest.name
            if self._confirm(screen, f"remove {name} to recoverable trash?"):
                self.service.remove(identifier)
                self._refresh()
                self.message = f"removed {name} to recoverable trash"
        return None

    def _open_selected(self, screen: Screen, current: SessionView) -> int | None:
        """Resolve unowned tabs before focusing or restoring a selected row."""
        unowned_action: UnownedTabsAction | None = None
        count = self.service.unowned_tab_count()
        if count:
            unowned_action = self._choose_unowned_tabs(
                screen,
                count,
                current.stored.manifest.name,
            )
            if unowned_action is None:
                self.message = "open cancelled; tabs unchanged"
                return None
        self.service.open(current.stored.manifest.id, unowned_action)
        if self.on_dismiss is None:
            return self._dismiss()
        self._refresh()
        return None

    def _choose_unowned_tabs(
        self,
        screen: Screen,
        count: int,
        target_name: str,
    ) -> UnownedTabsAction | None:
        """Render and read the attach, preserve, or cancel opening decision."""
        height, width = screen.getmaxyx()
        box_width = max(4, min(72, width - 2))
        box_height = min(6, height)
        top = max(0, (height - box_height) // 2)
        left = max(0, (width - box_width) // 2)
        bottom = top + box_height - 1
        inner_width = max(0, box_width - 4)
        lines = (
            ("", f"Unowned tabs · {count}", self.palette.accent | curses.A_BOLD),
            ("a", f"attach to {target_name}", self.palette.normal),
            ("s", "save separately, then open", self.palette.normal),
            ("q / Esc", "cancel; change nothing", self.palette.muted),
        )

        _safe_addstr(screen, top, left, "╭", self.palette.muted)
        _safe_hline(screen, top, left + 1, box_width - 2, self.palette.muted)
        _safe_addstr(screen, top, left + box_width - 1, "╮", self.palette.muted)
        for y in range(top + 1, bottom):
            _safe_addstr(screen, y, left + 1, " " * max(0, box_width - 2), self.palette.normal)
        _safe_vline(screen, top + 1, left, box_height - 2, self.palette.normal)
        _safe_vline(screen, top + 1, left + box_width - 1, box_height - 2, self.palette.normal)
        _safe_addstr(screen, bottom, left, "╰", self.palette.muted)
        _safe_hline(screen, bottom, left + 1, box_width - 2, self.palette.muted)
        _safe_addstr(screen, bottom, left + box_width - 1, "╯", self.palette.muted)

        for offset, (label, description, style) in enumerate(lines):
            y = top + 1 + offset
            if label:
                prefix = f"{label:<8}"
                _safe_addstr(screen, y, left + 2, prefix[:inner_width], self.palette.accent)
                available = max(0, inner_width - len(prefix))
                _safe_addstr(
                    screen,
                    y,
                    left + 2 + len(prefix),
                    description[:available],
                    style,
                )
            else:
                _safe_addstr(screen, y, left + 2, description[:inner_width], style)
        screen.refresh()

        while True:
            pressed = screen.get_wch()
            if pressed in ("a", "A"):
                return UnownedTabsAction.ATTACH
            if pressed in ("s", "S"):
                return UnownedTabsAction.SAVE_SEPARATELY
            if pressed in ("q", "Q", "\x1b"):
                return None

    def _dismiss(self) -> int | None:
        """Close a standalone manager or hide and refresh a resident panel."""
        if self.on_dismiss is None:
            return 0
        self.on_dismiss()
        self._refresh()
        return None

    def _dismiss_label(self) -> str:
        """Return the footer verb for the active manager presentation."""
        return "hide" if self.on_dismiss is not None else "close"

    def _handle_help_key(self, screen: Screen, key: object) -> int | None:
        """Navigate or close the modal help document."""
        if key == "Q":
            return 0
        if key in ("q", "h", "?", "\x1b"):
            self.help_open = False
            return None
        _, _, _, _, lines, body_capacity = self._help_layout(screen)
        maximum = max(0, len(lines) - body_capacity)
        if key in ("j", curses.KEY_DOWN):
            self.help_scroll = min(maximum, self.help_scroll + 1)
        elif key in ("k", curses.KEY_UP):
            self.help_scroll = max(0, self.help_scroll - 1)
        elif key == "\x04":
            self.help_scroll = min(maximum, self.help_scroll + max(1, body_capacity - 1))
        elif key == "\x15":
            self.help_scroll = max(0, self.help_scroll - max(1, body_capacity - 1))
        elif key == "g":
            self.help_scroll = 0
        elif key == "G":
            self.help_scroll = maximum
        return None

    def _selected(self) -> SessionView | None:
        """Return the selected row, or no row for an empty result set."""
        if not self.filtered:
            return None
        return self.filtered[self.selected]

    def _update_query(self, value: str) -> None:
        """Apply an incremental query and immediately refresh matches."""
        self.query = value
        self._refresh()

    def _confirm(self, screen: Screen, label: str) -> bool:
        """Read a conservative yes-or-no confirmation from the bottom row."""
        height, width = screen.getmaxyx()
        prompt = f"{label} [y/N]"
        screen.move(height - 1, 0)
        screen.clrtoeol()
        _safe_addstr(screen, height - 1, 2, prompt[: max(0, width - 4)], self.palette.warning)
        screen.refresh()
        return screen.get_wch() in ("y", "Y")

    def _prompt(
        self,
        screen: Screen,
        label: str,
        initial: str = "",
        *,
        on_change: Callable[[str], None] | None = None,
    ) -> str:
        """Edit one printable value with optional incremental change delivery."""
        height, width = screen.getmaxyx()
        prompt = f"{label}> "
        value = list(initial)
        _set_cursor(1)
        try:
            while True:
                if on_change is not None:
                    self._draw(screen)
                screen.move(height - 1, 0)
                screen.clrtoeol()
                rendered = prompt + "".join(value)
                _safe_addstr(screen, height - 1, 2, rendered[-(width - 4) :], self.palette.accent)
                screen.refresh()
                key = screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    result = "".join(value).strip()
                    if on_change is not None:
                        on_change(result)
                    return result
                if key == "\x1b":
                    if on_change is not None:
                        on_change(initial)
                        return initial
                    return ""
                changed = _edit_prompt_value(value, key)
                if changed and on_change is not None:
                    on_change("".join(value))
        finally:
            _set_cursor(0)


def _edit_prompt_value(value: list[str], key: object) -> bool:
    """Apply one backspace or printable key and report whether text changed."""
    if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
        if not value:
            return False
        value.pop()
        return True
    if isinstance(key, str) and key.isprintable():
        value.append(key)
        return True
    return False


def _pane_preview_label(pane: PanePreview) -> str:
    """Format one pane with agent, focus, command, and restore indicators."""
    agent_labels = {"claude": "✻ Claude", "codex": "◇ Codex"}
    label = agent_labels.get(pane.agent or "", pane.agent.title() if pane.agent else pane.program)
    if pane.active:
        label = f"• {label}"
    if pane.restore_available:
        label += " ↻"
    if pane.last_command and is_shell_program(pane.program):
        label += f" · last: {pane.last_command}"
    if pane.needs_attention:
        label += " !"
    return label


def _ellipsize(value: str, width: int) -> str:
    """Fit text to a positive cell budget while keeping truncation visible."""
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def _configure_palette() -> Palette:
    """Derive a readable palette from the terminal's active background."""
    if not curses.has_colors():
        return Palette(
            selected=curses.A_REVERSE,
            accent=curses.A_BOLD,
            good=curses.A_BOLD,
            muted=curses.A_DIM,
        )
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        background = curses.COLOR_BLACK
    curses.init_pair(1, -1 if background == -1 else curses.COLOR_WHITE, background)
    curses.init_pair(3, curses.COLOR_CYAN, background)
    curses.init_pair(4, curses.COLOR_GREEN, background)
    curses.init_pair(6, curses.COLOR_YELLOW, background)
    normal = curses.color_pair(1)
    return Palette(
        normal=normal,
        selected=normal | curses.A_REVERSE | curses.A_BOLD,
        accent=curses.color_pair(3),
        good=curses.color_pair(4),
        muted=normal | curses.A_DIM,
        warning=curses.color_pair(6),
    )


def _compact_path(path: str) -> str:
    """Replace an absolute home prefix with a compact tilde."""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def _format_row_time(
    value: str,
    *,
    include_time: bool = False,
    compact: bool = False,
    date_only: bool = False,
) -> str:
    """Format one stored ISO timestamp for the selected table density."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return "unknown"
    if date_only:
        return parsed.strftime("%m-%d")
    if compact:
        return parsed.strftime("%y-%m-%d %H:%M" if include_time else "%y-%m-%d")
    return parsed.strftime("%Y-%m-%d %H:%M" if include_time else "%Y-%m-%d")


def _session_columns(width: int) -> SessionColumns:
    """Calculate aligned session columns for wide, compact, or narrow panels."""
    if width >= FULL_TABLE_WIDTH:
        created_width, modified_width = 10, 16
        compact_dates, date_only = False, False
    elif width >= COMPACT_TABLE_WIDTH:
        created_width, modified_width = 8, 14
        compact_dates, date_only = True, False
    else:
        created_width, modified_width = 7, 8
        compact_dates, date_only = False, True
    usable = max(1, width - 4)
    fixed_width = 21 + created_width + modified_width
    name_width = max(8, min(28, usable - fixed_width))
    tabs_offset = 6 + name_width
    panes_offset = tabs_offset + 6
    created_offset = panes_offset + 7
    return SessionColumns(
        name_width=name_width,
        tabs_offset=tabs_offset,
        panes_offset=panes_offset,
        created_width=created_width,
        modified_width=modified_width,
        created_offset=created_offset,
        modified_offset=created_offset + created_width + 2,
        compact_dates=compact_dates,
        date_only=date_only,
    )


def _draw_session_heading(
    screen: Screen,
    y: int,
    width: int,
    heading: str,
    palette: Palette,
) -> None:
    """Render table labels at the same offsets used by session rows."""
    columns = _session_columns(width)
    available = max(8, columns.tabs_offset - 2)
    left_heading = heading if len(heading) <= available else "SESSIONS"
    _safe_addstr(screen, y, 2, left_heading[:available], palette.muted | curses.A_BOLD)
    _safe_addstr(
        screen,
        y,
        2 + columns.tabs_offset,
        "TABS",
        palette.muted | curses.A_BOLD,
    )
    _safe_addstr(
        screen,
        y,
        2 + columns.panes_offset,
        "PANES",
        palette.muted | curses.A_BOLD,
    )
    _safe_addstr(
        screen,
        y,
        2 + columns.created_offset,
        "CREATED"[: columns.created_width],
        palette.muted | curses.A_BOLD,
    )
    _safe_addstr(
        screen,
        y,
        2 + columns.modified_offset,
        "MODIFIED"[: columns.modified_width],
        palette.muted | curses.A_BOLD,
    )


def _help_lines(width: int, *, panel: bool = False) -> list[str]:
    """Wrap all help sections into modal-width display lines."""
    lines: list[str] = []
    key_width = min(15, max(8, width // 4))
    description_width = max(10, width - key_width)
    for section_index, (section, entries) in enumerate(_HELP_SECTIONS):
        if section_index:
            lines.append("")
        lines.append(section)
        for key, description in entries:
            if panel and key == "q / Esc / h":
                description = "Hide panel; Q terminates it."
            wrapped = textwrap.wrap(
                description,
                width=description_width,
                break_long_words=False,
                break_on_hyphens=False,
            ) or [""]
            prefix = f" {key:<{key_width - 1}}"
            lines.append(prefix + wrapped[0])
            continuation = " " * key_width
            lines.extend(continuation + part for part in wrapped[1:])
    return lines


def _draw_hints(
    screen: Screen,
    y: int,
    width: int,
    hints: Hints,
    palette: Palette,
) -> None:
    """Draw theme-derived key capsules, stopping cleanly at narrow widths."""
    x = 2
    for key, label in hints:
        segment_width = len(key) + len(label) + 4
        if x + segment_width > width - 1:
            break
        _safe_addstr(screen, y, x, f" {key} ", palette.selected)
        x += len(key) + 2
        _safe_addstr(screen, y, x, f" {label}", palette.muted)
        x += len(label) + 2


def _safe_addstr(screen: Screen, y: int, x: int, text: str, style: int = 0) -> None:
    """Render clipped content without failing on a terminal boundary race."""
    with suppress(curses.error):
        screen.addstr(y, x, text, style)


def _safe_hline(screen: Screen, y: int, x: int, length: int, style: int = 0) -> None:
    """Render a native continuous rule with a Unicode fallback."""
    if length <= 0:
        return
    glyph = getattr(curses, "ACS_HLINE", "─")
    try:
        screen.hline(y, x, glyph, length)
    except (AttributeError, curses.error):
        _safe_addstr(screen, y, x, "─" * length, style)


def _safe_vline(screen: Screen, y: int, x: int, length: int, style: int = 0) -> None:
    """Render a native vertical rule with a cell-by-cell fallback."""
    if length <= 0:
        return
    glyph = getattr(curses, "ACS_VLINE", "│")
    try:
        screen.vline(y, x, glyph, length)
    except (AttributeError, curses.error):
        for offset in range(length):
            _safe_addstr(screen, y + offset, x, "│", style)


def _set_cursor(visibility: int) -> None:
    """Set cursor visibility when supported by the active terminal."""
    with suppress(curses.error):
        curses.curs_set(visibility)
