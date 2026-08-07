"""Behavioral tests for typed CLI parsing, routing, and watcher payloads."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from unittest import mock

from kisesh.app_profiles import DEFAULT_APP_PROFILES
from kisesh.cli import (
    INVALID_CLOSE_MESSAGE,
    INVALID_EVENTS_MESSAGE,
    INVALID_PAYLOAD_MESSAGE,
    AddTab,
    ArchiveSession,
    AutosaveSession,
    CliConfig,
    CloseSession,
    CopyTab,
    CreateSession,
    DetachTab,
    DisableIntegration,
    Doctor,
    InstallIntegration,
    ListSessions,
    Manager,
    OpenSession,
    PrintLastOutput,
    RemoveSession,
    RenameSession,
    RestoreShell,
    ShowContext,
    UnarchiveSession,
    UninstallIntegration,
    _autosave_payload_from_stdin,
    _closing_capture,
    _dispatch,
    _needs_kitty,
    _normalized_events,
    _run_integration,
    _run_lifecycle,
    _run_maintenance,
    _run_manager,
    _run_membership,
    _run_read,
    _service,
    main,
    parse_arguments,
)
from kisesh.context import build_context
from kisesh.kitty_client import LiveTab
from kisesh.model import ClosingPaneCapture, SessionManifest
from kisesh.service import (
    KiSeshError,
    KiSeshService,
    SessionView,
    UnownedTabsAction,
    UnownedTabsDecision,
)
from kisesh.store import StoredSession


class ShellReplaced(RuntimeError):
    """Represent the non-returning shell exec boundary during a test."""


def _stored(name: str = "Project", slug: str = "project") -> StoredSession:
    """Create one valid in-memory stored-session result."""
    return StoredSession(
        SessionManifest(name=name, slug=slug, project_root="/tmp/project"),
        Path("/tmp") / slug,
    )


def _context() -> object:
    """Create representative saved shell context for CLI output tests."""
    return build_context(
        [
            LiveTab(
                1,
                7,
                0,
                "Project",
                "splits",
                [
                    {
                        "id": 11,
                        "cwd": "/tmp/project",
                        "foreground_processes": [{"cmdline": ["-zsh"]}],
                        "at_prompt": True,
                    }
                ],
            )
        ],
        command_outputs={11: "tests passed"},
    )


def _service_mock(stored: StoredSession | None = None) -> tuple[KiSeshService, mock.MagicMock]:
    """Return a typed service view and its configurable autospecced mock."""
    raw = mock.create_autospec(KiSeshService, instance=True)
    raw.store = mock.MagicMock()
    raw.kitty = mock.MagicMock()
    raw.profiles = DEFAULT_APP_PROFILES
    raw.store.get.return_value = stored or _stored()
    return cast(KiSeshService, raw), raw


def _stdin_payload(payload: object) -> io.StringIO:
    """Encode one watcher payload as a readable standard-input stream."""
    return io.StringIO(json.dumps(payload))


class CliTests(unittest.TestCase):
    """Cover every typed command family and payload compatibility path."""

    def test_tyro_parses_aliases_and_cascaded_global_options(self) -> None:
        """Accept concise subcommands with global options on either side."""
        before = parse_arguments(["--socket", "unix:/tmp/kitty", "add-tab", "project"])
        after = parse_arguments(
            [
                "add-tab",
                "project",
                "--data-dir",
                "/tmp/kisesh",
                "--app-config",
                "/tmp/apps.toml",
            ]
        )
        remove_alias = parse_arguments(["trash", "project"])
        attach = parse_arguments(["open", "project", "--unowned-tabs", "attach"])
        named = parse_arguments(
            [
                "open",
                "project",
                "--unowned-tabs",
                "save-separately",
                "--unowned-name",
                "Named scratch",
            ]
        )
        discard = parse_arguments(["open", "project", "--unowned-tabs", "discard"])
        promoted = parse_arguments(["close", "project", "--promote-os-window", "41"])
        integration = parse_arguments(["install", "--kitty-config", "/tmp/kitty.conf"])

        self.assertIsInstance(before.command, AddTab)
        self.assertEqual(before.socket, "unix:/tmp/kitty")
        self.assertIsInstance(after.command, AddTab)
        self.assertEqual(after.data_dir, Path("/tmp/kisesh"))
        self.assertEqual(after.app_config, Path("/tmp/apps.toml"))
        self.assertIsInstance(remove_alias.command, RemoveSession)
        self.assertEqual(
            cast(OpenSession, attach.command).unowned_tabs,
            UnownedTabsAction.ATTACH,
        )
        self.assertEqual(cast(OpenSession, named.command).unowned_name, "Named scratch")
        self.assertEqual(
            cast(OpenSession, discard.command).unowned_tabs,
            UnownedTabsAction.DISCARD,
        )
        self.assertEqual(cast(CloseSession, promoted.command).promote_os_window, 41)
        self.assertEqual(
            cast(InstallIntegration, integration.command).kitty_config,
            Path("/tmp/kitty.conf"),
        )

    def test_only_live_operations_construct_an_eager_kitty_client(self) -> None:
        """Keep stored-context reads offline unless a connection override is explicit."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_config = root / "apps.toml"
            app_config.write_text(
                "version = 1\n\n"
                '[defaults]\nrestore = "ignore"\nlabel = "Unknown"\nicon = "?"\n\n'
                '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\n'
                'label = "Tool"\nicon = "T"\n',
                encoding="utf-8",
            )
            offline = CliConfig(
                ListSessions(),
                data_dir=root / "data",
                app_config=app_config,
            )
            connected = CliConfig(
                ListSessions(),
                data_dir=root / "data",
                socket="unix:/tmp/kitty",
            )
            live = CliConfig(Manager(), data_dir=root / "data")
            with mock.patch("kisesh.cli.KittyClient") as client:
                offline_service = _service(offline)
                connected_service = _service(connected)
                live_service = _service(live)

        self.assertFalse(_needs_kitty(ListSessions()))
        self.assertTrue(_needs_kitty(Manager()))
        self.assertIsNone(offline_service.kitty)
        tool = offline_service.profiles.named("tool")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.icon if tool is not None else None, "T")
        self.assertIs(connected_service.kitty, client.return_value)
        self.assertIs(live_service.kitty, client.return_value)
        self.assertEqual(client.call_count, 2)

    def test_read_commands_render_text_json_context_output_and_shell(self) -> None:
        """Exercise every non-mutating CLI output shape and shell boundary."""
        stored = _stored()
        service, raw = _service_mock(stored)
        live_tab = LiveTab(1, 7, 0, "Project", "splits", [{"id": 11}])
        raw.views.return_value = [SessionView(stored, [live_tab])]
        raw.context.return_value = _context()

        text_output = io.StringIO()
        with redirect_stdout(text_output):
            self.assertEqual(_run_read(ListSessions(), service), 0)
        self.assertEqual(text_output.getvalue(), "project\tlive\tProject\n")

        json_output = io.StringIO()
        with redirect_stdout(json_output):
            self.assertEqual(_run_read(ListSessions(json=True), service), 0)
        payload = json.loads(json_output.getvalue())
        self.assertTrue(payload[0]["live"])
        self.assertEqual(payload[0]["live_tab_ids"], [7])

        context_output = io.StringIO()
        with redirect_stdout(context_output):
            self.assertEqual(_run_read(ShowContext(stored.manifest.id), service), 0)
        self.assertEqual(json.loads(context_output.getvalue())["command_count"], 0)

        for saved_output, expected in (
            ("tests passed", "tests passed\n"),
            ("ok\n", "ok\n"),
            ("", ""),
        ):
            raw.context.return_value = build_context(
                [live_tab],
                command_outputs={11: saved_output},
            )
            stream = io.StringIO()
            with self.subTest(saved_output=saved_output), redirect_stdout(stream):
                self.assertEqual(_run_read(PrintLastOutput(stored.manifest.id, 0, 0), service), 0)
                self.assertEqual(stream.getvalue(), expected)

        raw.context.return_value = _context()
        with (
            mock.patch(
                "kisesh.cli.run_restored_shell",
                side_effect=ShellReplaced,
            ) as restore,
            self.assertRaises(ShellReplaced),
        ):
            _run_read(RestoreShell(stored.manifest.id, 0, 0), service)
        restore.assert_called_once_with(stored, raw.context.return_value, 0, 0)

    def test_membership_commands_route_arguments_and_print_the_result_slug(self) -> None:
        """Route create, membership, close, and open operations exactly once."""
        stored = _stored()
        cases = (
            (CreateSession("Project", "/tmp/root"), "create_from_active", ("Project", "/tmp/root")),
            (AddTab("project"), "add_current_tab", ("project",)),
            (DetachTab("project"), "detach_current_tab", ("project",)),
            (CopyTab("project"), "copy_current_tab", ("project",)),
            (CloseSession("project"), "save_and_close", ("project", None)),
            (CloseSession("project", 41), "save_and_close", ("project", 41)),
            (OpenSession("project"), "open", ("project", None)),
            (
                OpenSession(
                    "project",
                    UnownedTabsAction.SAVE_SEPARATELY,
                    "Named scratch",
                ),
                "open",
                (
                    "project",
                    UnownedTabsDecision(
                        UnownedTabsAction.SAVE_SEPARATELY,
                        "Named scratch",
                    ),
                ),
            ),
        )
        for command, method_name, arguments in cases:
            service, raw = _service_mock(stored)
            operation = getattr(raw, method_name)
            operation.return_value = stored
            output = io.StringIO()
            with self.subTest(command=type(command).__name__), redirect_stdout(output):
                self.assertEqual(_run_membership(command, service), 0)
            operation.assert_called_once_with(*arguments)
            self.assertEqual(output.getvalue(), "project\n")

        service, _ = _service_mock(stored)
        with self.assertRaisesRegex(
            KiSeshError,
            "--unowned-name requires --unowned-tabs save-separately",
        ):
            _run_membership(OpenSession("project", None, "invalid"), service)

    def test_lifecycle_commands_route_and_print_stable_results(self) -> None:
        """Route all stored-session and reversible-tab lifecycle operations."""
        stored = _stored()
        cases = (
            (RenameSession("project", "Renamed"), "rename", ("project", "Renamed"), "project"),
            (ArchiveSession("project"), "archive", ("project",), "project"),
            (UnarchiveSession("project"), "unarchive", ("project",), "project"),
            (RemoveSession("project"), "remove", ("project",), "/tmp/trash/project"),
        )
        for command, method_name, arguments, expected in cases:
            service, raw = _service_mock(stored)
            operation = getattr(raw, method_name)
            operation.return_value = (
                Path("/tmp/trash/project") if isinstance(command, RemoveSession) else stored
            )
            output = io.StringIO()
            with self.subTest(command=type(command).__name__), redirect_stdout(output):
                self.assertEqual(_run_lifecycle(command, service), 0)
            operation.assert_called_once_with(*arguments)
            self.assertEqual(output.getvalue(), f"{expected}\n")

    def test_watcher_payload_validation_accepts_current_close_format(self) -> None:
        """Normalize event objects and complete synchronous close captures."""
        event = {
            "window_id": 99,
            "command": ["pwd"],
            "completed_at": "2026-08-04T11:31:00Z",
        }
        close = {
            "tab_index": 0,
            "pane_index": 1,
            "window": {"id": 99, "cwd": "/tmp/project"},
            "terminal_history": "pwd\n/tmp/project\n",
            "alternate_screen_text": "TOP FRAME\n",
            "last_command_output": "/tmp/project\n",
            "command_events": [event],
        }

        normalized = _normalized_events([event, {"window_id": 99}])
        self.assertEqual([item["command"] for item in normalized], ["pwd"])
        self.assertIsNone(_closing_capture(None))
        self.assertEqual(_closing_capture(close), cast(ClosingPaneCapture, _closing_capture(close)))

        with mock.patch("sys.stdin", _stdin_payload({"command_events": [event]})):
            events, closing = _autosave_payload_from_stdin(True)
        self.assertEqual([item["command"] for item in events], ["pwd"])
        self.assertIsNone(closing)

        with mock.patch("sys.stdin", _stdin_payload({"closing_pane": close})):
            events, closing = _autosave_payload_from_stdin(True)
        self.assertEqual(events, [])
        self.assertIsNotNone(closing)
        self.assertEqual(cast(ClosingPaneCapture, closing)["window"]["id"], 99)
        self.assertEqual(_autosave_payload_from_stdin(False), ([], None))

    def test_watcher_payload_validation_rejects_each_malformed_boundary(self) -> None:
        """Reject wrong container types, missing pane fields, and boolean indexes."""
        for payload in ("event", ["event"], {"command": "pwd"}):
            with (
                self.subTest(events=payload),
                self.assertRaisesRegex(
                    ValueError,
                    INVALID_EVENTS_MESSAGE,
                ),
            ):
                _normalized_events(payload)

        for close_payload in ("close", {}, {"window": {"id": True}}):
            with (
                self.subTest(close=close_payload),
                self.assertRaisesRegex(
                    ValueError,
                    INVALID_CLOSE_MESSAGE,
                ),
            ):
                _closing_capture(close_payload)

        with (
            mock.patch("sys.stdin", _stdin_payload("payload")),
            self.assertRaisesRegex(
                ValueError,
                INVALID_PAYLOAD_MESSAGE,
            ),
        ):
            _autosave_payload_from_stdin(True)

    def test_maintenance_commands_cover_autosave_close_and_doctor(self) -> None:
        """Exercise every maintenance path with observable service calls and status."""
        stored = _stored()
        service, raw = _service_mock(stored)
        with mock.patch("sys.stdin", _stdin_payload({"command_events": []})):
            self.assertEqual(_run_maintenance(AutosaveSession("session-id", True), service), 0)
        raw.save.assert_called_once_with("session-id", [])

        close = cast(
            ClosingPaneCapture,
            {
                "tab_index": 0,
                "pane_index": 0,
                "window": {"id": 99},
                "terminal_history": "history",
                "alternate_screen_text": "",
                "last_command_output": "output",
                "command_events": [],
            },
        )
        with mock.patch(
            "kisesh.cli._autosave_payload_from_stdin",
            return_value=([], close),
        ):
            self.assertEqual(_run_maintenance(AutosaveSession("session-id", True), service), 0)
        raw.save_closing_pane.assert_called_once_with("session-id", close)

        for findings, expected in ((["OK storage"], 0), (["ERROR broken"], 1)):
            raw.doctor.return_value = findings
            stream = io.StringIO()
            with self.subTest(findings=findings), redirect_stdout(stream):
                self.assertEqual(_run_maintenance(Doctor(), service), expected)
            self.assertEqual(stream.getvalue(), "\n".join(findings) + "\n")

    def test_dispatch_selects_each_cohesive_command_family(self) -> None:
        """Keep the top-level dispatcher exhaustive over the typed command union."""
        service, _ = _service_mock()
        cases = (
            (Manager(), "_run_manager", 11),
            (ListSessions(), "_run_read", 12),
            (CreateSession("Project"), "_run_membership", 13),
            (ArchiveSession("project"), "_run_lifecycle", 14),
            (Doctor(), "_run_maintenance", 15),
            (InstallIntegration(), "_run_integration", 16),
        )
        for command, function_name, result in cases:
            config = CliConfig(command)
            with (
                self.subTest(command=type(command).__name__),
                mock.patch(
                    f"kisesh.cli.{function_name}",
                    return_value=result,
                ) as runner,
            ):
                self.assertEqual(_dispatch(config, service), result)
            if isinstance(command, Manager):
                runner.assert_called_once_with(service)
            elif isinstance(command, InstallIntegration):
                runner.assert_called_once_with(command)
            else:
                runner.assert_called_once_with(command, service)

    def test_integration_commands_map_to_reversible_typed_installer_actions(self) -> None:
        """Keep install, disable, uninstall, and purge available from packaged wheels."""
        config = Path("/tmp/kitty.conf")
        cases = (
            (InstallIntegration(config), {"enable": True}),
            (DisableIntegration(config), {"disable": True}),
            (UninstallIntegration(config), {"uninstall": True, "purge": False}),
            (UninstallIntegration(config, purge=True), {"uninstall": False, "purge": True}),
        )
        for command, expected in cases:
            with (
                self.subTest(command=command),
                mock.patch("kisesh.installer.run", return_value=19) as install,
            ):
                self.assertEqual(_run_integration(command), 19)
            arguments = install.call_args.args[0]
            self.assertEqual(arguments.kitty_config, config)
            for field, value in expected.items():
                self.assertEqual(getattr(arguments, field), value)

    def test_manager_uses_panel_dismissal_only_for_the_resident_surface(self) -> None:
        """Keep normal overlays self-contained while resident panels hide on dismissal."""
        service, _ = _service_mock()
        for is_panel, expects_dismissal in ((False, False), (True, True)):
            with (
                self.subTest(is_panel=is_panel),
                mock.patch("kisesh.cli.is_panel_process", return_value=is_panel),
                mock.patch("kisesh.cli.expand_manager_surface") as expand,
                mock.patch("kisesh.cli.restore_manager_surface") as restore,
                mock.patch("kisesh.cli.SessionManager") as manager,
            ):
                surface = object()
                expand.return_value = surface
                manager.return_value.run.return_value = 9
                self.assertEqual(_run_manager(service), 9)

            expand.assert_called_once_with(service.kitty)
            restore.assert_called_once_with(surface)
            dismiss = manager.call_args.kwargs["on_dismiss"]
            self.assertEqual(dismiss is not None, expects_dismissal)

    def test_manager_restores_its_tab_when_curses_raises(self) -> None:
        """Guarantee normal process cleanup even when rendering aborts unexpectedly."""
        service, _ = _service_mock()
        surface = object()
        with (
            mock.patch("kisesh.cli.expand_manager_surface", return_value=surface),
            mock.patch("kisesh.cli.restore_manager_surface") as restore,
            mock.patch("kisesh.cli.is_panel_process", return_value=False),
            mock.patch("kisesh.cli.SessionManager") as manager,
            self.assertRaisesRegex(RuntimeError, "curses failed"),
        ):
            manager.return_value.run.side_effect = RuntimeError("curses failed")
            _run_manager(service)

        restore.assert_called_once_with(surface)

    def test_main_returns_dispatch_status_and_formats_expected_errors(self) -> None:
        """Translate operational failures to one concise stderr line without traceback."""
        config = CliConfig(ListSessions())
        service, _ = _service_mock()
        with (
            mock.patch("kisesh.cli.parse_arguments", return_value=config),
            mock.patch("kisesh.cli._service", return_value=service),
            mock.patch("kisesh.cli._dispatch", return_value=7),
        ):
            self.assertEqual(main([]), 7)

        stderr = io.StringIO()
        with (
            mock.patch("kisesh.cli.parse_arguments", return_value=config),
            mock.patch("kisesh.cli._service", side_effect=KiSeshError("not live")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main([]), 1)
        self.assertEqual(stderr.getvalue(), "kisesh: not live\n")

        integration = CliConfig(InstallIntegration())
        with (
            mock.patch("kisesh.cli.parse_arguments", return_value=integration),
            mock.patch("kisesh.cli._run_integration", return_value=8) as install,
            mock.patch("kisesh.cli._service") as service_factory,
        ):
            self.assertEqual(main([]), 8)
        install.assert_called_once_with(integration.command)
        service_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
