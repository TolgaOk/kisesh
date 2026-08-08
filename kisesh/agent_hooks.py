"""Parse native agent hook events without coupling them to CLI or storage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TextIO

from .app_profiles import ResumeAdapter

INVALID_SESSION_START_MESSAGE = "agent SessionStart input is incomplete"


@dataclass(frozen=True, slots=True)
class AgentSessionStart:
    """Validated external session identity paired with its originating pane."""

    adapter: ResumeAdapter
    external_session_id: str
    window_id: int


def read_session_start(
    adapter: ResumeAdapter,
    stream: TextIO,
    environment: Mapping[str, str],
) -> AgentSessionStart:
    """Decode one native SessionStart event and its inherited Kitty pane ID."""
    try:
        payload: object = json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(INVALID_SESSION_START_MESSAGE) from error
    window_value = environment.get("KITTY_WINDOW_ID")
    try:
        window_id = int(window_value) if window_value is not None else 0
    except ValueError as error:
        raise ValueError(INVALID_SESSION_START_MESSAGE) from error
    if not isinstance(payload, Mapping):
        raise ValueError(INVALID_SESSION_START_MESSAGE)
    external_session_id = payload.get("session_id")
    if (
        payload.get("hook_event_name") != "SessionStart"
        or not isinstance(external_session_id, str)
        or not external_session_id
        or window_id <= 0
    ):
        raise ValueError(INVALID_SESSION_START_MESSAGE)
    return AgentSessionStart(adapter, external_session_id, window_id)
