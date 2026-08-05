"""Build concise selected-session previews from live or persisted state."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .app_profiles import DEFAULT_APP_PROFILES, AppProfiles
from .domain import KittyWindow, PaneContext, TabContext
from .service import SessionView

PreviewSource = Literal["live", "saved", "summary"]

_SHELLS = {"ash", "bash", "dash", "fish", "nu", "pwsh", "sh", "tcsh", "zsh"}


@dataclass(slots=True, frozen=True)
class PanePreview:
    """Display-relevant state for one live or persisted terminal pane."""

    program: str
    agent: str | None
    label: str
    icon: str
    last_command: str | None
    active: bool
    restore_available: bool
    needs_attention: bool


@dataclass(slots=True, frozen=True)
class TabPreview:
    """Ordered panes and concise metadata for one session tab."""

    title: str
    layout: str
    focused: bool
    panes: tuple[PanePreview, ...]
    details_available: bool = True


@dataclass(slots=True, frozen=True)
class SessionPreview:
    """Selected-session contents paired with their authoritative source."""

    source: PreviewSource
    tabs: tuple[TabPreview, ...]


def build_session_preview(
    view: SessionView,
    profiles: AppProfiles = DEFAULT_APP_PROFILES,
) -> SessionPreview:
    """Prefer current Kitty contents, then saved context, then manifest summaries."""
    if view.live_tabs:
        tabs = tuple(
            TabPreview(
                title=tab.title.strip() or f"Tab {index + 1}",
                layout=tab.layout.strip(),
                focused=tab.is_focused,
                panes=tuple(_live_pane(window, profiles) for window in tab.windows),
            )
            for index, tab in enumerate(view.live_tabs)
        )
        return SessionPreview("live", tabs)
    if view.context is not None and view.context["tabs"]:
        return SessionPreview(
            "saved",
            tuple(
                _saved_tab(tab, index, profiles) for index, tab in enumerate(view.context["tabs"])
            ),
        )

    summary = view.stored.manifest.summary
    total = max(summary.tab_count, len(summary.tab_titles))
    tabs = tuple(
        TabPreview(
            title=(
                summary.tab_titles[index].strip()
                if index < len(summary.tab_titles) and summary.tab_titles[index].strip()
                else f"Tab {index + 1}"
            ),
            layout="",
            focused=False,
            panes=(),
            details_available=False,
        )
        for index in range(total)
    )
    return SessionPreview("summary", tabs)


def _saved_tab(tab: TabContext, index: int, profiles: AppProfiles) -> TabPreview:
    """Convert one fully typed persisted tab into preview state."""
    focus_candidates = [
        (pane_index, pane["last_focused_at"])
        for pane_index, pane in enumerate(tab["panes"])
        if "last_focused_at" in pane
    ]
    active_index = max(focus_candidates, key=lambda item: item[1], default=(None, 0.0))[0]
    return TabPreview(
        title=tab["title"].strip() or f"Tab {index + 1}",
        layout=tab["layout"].strip(),
        focused=tab["focused"],
        panes=tuple(
            _saved_pane(pane, pane_index == active_index, profiles)
            for pane_index, pane in enumerate(tab["panes"])
        ),
    )


def _saved_pane(pane: PaneContext, active: bool, profiles: AppProfiles) -> PanePreview:
    """Retain persisted program, command, restore, and attention metadata."""
    program = (pane["program"] or "").strip()
    if not program and pane["foreground_argv"]:
        program = _program_name(pane["foreground_argv"])
    if not program:
        program = pane["title"].strip() or "shell"
    matched = profiles.match(program)
    agent = (pane["agent"] or "").strip().casefold() or (
        matched.name if matched is not None and matched.agent else None
    )
    label, icon = _presentation(program, agent, profiles)
    last_command = " ".join((pane["last_command"] or "").split()) or None
    return PanePreview(
        program=program,
        agent=agent,
        label=label,
        icon=icon,
        last_command=last_command,
        active=active,
        restore_available=pane["restore"] is not None,
        needs_attention=pane["needs_attention"],
    )


def _live_pane(window: KittyWindow, profiles: AppProfiles) -> PanePreview:
    """Derive current foreground identity without consulting stale saved context."""
    argv: list[str] = []
    for process in reversed(window.get("foreground_processes", [])):
        if process.get("cmdline"):
            argv = process["cmdline"]
            break
    reported = " ".join(window.get("last_reported_cmdline", "").split())
    if not argv and reported and not window.get("at_prompt"):
        try:
            argv = shlex.split(reported)
        except ValueError:
            argv = []
    program = _program_name(argv)
    if not program:
        program = window.get("title", "").strip() or "shell"
    matched = profiles.match(program)
    agent = matched.name if matched is not None and matched.agent else None
    label, icon = _presentation(program, agent, profiles)
    return PanePreview(
        program=program,
        agent=agent,
        label=label,
        icon=icon,
        last_command=reported or None,
        active=bool(window.get("is_active") or window.get("is_focused")),
        restore_available=False,
        needs_attention=bool(window.get("needs_attention")),
    )


def _program_name(argv: list[str]) -> str:
    """Return a clean executable basename or an empty fallback."""
    if not argv:
        return ""
    return Path(argv[0]).name.lstrip("-").strip()


def _presentation(
    program: str,
    agent: str | None,
    profiles: AppProfiles,
) -> tuple[str, str]:
    """Resolve configured label and icon with a visible unmatched fallback."""
    profile = profiles.named(agent) or profiles.match(program)
    if profile is not None:
        return profile.label, profile.icon
    return program or profiles.defaults.label, profiles.defaults.icon


def is_shell_program(program: str) -> bool:
    """Report whether a preview program is a known interactive shell."""
    return program.casefold() in _SHELLS
