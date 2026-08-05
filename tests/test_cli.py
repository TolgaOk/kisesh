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

from kitty_workbench.cli import (
    INVALID_CLOSE_MESSAGE,
    INVALID_EVENTS_MESSAGE,
    INVALID_PAYLOAD_MESSAGE,
    AddTab,
    ArchiveSession,
    AutosaveSession,
    CliConfig,
    CopyTab,
    CreateSession,
    DetachTab,
    Doctor,
    ListSessions,
    Manager,
    OpenSession,
    PrintLastOutput,
    RemoveSession,
    RenameSession,
    RestoreShell,
    SaveSession,
    ShowContext,
    UnarchiveSession,
    _autosave_payload_from_stdin,
    _closing_capture,
    _dispatch,
    _needs_kitty,
    _normalized_events,
    _run_lifecycle,
    _run_maintenance,
    _run_manager,
    _run_membership,
    _run_read,
    _service,
    main,
    parse_arguments,
)
from kitty_workbench.context import build_context
from kitty_workbench.domain import ClosingPaneCapture
from kitty_workbench.kitty_client import LiveTab
from kitty_workbench.model import SessionManifest
from kitty_workbench.service import SessionView, WorkbenchError, WorkbenchService
from kitty_workbench.store import StoredSession


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


def _service_mock(stored: StoredSession | None = None) -> tuple[WorkbenchService, mock.MagicMock]:
    """Return a typed service view and its configurable autospecced mock."""
    raw = mock.create_autospec(WorkbenchService, instance=True)
    raw.store = mock.MagicMock()
    raw.store.get.return_value = stored or _stored()
    return cast(WorkbenchService, raw), raw


def _stdin_payload(payload: object) -> io.StringIO:
    """Encode one watcher payload as a readable standard-input stream."""
    return io.StringIO(json.dumps(payload))


class CliTests(unittest.TestCase):
    """Cover every typed command family and payload compatibility path."""

    def test_tyro_parses_aliases_and_cascaded_global_options(self) -> None:
        """Accept concise subcommands with global options on either side."""
        before = parse_arguments(["--socket", "unix:/tmp/kitty", "add-tab", "project"])
        after = parse_arguments(["add-tab", "project", "--data-dir", "/tmp/workbench"])
        remove_alias = parse_arguments(["trash", "project"])

        self.assertIsInstance(before.command, AddTab)
        self.assertEqual(before.socket, "unix:/tmp/kitty")
        self.assertIsInstance(after.command, AddTab)
        self.assertEqual(after.data_dir, Path("/tmp/workbench"))
        self.assertIsInstance(remove_alias.command, RemoveSession)

    def test_only_live_operations_construct_an_eager_kitty_client(self) -> None:
        """Keep stored-context reads offline unless a connection override is explicit."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            offline = CliConfig(ListSessions(), data_dir=root / "data")
            connected = CliConfig(
                ListSessions(),
                data_dir=root / "data",
                socket="unix:/tmp/kitty",
            )
            live = CliConfig(Manager(), data_dir=root / "data")
            with mock.patch("kitty_workbench.cli.KittyClient") as client:
                offline_service = _service(offline)
                connected_service = _service(connected)
                live_service = _service(live)

        self.assertFalse(_needs_kitty(ListSessions()))
        self.assertTrue(_needs_kitty(Manager()))
        self.assertIsNone(offline_service.kitty)
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
                "kitty_workbench.cli.run_restored_shell",
                side_effect=ShellReplaced,
            ) as restore,
            self.assertRaises(ShellReplaced),
        ):
            _run_read(RestoreShell(stored.manifest.id, 0, 0), service)
        restore.assert_called_once_with(stored, raw.context.return_value, 0, 0)

    def test_membership_commands_route_arguments_and_print_the_result_slug(self) -> None:
        """Route create, attach, detach, copy, save, and open operations exactly once."""
        stored = _stored()
        cases = (
            (CreateSession("Project", "/tmp/root"), "create_from_active", ("Project", "/tmp/root")),
            (AddTab("project"), "add_current_tab", ("project",)),
            (DetachTab("project"), "detach_current_tab", ("project",)),
            (CopyTab("project"), "copy_current_tab", ("project",)),
            (SaveSession("project"), "save", ("project",)),
            (SaveSession(), "save_current", ()),
            (OpenSession("project"), "open", ("project",)),
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

    def test_watcher_payload_validation_accepts_legacy_and_close_formats(self) -> None:
        """Normalize legacy event lists and complete synchronous close captures."""
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

        with mock.patch("sys.stdin", _stdin_payload([event])):
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
            "kitty_workbench.cli._autosave_payload_from_stdin",
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
        )
        for command, function_name, result in cases:
            config = CliConfig(command)
            with (
                self.subTest(command=type(command).__name__),
                mock.patch(
                    f"kitty_workbench.cli.{function_name}",
                    return_value=result,
                ) as runner,
            ):
                self.assertEqual(_dispatch(config, service), result)
            if isinstance(command, Manager):
                runner.assert_called_once_with(service)
            else:
                runner.assert_called_once_with(command, service)

    def test_manager_uses_panel_dismissal_only_for_the_resident_surface(self) -> None:
        """Keep normal overlays self-contained while resident panels hide on dismissal."""
        service, _ = _service_mock()
        for is_panel, expects_dismissal in ((False, False), (True, True)):
            with (
                self.subTest(is_panel=is_panel),
                mock.patch("kitty_workbench.cli.is_panel_process", return_value=is_panel),
                mock.patch("kitty_workbench.cli.SessionManager") as manager,
            ):
                manager.return_value.run.return_value = 9
                self.assertEqual(_run_manager(service), 9)

            dismiss = manager.call_args.kwargs["on_dismiss"]
            self.assertEqual(dismiss is not None, expects_dismissal)

    def test_main_returns_dispatch_status_and_formats_expected_errors(self) -> None:
        """Translate operational failures to one concise stderr line without traceback."""
        config = CliConfig(ListSessions())
        service, _ = _service_mock()
        with (
            mock.patch("kitty_workbench.cli.parse_arguments", return_value=config),
            mock.patch("kitty_workbench.cli._service", return_value=service),
            mock.patch("kitty_workbench.cli._dispatch", return_value=7),
        ):
            self.assertEqual(main([]), 7)

        stderr = io.StringIO()
        with (
            mock.patch("kitty_workbench.cli.parse_arguments", return_value=config),
            mock.patch("kitty_workbench.cli._service", side_effect=WorkbenchError("not live")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main([]), 1)
        self.assertEqual(stderr.getvalue(), "kitty-workbench: not live\n")


if __name__ == "__main__":
    unittest.main()
