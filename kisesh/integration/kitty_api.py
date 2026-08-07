"""Typed dynamic boundary for APIs supplied by Kitty's embedded Python."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import ParamSpec, TypeVar, cast

_P = ParamSpec("_P")
_R = TypeVar("_R")


def result_handler(*, no_ui: bool) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Load Kitty's result-handler decorator without a build-time dependency."""
    module = importlib.import_module("kittens.tui.handler")
    factory = cast(Callable[..., object], module.result_handler)
    return cast(Callable[[Callable[_P, _R]], Callable[_P, _R]], factory(no_ui=no_ui))


def get_options() -> object:
    """Return Kitty's live options through its embedded fast-data module."""
    module = importlib.import_module("kitty.fast_data_types")
    resolver = cast(Callable[[], object], module.get_options)
    return resolver()
