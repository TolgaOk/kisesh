from __future__ import annotations

import importlib
import os
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypeVar, cast
from unittest import mock

Handler = TypeVar("Handler", bound=Callable[..., object])


def result_handler(*, no_ui: bool) -> Callable[[Handler], Handler]:
    """Model Kitty's no-UI decorator without requiring a Kitty process."""
    if not no_ui:
        raise AssertionError("packaged kittens must remain no-UI")

    def decorate(handler: Handler) -> Handler:
        """Return the decorated handler unchanged for direct assertions."""
        return handler

    return decorate


def call(module: ModuleType, name: str, *arguments: object) -> object:
    """Invoke one dynamically loaded kitten entry point with checked arguments."""
    function = cast(Callable[..., object], getattr(module, name))
    return function(*arguments)


class PackagedIntegrationTests(unittest.TestCase):
    """Exercise the exact Python resources shipped inside the wheel."""

    def test_packaged_kittens_load_from_a_runtime_and_forward_exact_calls(self) -> None:
        handler_module = ModuleType("kittens.tui.handler")
        handler_module.__dict__["result_handler"] = result_handler
        options = object()
        fast_data_types = ModuleType("kitty.fast_data_types")
        fast_data_types.__dict__["get_options"] = lambda: options
        fake_modules = {
            "kittens": ModuleType("kittens"),
            "kittens.tui": ModuleType("kittens.tui"),
            "kittens.tui.handler": handler_module,
            "kitty": ModuleType("kitty"),
            "kitty.fast_data_types": fast_data_types,
        }
        names = (
            "kisesh.integration.layout_toggle",
            "kisesh.integration.reload_tab_bar",
            "kisesh.integration.safe_close",
            "kisesh.integration.session_filter",
            "kisesh.integration.tab_bar",
        )
        original_path = list(sys.path)
        runtime = Path(self.id()).absolute()
        loaded: dict[str, ModuleType] = {}

        try:
            with (
                mock.patch.dict(os.environ, {"KISESH_INSTALL_ROOT": str(runtime)}),
                mock.patch.dict(sys.modules, fake_modules),
                mock.patch("importlib.reload", side_effect=lambda module: module),
            ):
                for name in names:
                    sys.modules.pop(name, None)
                    while str(runtime) in sys.path:
                        sys.path.remove(str(runtime))
                    importlib.import_module(name)
                    self.assertEqual(sys.path[0], str(runtime))
                    sys.modules.pop(name)
                    loaded[name] = importlib.import_module(name)
        finally:
            sys.path[:] = original_path
            for name in names:
                sys.modules.pop(name, None)

        layout = loaded["kisesh.integration.layout_toggle"]
        with mock.patch.object(layout, "toggle_session_layout") as toggle:
            self.assertIsNone(call(layout, "main", ["ignored"]))
            self.assertIsNone(call(layout, "handle_result", ["kitten"], None, 7, "boss"))
            toggle.assert_called_once_with("boss")

        reload_bar = loaded["kisesh.integration.reload_tab_bar"]
        with mock.patch.object(reload_bar, "reload_session_bar") as reload_call:
            self.assertIsNone(call(reload_bar, "main", []))
            self.assertEqual(
                call(reload_bar, "handle_result", ["kitten"], None, 8, "boss"),
                "native session bar reloaded",
            )
            reload_call.assert_called_once_with("boss")

        safe_close = loaded["kisesh.integration.safe_close"]
        with mock.patch.object(safe_close, "request_tab_close") as close:
            self.assertIsNone(call(safe_close, "main", []))
            self.assertIsNone(call(safe_close, "handle_result", [], None, 9, "boss"))
            close.assert_called_once_with(9, "boss")

        session_filter = loaded["kisesh.integration.session_filter"]
        self.assertIsNone(call(session_filter, "main", []))
        with self.assertRaisesRegex(ValueError, "exactly one expression"):
            call(session_filter, "handle_result", ["kitten"], None, 10, "boss")
        with mock.patch.object(session_filter, "set_session_filter") as set_filter:
            self.assertIsNone(
                call(
                    session_filter,
                    "handle_result",
                    ["kitten", "var:kisesh_session=one"],
                    None,
                    10,
                    "boss",
                )
            )
            set_filter.assert_called_once_with("var:kisesh_session=one", "boss", options)

        tab_bar = loaded["kisesh.integration.tab_bar"]
        self.assertEqual(tab_bar.draw_tab.__module__, "kisesh.session_bar")


if __name__ == "__main__":
    unittest.main()
