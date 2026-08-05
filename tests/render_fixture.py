"""Typed terminal and service fixtures for deterministic TUI rendering."""

from __future__ import annotations

import curses
from pathlib import Path

from kitty_workbench.kitty_client import LiveTab
from kitty_workbench.model import SessionManifest, SessionStatus, SnapshotSummary, slugify
from kitty_workbench.service import SessionView
from kitty_workbench.store import StoredSession
from kitty_workbench.tui import CursesGlyph, Palette, SessionManager


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

    def unarchive(self, slug_or_id: str) -> StoredSession:
        """Mark one fixture session active."""
        stored = self._stored(slug_or_id)
        stored.manifest.status = "active"
        return stored

    def open(self, slug_or_id: str) -> StoredSession:
        """Resolve the session that would be focused or restored."""
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

    def rename(self, slug_or_id: str, new_name: str) -> StoredSession:
        """Rename one fixture session in place."""
        stored = self._stored(slug_or_id)
        stored.manifest.name = new_name
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
    live: bool = False,
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
    live_tabs = [LiveTab(1, 10, 0, name, "splits", [{"id": 20}])] if live else []
    return SessionView(StoredSession(manifest, Path("/tmp") / slug), live_tabs)


def sample_views() -> list[SessionView]:
    """Return representative rows for layout and lifecycle rendering."""
    home = str(Path.home())
    return [
        _view(
            "JAX Agents",
            "jax-agents",
            f"{home}/research/jaxtor",
            tabs=3,
            panes=6,
            live=True,
        ),
        _view("Dotfiles", "dotfiles", f"{home}/dotfiles", tabs=2, panes=3),
        _view(
            "Main Vault",
            "main-vault",
            f"{home}/Library/Mobile Documents/Main",
            tabs=1,
            panes=1,
            status="archived",
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
