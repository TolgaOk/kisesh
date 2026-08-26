from __future__ import annotations

import importlib
import os
import shutil
import subprocess
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


def assert_session_close_handler(module: ModuleType, options: object) -> None:
    """Exercise validation and forwarding through the packaged close kitten."""
    with unittest.TestCase().assertRaisesRegex(
        ValueError,
        "target and one optional successor",
    ):
        call(module, "handle_result", ["kitten", "session-close"], None, 10, "boss")
    with unittest.TestCase().assertRaisesRegex(ValueError, "successor identity is incomplete"):
        call(
            module,
            "handle_result",
            ["kitten", "session-close", "closing", "remaining", "-"],
            None,
            10,
            "boss",
        )
    with unittest.TestCase().assertRaises(ValueError):
        call(
            module,
            "handle_result",
            ["kitten", "session-close", "closing", "remaining", "not-an-id"],
            None,
            10,
            "boss",
        )
    with mock.patch.object(module, "close_live_session") as close_session:
        call(
            module,
            "handle_result",
            ["kitten", "session-close", "closing", "remaining", "12"],
            None,
            10,
            "boss",
        )
        close_session.assert_called_once_with("closing", "remaining", 12, "boss", options)
    with mock.patch.object(module, "close_live_session") as close_session:
        call(
            module,
            "handle_result",
            ["kitten", "session-close", "closing", "-", "-"],
            None,
            10,
            "boss",
        )
        close_session.assert_called_once_with("closing", None, None, "boss", options)


class PackagedIntegrationTests(unittest.TestCase):
    """Exercise the exact Python resources shipped inside the wheel."""

    @unittest.skipUnless(shutil.which("kitty"), "Kitty is required")
    def test_no_ui_actions_load_with_pre_filter_model_cached_by_kitty(self) -> None:
        actions = Path(__file__).parents[1] / "kisesh" / "integration" / "actions.py"
        project = Path(__file__).parents[1]
        script = (
            "import runpy,sys; "
            f"sys.path.insert(0,{str(project)!r}); "
            "import kisesh.model as model; "
            "model.__dict__.pop('SessionFilterTarget',None); "
            "model.__dict__.pop('session_filter_expression',None); "
            "sys.modules.pop('kisesh.kitty_actions',None); "
            "loaded=runpy.run_path(sys.argv[1]); "
            "print(callable(loaded['handle_result']))"
        )

        result = subprocess.run(
            [shutil.which("kitty") or "kitty", "+runpy", script, str(actions)],
            cwd=project,
            env={**os.environ, "KISESH_INSTALL_ROOT": str(project)},
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

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
            "kisesh.integration.actions",
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

        actions = loaded["kisesh.integration.actions"]
        self.assertIsNone(call(actions, "main", ["ignored"]))
        with mock.patch.object(actions, "toggle_session_layout") as toggle:
            self.assertIsNone(
                call(actions, "handle_result", ["kitten", "layout-toggle"], None, 7, "boss")
            )
            toggle.assert_called_once_with("boss")

        with mock.patch.object(actions, "request_tab_close") as close:
            self.assertIsNone(
                call(actions, "handle_result", ["kitten", "safe-close"], None, 9, "boss")
            )
            close.assert_called_once_with(9, "boss")

        with mock.patch.object(actions, "close_manager_overlay") as close_manager:
            self.assertIsNone(
                call(actions, "handle_result", ["kitten", "manager-close"], None, 11, "boss")
            )
            close_manager.assert_called_once_with(11, "boss")

        with mock.patch.object(actions, "reload_config_preserving_session") as reload_config:
            self.assertIsNone(
                call(actions, "handle_result", ["kitten", "reload-config"], None, 9, "boss")
            )
            reload_config.assert_called_once_with("boss", actions.get_options)

        with mock.patch.dict(sys.modules, fake_modules):
            assert_session_close_handler(actions, options)

        with self.assertRaisesRegex(ValueError, "exactly one expression"):
            call(actions, "handle_result", ["kitten", "session-filter"], None, 10, "boss")
        with (
            mock.patch.dict(sys.modules, fake_modules),
            mock.patch.object(actions, "set_session_filter") as set_filter,
        ):
            self.assertIsNone(
                call(
                    actions,
                    "handle_result",
                    ["kitten", "session-filter", "var:kisesh_session=one"],
                    None,
                    10,
                    "boss",
                )
            )
            set_filter.assert_called_once_with("var:kisesh_session=one", "boss", options)

        for arguments in (
            ["kitten"],
            ["kitten", "unknown"],
            ["kitten", "safe-close", "extra"],
            ["kitten", "manager-close", "extra"],
            ["kitten", "reload-config", "extra"],
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(ValueError, "unknown KiSesh action"),
            ):
                call(actions, "handle_result", arguments, None, 10, "boss")

        tab_bar = loaded["kisesh.integration.tab_bar"]
        self.assertEqual(tab_bar.draw_tab.__module__, "kisesh.session_bar")


if __name__ == "__main__":
    unittest.main()
