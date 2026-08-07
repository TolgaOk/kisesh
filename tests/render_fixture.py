"""Typed terminal and service fixtures for deterministic TUI rendering."""

from __future__ import annotations

import curses
from pathlib import Path

from kisesh.kitty_client import LiveTab
from kisesh.model import (
    KittyWindow,
    PaneContext,
    RestoreKind,
    RestoreSpec,
    SessionContext,
    SessionManifest,
    SessionStatus,
    SnapshotSummary,
    TabContext,
    slugify,
)
from kisesh.service import SessionView, UnownedTabsDecision, UnownedTabsInfo
from kisesh.store import StoredSession
from kisesh.tui import CursesGlyph, Palette, SessionManager


class Canvas:
    """Implement the manager's curses screen contract with strict cell bounds."""

    def __init__(self, height: int, width: int) -> None:
        """Allocate a fixed terminal canvas and input state."""
        self.height = height
        self.width = width
        self.cells: list[list[str]] = []
        self.styles: list[list[int]] = []
        self.cursor = (0, 0)
        self.keypad_enabled = False
        self.timeout_ms = -1
        self.erase()

    def erase(self) -> None:
        """Clear every character and style cell."""
        self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]
        self.styles = [[0 for _ in range(self.width)] for _ in range(self.height)]

    def getmaxyx(self) -> tuple[int, int]:
        """Return the fixed terminal dimensions."""
        return self.height, self.width

    def addstr(self, y: int, x: int, value: str, style: int = 0) -> None:
        """Write styled text and fail when rendering exceeds the canvas."""
        if y < 0 or y >= self.height or x < 0 or x + len(value) > self.width:
            raise curses.error("render exceeded terminal canvas")
        for offset, character in enumerate(value):
            self.cells[y][x + offset] = character
            self.styles[y][x + offset] = style

    @staticmethod
    def _glyph(value: CursesGlyph) -> str:
        """Convert ncurses alternate-character constants to Unicode glyphs."""
        if isinstance(value, str):
            return value[0]
        if isinstance(value, bytes):
            return value[:1].decode(errors="replace")
        fallback = chr(value & 0xFF)
        return {"q": "─", "x": "│"}.get(fallback, fallback)

    def addch(self, y: int, x: int, value: CursesGlyph, style: int = 0) -> None:
        """Write one curses-compatible glyph."""
        self.addstr(y, x, self._glyph(value), style)

    def hline(self, y: int, x: int, value: CursesGlyph, length: int) -> None:
        """Write one continuous horizontal rule."""
        self.addstr(y, x, self._glyph(value) * length)

    def vline(self, y: int, x: int, value: CursesGlyph, length: int) -> None:
        """Write one continuous vertical rule."""
        glyph = self._glyph(value)
        for offset in range(length):
            self.addstr(y + offset, x, glyph)

    def refresh(self) -> None:
        """Accept a curses refresh without external side effects."""

    def keypad(self, enabled: bool) -> None:
        """Record whether special-key decoding was requested."""
        self.keypad_enabled = enabled

    def timeout(self, delay: int) -> None:
        """Record the requested input timeout."""
        self.timeout_ms = delay

    def get_wch(self) -> object:
        """Reject unexpected input reads on a rendering-only canvas."""
        raise AssertionError("the rendering canvas has no queued input")

    def move(self, y: int, x: int) -> None:
        """Move the simulated cursor."""
        self.cursor = (y, x)

    def clrtoeol(self) -> None:
        """Clear cells from the simulated cursor to the line end."""
        y, x = self.cursor
        for column in range(x, self.width):
            self.cells[y][column] = " "
            self.styles[y][column] = 0

    def render(self) -> str:
        """Return the visible canvas with trailing spaces and rows removed."""
        lines = ["".join(row).rstrip() for row in self.cells]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines) + "\n"


class StaticService:
    """Implement all manager operations over deterministic in-memory rows."""

    def __init__(self) -> None:
        """Start with the representative live, saved, and archived fixtures."""
        self._rows = sample_views()
        self.unowned_count = 0
        self.unowned_suggested_name = "Amber Badger"
        self.unowned_creations: list[tuple[str, UnownedTabsDecision]] = []

    def views(self) -> list[SessionView]:
        """Return an isolated list of current rows."""
        return list(self._rows)

    def _stored(self, identifier: str) -> StoredSession:
        """Resolve one fixture session by stable ID."""
        return next(row.stored for row in self._rows if row.stored.manifest.id == identifier)

    def create_from_active(
        self,
        name: str,
        project_root: str | None = None,
    ) -> StoredSession:
        """Append a saved one-pane fixture session."""
        view = _view(
            name,
            slugify(name),
            project_root or "/tmp/project",
            tabs=1,
            panes=1,
        )
        self._rows.append(view)
        return view.stored

    def create_from_unowned(
        self,
        name: str,
        decision: UnownedTabsDecision,
        project_root: str | None = None,
    ) -> StoredSession:
        """Append a fixture session after accepting an explicit tab decision."""
        self.unowned_creations.append((name, decision))
        return self.create_from_active(name, project_root)

    def unarchive(self, slug_or_id: str) -> StoredSession:
        """Mark one fixture session active."""
        stored = self._stored(slug_or_id)
        stored.manifest.status = "active"
        return stored

    def unowned_tabs_info(self) -> UnownedTabsInfo | None:
        """Return configured source-tab count and editable name suggestion."""
        if not self.unowned_count:
            return None
        return UnownedTabsInfo(self.unowned_count, self.unowned_suggested_name)

    def open(
        self,
        slug_or_id: str,
        unowned_decision: UnownedTabsDecision | None = None,
    ) -> StoredSession:
        """Resolve the session that would be focused or restored."""
        del unowned_decision
        return self._stored(slug_or_id)

    def add_current_tab(self, slug_or_id: str) -> StoredSession:
        """Resolve the target that would receive the source tab."""
        return self._stored(slug_or_id)

    def detach_current_tab(self, slug_or_id: str) -> StoredSession:
        """Resolve the session that would release the source tab."""
        return self._stored(slug_or_id)

    def copy_current_tab(self, slug_or_id: str) -> StoredSession:
        """Resolve the target that would receive a safe tab copy."""
        return self._stored(slug_or_id)

    def save(self, slug_or_id: str) -> StoredSession:
        """Resolve the session that would be saved."""
        return self._stored(slug_or_id)

    def save_and_close(self, slug_or_id: str) -> StoredSession:
        """Save one fixture session and remove its live tab markers."""
        stored = self._stored(slug_or_id)
        row = next(row for row in self._rows if row.stored.manifest.id == slug_or_id)
        row.live_tabs.clear()
        return stored

    def rename(self, slug_or_id: str, new_name: str) -> StoredSession:
        """Rename one fixture session in place."""
        stored = self._stored(slug_or_id)
        stored.manifest.name = new_name
        return stored

    def rename_tab(self, slug_or_id: str, tab_index: int, new_title: str) -> StoredSession:
        """Rename one live or persisted fixture tab by preview index."""
        stored = self._stored(slug_or_id)
        row = next(view for view in self._rows if view.stored.manifest.id == stored.manifest.id)
        if row.live_tabs:
            row.live_tabs[tab_index].title = new_title
            titles = [tab.title for tab in row.live_tabs]
        elif row.context is not None:
            row.context["tabs"][tab_index]["title"] = new_title
            titles = [tab["title"] for tab in row.context["tabs"]]
        else:
            titles = list(stored.manifest.summary.tab_titles)
            titles[tab_index] = new_title
        stored.manifest.summary.tab_titles = titles
        return stored

    def archive(self, slug_or_id: str) -> StoredSession:
        """Mark one fixture session archived."""
        stored = self._stored(slug_or_id)
        stored.manifest.status = "archived"
        return stored

    def remove(self, slug_or_id: str) -> Path:
        """Remove one fixture session and return its recoverable destination."""
        stored = self._stored(slug_or_id)
        self._rows = [row for row in self._rows if row.stored.manifest.id != slug_or_id]
        return Path("/tmp/trash") / stored.manifest.slug


def _view(
    name: str,
    slug: str,
    root: str,
    *,
    tabs: int,
    panes: int,
    status: SessionStatus = "active",
    live_tabs: list[LiveTab] | None = None,
    context: SessionContext | None = None,
) -> SessionView:
    """Build one display row with stable timestamps and summary counts."""
    manifest = SessionManifest(
        name=name,
        slug=slug,
        project_root=root,
        status=status,
        created_at="2026-07-18T09:15:00Z",
        updated_at="2026-08-04T11:30:00Z",
        last_used_at="2026-08-04T11:30:00Z",
        summary=SnapshotSummary(tab_count=tabs, pane_count=panes, tab_titles=[name]),
    )
    return SessionView(
        StoredSession(manifest, Path("/tmp") / slug),
        list(live_tabs or []),
        context,
    )


def _live_window(
    window_id: int,
    program: str,
    *,
    active: bool = False,
    last_command: str = "",
    needs_attention: bool = False,
) -> KittyWindow:
    """Build representative current Kitty pane metadata."""
    return {
        "id": window_id,
        "title": Path(program).name,
        "cwd": "/tmp/project",
        "foreground_processes": [
            {"cmdline": [program], "cwd": "/tmp/project", "pid": window_id + 1000}
        ],
        "is_active": active,
        "is_focused": active,
        "at_prompt": False,
        "last_reported_cmdline": last_command,
        "needs_attention": needs_attention,
    }


def _pane_context(
    window_id: int,
    program: str,
    *,
    agent: str | None = None,
    last_command: str | None = None,
    restorable: bool = False,
    focused_at: float | None = None,
) -> PaneContext:
    """Build a complete saved pane with practical restore and command state."""
    restore: RestoreSpec | None = None
    if restorable:
        kind: RestoreKind = "agent" if agent else "foreground"
        restore = {
            "argv": [program],
            "command": program,
            "kind": kind,
            "auto_run": True,
        }
    pane: PaneContext = {
        "window_id": window_id,
        "title": program,
        "cwd": "/tmp/project",
        "program": program,
        "agent": agent,
        "foreground_argv": [program],
        "foreground_command": program,
        "restore": restore,
        "at_prompt": program == "zsh",
        "alternate_screen": program != "zsh",
        "last_exit_status": 0,
        "needs_attention": False,
        "had_activity": False,
        "command_history": (
            [{"command": last_command, "completed_at": "2026-08-04T11:29:00Z"}]
            if last_command
            else []
        ),
        "last_command": last_command,
        "last_command_output": None,
        "last_command_output_truncated": False,
        "last_output_command": None,
        "terminal_history": None,
        "terminal_history_truncated": False,
        "alternate_screen_text": None,
        "alternate_screen_text_truncated": False,
    }
    if focused_at is not None:
        pane["last_focused_at"] = focused_at
    return pane


def _saved_context(tabs: list[TabContext]) -> SessionContext:
    """Build a coherent context around representative saved tabs."""
    panes = [pane for tab in tabs for pane in tab["panes"]]
    return {
        "schema_version": 1,
        "captured_at": "2026-08-04T11:30:00Z",
        "programs": sorted({pane["program"] for pane in panes if pane["program"]}),
        "agents": sorted({pane["agent"] for pane in panes if pane["agent"]}),
        "command_count": sum(len(pane["command_history"]) for pane in panes),
        "restore_commands": [],
        "tabs": tabs,
    }


def sample_views() -> list[SessionView]:
    """Return representative rows for layout and lifecycle rendering."""
    home = str(Path.home())
    live_tabs = [
        LiveTab(
            1,
            10,
            0,
            "agents",
            "splits",
            [
                _live_window(20, "/opt/homebrew/bin/claude", active=True),
                _live_window(21, "/opt/homebrew/bin/codex"),
            ],
            is_focused=True,
            is_active=True,
        ),
        LiveTab(
            1,
            11,
            1,
            "editor + tests",
            "tall",
            [
                _live_window(22, "nvim"),
                _live_window(23, "pytest", last_command="pytest -q"),
                _live_window(24, "zsh", last_command="git status"),
            ],
        ),
        LiveTab(
            1,
            12,
            2,
            "monitor",
            "stack",
            [_live_window(25, "top", needs_attention=True)],
        ),
    ]
    dotfiles_context = _saved_context(
        [
            {
                "title": "editor",
                "layout": "splits",
                "focused": True,
                "panes": [
                    _pane_context(
                        30,
                        "claude",
                        agent="claude",
                        last_command="claude --continue",
                        restorable=True,
                        focused_at=12.0,
                    ),
                    _pane_context(31, "nvim", restorable=True, focused_at=10.0),
                ],
            },
            {
                "title": "shell",
                "layout": "tall",
                "focused": False,
                "panes": [_pane_context(32, "zsh", last_command="git status")],
            },
        ]
    )
    vault_context = _saved_context(
        [
            {
                "title": "notes",
                "layout": "splits",
                "focused": False,
                "panes": [_pane_context(40, "nvim", restorable=True)],
            }
        ]
    )
    return [
        _view(
            "JAX Agents",
            "jax-agents",
            f"{home}/research/jaxtor",
            tabs=3,
            panes=6,
            live_tabs=live_tabs,
        ),
        _view(
            "Dotfiles",
            "dotfiles",
            f"{home}/dotfiles",
            tabs=2,
            panes=3,
            context=dotfiles_context,
        ),
        _view(
            "Main Vault",
            "main-vault",
            f"{home}/Library/Mobile Documents/Main",
            tabs=1,
            panes=1,
            status="archived",
            context=vault_context,
        ),
    ]


def rendered_manager(width: int = 100, height: int = 16) -> tuple[str, Canvas, Palette]:
    """Render the selected sample manager into a strict terminal canvas."""
    manager = SessionManager(StaticService())
    manager._refresh()
    manager.selected = 1
    palette = Palette(normal=1, selected=2, accent=3, good=4, muted=5, warning=6)
    manager.palette = palette
    canvas = Canvas(height, width)
    manager._draw(canvas)
    return canvas.render(), canvas, palette


if __name__ == "__main__":
    print(rendered_manager()[0], end="")
