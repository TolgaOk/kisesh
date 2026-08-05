"""Render Workbench identity inside Kitty's native custom tab bar."""

from __future__ import annotations

import importlib
import shlex
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .model import AGENT_VAR, SESSION_ID_VAR, SESSION_NAME_VAR, SESSION_SLUG_VAR

SESSION_ICON = ""
TAB_ICON = "󰓩"
UNATTACHED_ICON = "○"
ELLIPSIS = "…"

_AGENT_LABELS = {"claude": "✻ Claude", "codex": "◇ Codex"}
_AGENT_SYMBOLS = {"claude": "✻", "codex": "◇"}


@dataclass(frozen=True, slots=True)
class SessionBarTab:
    """Cached metadata needed to label one native Kitty tab."""

    title: str
    session_id: str | None
    session_name: str | None
    agents: tuple[str, ...] = ()


class _TabDatum(Protocol):
    """Subset of Kitty's immutable tab-bar datum used by the adapter."""

    @property
    def title(self) -> str:
        """Return the native tab title."""

    @property
    def tab_id(self) -> int:
        """Return the native tab identifier."""


TabDrawer = Callable[[object, object, object, object, int, int, bool, object], int]
_drawer: TabDrawer | None = None


def _clean(value: object, fallback: str) -> str:
    """Flatten display text and remove terminal controls."""
    text = "".join(
        character
        for character in str(value or "")
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(text.split()) or fallback


def _cell_width(value: str) -> int:
    """Approximate Kitty cell width for plain labels and Nerd Font glyphs."""
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in value
    )


def _ellipsize(value: str, max_cells: int) -> str:
    """Shorten text to a cell budget while retaining a visible truncation mark."""
    if max_cells <= 0:
        return ""
    if _cell_width(value) <= max_cells:
        return value
    if max_cells == 1:
        return ELLIPSIS
    output: list[str] = []
    used = 0
    for character in value:
        width = _cell_width(character)
        if used + width > max_cells - 1:
            break
        output.append(character)
        used += width
    return "".join(output) + ELLIPSIS


def _agent_names(agents: Iterable[str]) -> tuple[str, ...]:
    """Keep supported agent names once in their stable visual order."""
    normalized = {str(agent).strip().casefold() for agent in agents}
    return tuple(agent for agent in _AGENT_LABELS if agent in normalized)


def _suffix(agents: tuple[str, ...], *, verbose: bool) -> str:
    """Render recognized agents as concise symbols or labeled markers."""
    labels = _AGENT_LABELS if verbose else _AGENT_SYMBOLS
    return f" {' '.join(labels[agent] for agent in agents)}" if agents else ""


def _fit_group(
    icon: str,
    session_name: str,
    title: str,
    agents: tuple[str, ...],
    max_cells: int,
) -> str:
    """Compact a session boundary while retaining identity and tab title."""
    suffix = _suffix(agents, verbose=False)
    fixed = f"{icon}  · "
    available = max_cells - _cell_width(fixed) - _cell_width(suffix)
    if available < 4:
        return _ellipsize(f"{icon} {session_name}", max_cells)
    session_width = _cell_width(session_name)
    title_width = _cell_width(title)
    session_budget = min(session_width, max(2, available // 2))
    title_budget = min(title_width, available - session_budget)
    if title_budget < min(2, title_width):
        title_budget = min(2, title_width)
        session_budget = available - title_budget
    label = (
        f"{icon} {_ellipsize(session_name, session_budget)} · "
        f"{_ellipsize(title, title_budget)}{suffix}"
    )
    return _ellipsize(label, max_cells)


def _fit_tab(title: str, agents: tuple[str, ...], max_cells: int) -> str:
    """Compact a normal tab while retaining agent symbols when space permits."""
    suffix = _suffix(agents, verbose=False)
    if _cell_width(suffix) + 2 > max_cells:
        suffix = ""
    return f"{_ellipsize(title, max_cells - _cell_width(suffix))}{suffix}"


def render_tab_label(
    tab: SessionBarTab,
    previous: SessionBarTab | None,
    max_cells: int,
) -> str:
    """Build a themed-label payload for one tab with graceful width fallbacks."""
    if max_cells <= 0:
        return ""
    title = _clean(tab.title, "Shell")
    agents = _agent_names(tab.agents)
    starts_group = previous is None or previous.session_id != tab.session_id
    if starts_group:
        tracked = tab.session_id is not None
        icon = SESSION_ICON if tracked else UNATTACHED_ICON
        session_name = _clean(tab.session_name, "Session") if tracked else "Unattached"
        full = f"{icon} {session_name} │ {TAB_ICON} {title}{_suffix(agents, verbose=True)}"
        if _cell_width(full) <= max_cells:
            return full
        medium = f"{icon} {session_name} │ {title}{_suffix(agents, verbose=False)}"
        if _cell_width(medium) <= max_cells:
            return medium
        return _fit_group(icon, session_name, title, agents, max_cells)
    full = f"{TAB_ICON} {title}{_suffix(agents, verbose=True)}"
    if _cell_width(full) <= max_cells:
        return full
    medium = f"{title}{_suffix(agents, verbose=False)}"
    return medium if _cell_width(medium) <= max_cells else _fit_tab(title, agents, max_cells)


def _mapping(value: object) -> Mapping[object, object]:
    """Read a cached Kitty mapping while tolerating callable compatibility APIs."""
    if callable(value):
        try:
            value = value()
        except Exception:
            return {}
    return value if isinstance(value, Mapping) else {}


def _first_variable(windows: Sequence[object], name: str) -> str | None:
    """Return the first nonempty cached user variable across a tab's panes."""
    for window in windows:
        value = _mapping(getattr(window, "user_vars", {})).get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _command_agent(value: object) -> str | None:
    """Recognize an agent from a cached initial child command."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        arguments = tuple(str(item) for item in value)
    elif isinstance(value, str):
        try:
            arguments = tuple(shlex.split(value, posix=True))
        except ValueError:
            arguments = ()
    else:
        arguments = ()
    if not arguments:
        return None
    executable = Path(arguments[0]).name.lstrip("-").casefold()
    return next(
        (
            agent
            for agent in _AGENT_LABELS
            if executable == agent or executable.startswith(f"{agent}-")
        ),
        None,
    )


def _cached_agent(window: object) -> str | None:
    """Read only precomputed variables, initial commands, and titles for an agent."""
    marker = _mapping(getattr(window, "user_vars", {})).get(AGENT_VAR)
    if marker is not None and str(marker).casefold() in _AGENT_LABELS:
        return str(marker).casefold()
    child = getattr(window, "child", None)
    command_agent = _command_agent(getattr(child, "cmdline", ()))
    return command_agent or _command_agent(getattr(window, "title", ""))


def _native_tab(boss: object, tab_id: int) -> object | None:
    """Resolve a tab through Kitty's in-process boss without remote control."""
    resolver = getattr(boss, "tab_for_id", None)
    if not callable(resolver):
        return None
    try:
        return cast(object | None, resolver(tab_id))
    except Exception:
        return None


def _bar_tab(datum: _TabDatum, boss: object) -> SessionBarTab | None:
    """Extract render metadata from cached Kitty tab and window objects."""
    native = _native_tab(boss, datum.tab_id)
    if native is None:
        return None
    try:
        windows = tuple(cast(Iterable[object], native))
    except Exception:
        return None
    session_id = _first_variable(windows, SESSION_ID_VAR)
    session_name = None
    if session_id is not None:
        session_name = _first_variable(windows, SESSION_NAME_VAR) or _first_variable(
            windows, SESSION_SLUG_VAR
        )
    agents = tuple(agent for window in windows if (agent := _cached_agent(window)) is not None)
    return SessionBarTab(datum.title, session_id, session_name, agents)


def _kitty_boss() -> object:
    """Return Kitty's current in-process controller through a lazy import."""
    module = importlib.import_module("kitty.fast_data_types")
    resolver = module.get_boss
    return resolver()


def _kitty_drawer() -> TabDrawer:
    """Cache Kitty's theme-aware powerline renderer after its first use."""
    global _drawer
    if _drawer is None:
        module = importlib.import_module("kitty.tab_bar")
        _drawer = cast(TabDrawer, module.draw_tab_with_powerline)
    return _drawer


def draw_tab(
    draw_data: object,
    screen: object,
    tab: object,
    before: object,
    max_title_length: int,
    index: int,
    is_last: bool,
    extra_data: object,
) -> int:
    """Decorate one native Kitty tab and delegate its themed drawing unchanged."""
    datum = cast(_TabDatum, tab)
    previous_datum = cast(_TabDatum | None, before)
    try:
        boss = _kitty_boss()
        current = _bar_tab(datum, boss)
        previous = _bar_tab(previous_datum, boss) if previous_datum is not None else None
    except Exception:
        current = None
        previous = None
    replacement = cast(Callable[..., object], getattr(tab, "_replace", None))
    decorated = (
        replacement(title=render_tab_label(current, previous, max_title_length))
        if current is not None and callable(replacement)
        else tab
    )
    return _kitty_drawer()(
        draw_data,
        screen,
        decorated,
        before,
        max_title_length,
        index,
        is_last,
        extra_data,
    )
