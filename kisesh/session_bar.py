"""Render KiSesh identity inside Kitty's native custom tab bar."""

from __future__ import annotations

import importlib
import shlex
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from typing import Protocol, cast

from .app_profiles import current_app_profiles

SESSION_ICON = ""
ELLIPSIS = "…"
TAB_START_CAP = ""
SESSION_ID_VAR = "kisesh_session"
SESSION_SLUG_VAR = "kisesh_slug"
SESSION_NAME_VAR = "kisesh_name"
AGENT_VAR = "kisesh_agent"
APP_VAR = "kisesh_app"
KISESH_UI_VAR = "kisesh_ui"
MIN_SPLIT_SEGMENT_CELLS = 6


@dataclass(frozen=True, slots=True)
class SessionBarTab:
    """Cached focused-pane and session metadata for one native Kitty tab."""

    focused_title: str
    session_id: str | None
    session_name: str | None
    focused_application: str | None = None
    focused_pane_index: int = 0
    pane_count: int = 1


class _TabDatum(Protocol):
    """Subset of Kitty's immutable tab-bar datum used by the adapter."""

    @property
    def title(self) -> str:
        """Return the native tab title."""

    @property
    def tab_id(self) -> int:
        """Return the native tab identifier."""


class _ExtraData(Protocol):
    """Neighboring tab data supplied by Kitty's custom renderer contract."""

    @property
    def prev_tab(self) -> _TabDatum | None:
        """Return the previous native tab, if this is not the first tab."""


class _DefaultBarDrawData(Protocol):
    """Theme background exposed by Kitty's immutable tab-bar draw data."""

    @property
    def default_bg(self) -> int:
        """Return the tab-bar background as an integer-compatible color."""


class _ScreenCursor(Protocol):
    """Mutable Kitty cursor state needed to draw adjacent native segments."""

    x: int
    bg: int
    fg: int


class _SegmentScreen(Protocol):
    """Kitty screen operations needed to start one rounded tab segment."""

    cursor: _ScreenCursor

    def draw(self, text: str) -> None:
        """Draw text at the current cursor position."""


@dataclass(frozen=True, slots=True)
class _SegmentExtraData:
    """Neighbor state for the synthetic session segment inside one tab."""

    prev_tab: object | None
    next_tab: object | None
    for_layout: bool


@dataclass(frozen=True, slots=True)
class _ResolvedTabs:
    """KiSesh metadata resolved for the current and previous native tabs."""

    current: SessionBarTab | None
    previous: SessionBarTab | None


@dataclass(frozen=True, slots=True)
class _TabColors:
    """Theme colors encoded for direct assignment to Kitty's screen cursor."""

    background: int
    foreground: int


@dataclass(frozen=True, slots=True)
class _SegmentPlan:
    """Complete rendering plan for a capped session and native tab segment."""

    session_draw_data: object
    session_tab: object
    content_tab: object
    session_width: int
    content_width: int
    session_colors: _TabColors
    content_colors: _TabColors
    default_background: int


class SessionBarCache(Protocol):
    """Kitty run-once loader whose cached custom renderer can be cleared."""

    def clear_cached(self) -> None:
        """Forget the previously loaded custom renderer."""


class SessionBarManager(Protocol):
    """Native tab manager operations needed for an immediate redraw."""

    def mark_tab_bar_dirty(self) -> None:
        """Request a native tab-bar repaint."""

    def update_tab_bar_data(self) -> None:
        """Rebuild native tab-bar data with the refreshed renderer."""


class SessionBarBoss(Protocol):
    """Native Kitty controller operations used by the one-shot reloader."""

    all_tab_managers: Iterable[SessionBarManager]

    def refresh_active_tab_bar(self) -> bool:
        """Refresh the focused operating-system window's native bar."""


TabDrawer = Callable[[object, object, object, int, int, int, bool, object], int]
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
    widths = accumulate(_cell_width(character) for character in value)
    visible = (
        character for character, used in zip(value, widths, strict=True) if used <= max_cells - 1
    )
    return "".join(visible) + ELLIPSIS


def _pane_descriptor(tab: SessionBarTab) -> tuple[str, str]:
    """Return the configured icon and name for only the tab's focused pane."""
    profiles = current_app_profiles()
    profile = profiles.named(tab.focused_application)
    if profile is not None:
        return profile.icon, profile.label
    return profiles.defaults.icon, _clean(tab.focused_title, profiles.defaults.label)


def _starts_group(tab: SessionBarTab, previous: SessionBarTab | None) -> bool:
    """Return whether a tab begins a new contiguous session group."""
    return tab.session_id is not None and (
        previous is None or previous.session_id != tab.session_id
    )


def _session_descriptor(tab: SessionBarTab) -> tuple[str, str]:
    """Return the icon and sanitized name for a tab's session boundary."""
    return SESSION_ICON, _clean(tab.session_name, "Session")


def _pane_label(tab: SessionBarTab, max_cells: int) -> str:
    """Fit focused-pane identity while preserving its current/total position."""
    if max_cells <= 0:
        return ""
    pane_icon, pane_name = _pane_descriptor(tab)
    pane_count = max(0, tab.pane_count)
    if pane_count <= 1:
        return _ellipsize(f"{pane_icon} {pane_name}", max_cells)
    focused_index = min(max(0, tab.focused_pane_index), pane_count - 1)
    position = f"{focused_index + 1}/{pane_count}"
    suffix = f"  {position}"
    full = f"{pane_icon} {pane_name}{suffix}"
    if _cell_width(full) <= max_cells:
        return full
    prefix = f"{pane_icon} "
    name_budget = max_cells - _cell_width(prefix) - _cell_width(suffix)
    if name_budget > 0:
        return f"{prefix}{_ellipsize(pane_name, name_budget)}{suffix}"
    compact = f"{pane_icon} {position}"
    return compact if _cell_width(compact) <= max_cells else _ellipsize(position, max_cells)


def _fit_group(
    session_icon: str,
    session_name: str,
    tab: SessionBarTab,
    max_cells: int,
) -> str:
    """Compact a session boundary while retaining focused-pane identity."""
    fixed = f"{session_icon}  · "
    available = max_cells - _cell_width(fixed)
    if available < 4:
        return _pane_label(tab, max_cells)
    session_width = _cell_width(session_name)
    session_budget = min(session_width, max(1, available // 3))
    pane_label = _pane_label(tab, max(1, available - session_budget))
    session_budget = min(session_width, max(1, available - _cell_width(pane_label)))
    label = f"{session_icon} {_ellipsize(session_name, session_budget)} · {pane_label}"
    return _ellipsize(label, max_cells)


def render_tab_label(
    tab: SessionBarTab,
    previous: SessionBarTab | None,
    max_cells: int,
) -> str:
    """Render only focused-pane identity plus a leading session boundary."""
    if max_cells <= 0:
        return ""
    pane_label = _pane_label(tab, max_cells)
    if _starts_group(tab, previous):
        session_icon, session_name = _session_descriptor(tab)
        full = f"{session_icon} {session_name} │ {pane_label}"
        if _cell_width(full) <= max_cells:
            return full
        return _fit_group(session_icon, session_name, tab, max_cells)
    return pane_label


def _mapping(value: object) -> Mapping[object, object]:
    """Read a cached Kitty mapping while tolerating callable compatibility APIs."""
    if callable(value):
        try:
            value = value()
        except Exception:
            return {}
    return value if isinstance(value, Mapping) else {}


def _variable(variables: Mapping[object, object], name: str) -> str | None:
    """Resolve one cached nonempty user variable."""
    value = variables.get(name)
    return str(value).strip() if value is not None and str(value).strip() else None


def _first_variable(windows: Sequence[object], name: str) -> str | None:
    """Return the first user variable across a tab's panes."""
    for window in windows:
        if value := _variable(_mapping(getattr(window, "user_vars", {})), name):
            return value
    return None


def _is_kisesh_ui_window(window: object) -> bool:
    """Exclude transient manager overlays from native pane indicators."""
    value = _variable(_mapping(getattr(window, "user_vars", {})), KISESH_UI_VAR) or ""
    return value.casefold() not in {"", "0", "false", "no"}


def _command_application(value: object) -> str | None:
    """Recognize a configured application from a cached initial child command."""
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
    profile = current_app_profiles().match(executable)
    return profile.name if profile is not None else None


def _foreground_application(child: object) -> str | None:
    """Return a configured app from Kitty's complete foreground process group."""
    try:
        processes = cast(Iterable[object], getattr(child, "foreground_processes", ()))
        for process in processes:
            application = _command_application(_mapping(process).get("cmdline"))
            if application is not None:
                return application
    except Exception:
        pass
    try:
        return _command_application(getattr(child, "foreground_cmdline", ()))
    except Exception:
        return None


def _cached_application(window: object) -> str | None:
    """Resolve an app from markers, Kitty's foreground command, or stable fallbacks."""
    variables = _mapping(getattr(window, "user_vars", {}))
    marker = _variable(variables, APP_VAR) or _variable(variables, AGENT_VAR)
    profile = current_app_profiles().named(str(marker).casefold() if marker is not None else None)
    if profile is not None:
        return profile.name
    child = getattr(window, "child", None)
    foreground_application = _foreground_application(child)
    if foreground_application is not None:
        return foreground_application
    command_application = _command_application(getattr(child, "cmdline", ()))
    return command_application or _command_application(getattr(window, "title", ""))


def _focused_window(native: object, windows: tuple[object, ...]) -> object | None:
    """Return Kitty's active pane with a stable first-pane compatibility fallback."""
    try:
        focused = getattr(native, "active_window", None)
    except Exception:
        focused = None
    if focused is not None and any(focused is window for window in windows):
        return cast(object, focused)
    return next(iter(windows), None)


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
    content_windows = tuple(window for window in windows if not _is_kisesh_ui_window(window))
    focused = _focused_window(native, content_windows)
    focused_title = getattr(focused, "title", datum.title) if focused is not None else datum.title
    focused_application = _cached_application(focused) if focused is not None else None
    focused_pane_index = next(
        (index for index, window in enumerate(content_windows) if window is focused),
        0,
    )
    return SessionBarTab(
        focused_title=str(focused_title),
        session_id=session_id,
        session_name=session_name,
        focused_application=focused_application,
        focused_pane_index=focused_pane_index,
        pane_count=len(content_windows),
    )


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


def _native_tab_colors(draw_data: object, tab: object) -> _TabColors | None:
    """Resolve one tab's theme colors in Kitty's encoded screen format."""
    background = cast(Callable[[object], int] | None, getattr(draw_data, "tab_bg", None))
    foreground = cast(Callable[[object], int] | None, getattr(draw_data, "tab_fg", None))
    if not callable(background) or not callable(foreground):
        return None
    try:
        module = importlib.import_module("kitty.tab_bar")
        as_rgb = cast(Callable[[int], int], module.as_rgb)
        return _TabColors(as_rgb(background(tab)), as_rgb(foreground(tab)))
    except Exception:
        return None


def _default_bar_background(draw_data: object) -> int | None:
    """Resolve the theme's tab-bar background between rounded segments."""
    try:
        module = importlib.import_module("kitty.tab_bar")
        as_rgb = cast(Callable[[int], int], module.as_rgb)
        return as_rgb(int(cast(_DefaultBarDrawData, draw_data).default_bg))
    except Exception:
        return None


def _rounded_draw_data(draw_data: object) -> object | None:
    """Clone Kitty's immutable draw data with its native round separator style."""
    replacement = cast(Callable[..., object] | None, getattr(draw_data, "_replace", None))
    if not callable(replacement):
        return None
    try:
        return replacement(powerline_style="round")
    except (TypeError, ValueError):
        return None


def _resolved_tabs(datum: _TabDatum, neighbors: _ExtraData) -> _ResolvedTabs:
    """Resolve cached KiSesh metadata while tolerating disappearing native tabs."""
    try:
        boss = _kitty_boss()
        current = _bar_tab(datum, boss)
    except Exception:
        current = None
        boss = None
    previous_datum = getattr(neighbors, "prev_tab", None)
    try:
        previous = (
            _bar_tab(previous_datum, boss)
            if previous_datum is not None and boss is not None
            else None
        )
    except Exception:
        previous = None
    return _ResolvedTabs(current, previous)


def _segment_plan(
    draw_data: object,
    tab: object,
    resolved: _ResolvedTabs,
    max_title_length: int,
) -> _SegmentPlan | None:
    """Build two native segments while preserving useful content for each."""
    current = resolved.current
    replacement = cast(Callable[..., object] | None, getattr(tab, "_replace", None))
    session_draw_data = _rounded_draw_data(draw_data)
    if (
        current is None
        or not _starts_group(current, resolved.previous)
        or max_title_length < MIN_SPLIT_SEGMENT_CELLS * 2 + _cell_width(TAB_START_CAP)
        or not callable(replacement)
        or session_draw_data is None
    ):
        return None
    icon, session_name = _session_descriptor(current)
    session_label = f"{icon} {session_name}"
    maximum_session_width = max_title_length - MIN_SPLIT_SEGMENT_CELLS - _cell_width(TAB_START_CAP)
    session_width = max(
        MIN_SPLIT_SEGMENT_CELLS,
        min(_cell_width(session_label) + 2, maximum_session_width),
    )
    content_width = max_title_length - session_width - _cell_width(TAB_START_CAP)
    try:
        session_tab = replacement(
            title=_ellipsize(session_label, session_width - 2),
            is_active=False,
            needs_attention=False,
        )
        content_tab = replacement(title=render_tab_label(current, current, content_width - 2))
    except (TypeError, ValueError):
        return None
    session_colors = _native_tab_colors(draw_data, session_tab)
    content_colors = _native_tab_colors(draw_data, content_tab)
    default_background = _default_bar_background(draw_data)
    if session_colors is None or content_colors is None or default_background is None:
        return None
    return _SegmentPlan(
        session_draw_data,
        session_tab,
        content_tab,
        session_width,
        content_width,
        session_colors,
        content_colors,
        default_background,
    )


def _draw_tab_start(screen: _SegmentScreen, plan: _SegmentPlan) -> None:
    """Paint only the tab's left cap against the real tab-bar background."""
    cursor = screen.cursor
    cursor.bg = plan.default_background
    cursor.fg = plan.content_colors.background
    screen.draw(TAB_START_CAP)
    cursor.bg = plan.content_colors.background
    cursor.fg = plan.content_colors.foreground


def reload_session_bar(boss: SessionBarBoss) -> None:
    """Clear Kitty's run-once custom-bar cache and redraw every native bar."""
    global _drawer
    module = importlib.import_module("kitty.tab_bar")
    loaders = (
        cast(SessionBarCache, module.load_custom_draw_tab),
        cast(SessionBarCache, module.load_custom_draw_tab_module),
    )
    for loader in loaders:
        loader.clear_cached()
    _drawer = None
    for manager in boss.all_tab_managers:
        manager.mark_tab_bar_dirty()
        manager.update_tab_bar_data()
    boss.refresh_active_tab_bar()


def draw_tab(
    draw_data: object,
    screen: object,
    tab: object,
    before: int,
    max_title_length: int,
    index: int,
    is_last: bool,
    extra_data: object,
) -> int:
    """Draw session identity separately while retaining one native tab extent."""
    datum = cast(_TabDatum, tab)
    neighbors = cast(_ExtraData, extra_data)
    resolved = _resolved_tabs(datum, neighbors)
    drawer = _kitty_drawer()
    cursor = cast(_ScreenCursor | None, getattr(screen, "cursor", None))
    screen_draw = getattr(screen, "draw", None)
    plan = (
        _segment_plan(draw_data, tab, resolved, max_title_length)
        if cursor is not None
        and isinstance(getattr(cursor, "x", None), int)
        and callable(screen_draw)
        else None
    )
    if plan is not None and cursor is not None:
        cursor.bg = plan.session_colors.background
        cursor.fg = plan.session_colors.foreground
        segment_neighbors = _SegmentExtraData(
            prev_tab=getattr(neighbors, "prev_tab", None),
            next_tab=None,
            for_layout=bool(getattr(neighbors, "for_layout", False)),
        )
        drawer(
            plan.session_draw_data,
            screen,
            plan.session_tab,
            before,
            plan.session_width,
            index,
            False,
            segment_neighbors,
        )
        _draw_tab_start(cast(_SegmentScreen, screen), plan)
        return drawer(
            draw_data,
            screen,
            plan.content_tab,
            cursor.x,
            plan.content_width,
            index,
            is_last,
            extra_data,
        )
    replacement = cast(Callable[..., object] | None, getattr(tab, "_replace", None))
    decorated = (
        replacement(title=render_tab_label(resolved.current, resolved.previous, max_title_length))
        if resolved.current is not None and callable(replacement)
        else tab
    )
    return drawer(
        draw_data,
        screen,
        decorated,
        before,
        max_title_length,
        index,
        is_last,
        extra_data,
    )
