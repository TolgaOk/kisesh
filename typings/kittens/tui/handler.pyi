"""Typed subset of Kitty's result-handler decorator."""

from collections.abc import Callable
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")

def result_handler(*, no_ui: bool = ...) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]: ...
