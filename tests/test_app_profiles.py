"""Behavioral and boundary tests for configurable application profiles."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from kisesh import app_profiles, session_bar
from kisesh.app_profiles import (
    DEFAULT_APP_PROFILES,
    AppProfileError,
    CapturedRestore,
    ConfiguredRestore,
    IgnoreRestore,
    PrefillRestore,
    ResumeRestore,
    app_config_path,
    current_app_profiles,
    load_app_profiles,
    parse_app_profiles,
    refresh_app_profiles,
)
from kisesh.context import build_context
from kisesh.kitty_client import LiveTab
from kisesh.model import KittyWindow, SessionManifest
from kisesh.preview import build_session_preview
from kisesh.service import SessionView
from kisesh.session_bar import SessionBarTab, render_tab_label
from kisesh.store import StoredSession

PROJECT = Path(__file__).parents[1]

CUSTOM_CONFIG = """\
version = 2

[defaults]
restore = "prefill"
label = "Unknown"
icon = "?"

[agents.claude]
match = ["claude", "claude-*"]
restore = "resume"
adapter = "claude"
label = "Claude Custom"
icon = "C"

[apps.custom]
match = ["custom", "custom-*"]
restore = "configured"
argv = ["custom", "--restore", "workspace"]
label = "Custom App"
icon = "H"

[apps.capture]
match = ["capture"]
restore = "captured"
label = "Capture"
icon = "X"

[apps.note]
match = ["note"]
restore = "prefill"
label = "Note"
icon = "N"

[apps.ignored]
match = ["ignored"]
restore = "ignore"
label = "Ignored"
icon = "I"
"""


def _document(apps: str, *, defaults: str | None = None, version: str = "2") -> str:
    """Build one complete TOML document around a focused validation case."""
    default_table = defaults or 'restore = "prefill"\nlabel = "App"\nicon = "?"'
    return f"version = {version}\n\n[defaults]\n{default_table}\n\n{apps}"


def _window(window_id: int, *argv: str) -> KittyWindow:
    """Create one representative foreground application pane."""
    return {
        "id": window_id,
        "title": argv[0],
        "cwd": "/tmp/project",
        "foreground_processes": [{"cmdline": list(argv)}],
        "at_prompt": False,
    }


class AppProfileBehaviorTests(unittest.TestCase):
    """Exercise profiles through capture, restore, preview, and native rendering."""

    def test_bundled_profiles_cover_apps_and_resumable_agents(self) -> None:
        """Ship a populated, ordered config with safe unmatched behavior."""
        codex = DEFAULT_APP_PROFILES.match("/opt/homebrew/bin/CODEX-nightly")
        pi = DEFAULT_APP_PROFILES.named("pi")
        top = DEFAULT_APP_PROFILES.named("TOP")
        neovim = DEFAULT_APP_PROFILES.match("/usr/bin/nvim")
        vim = DEFAULT_APP_PROFILES.match("vi")

        self.assertIsInstance(DEFAULT_APP_PROFILES.defaults.restore, PrefillRestore)
        self.assertEqual(DEFAULT_APP_PROFILES.defaults.icon, "")
        self.assertIsNotNone(codex)
        self.assertEqual(codex.icon if codex is not None else None, "󰋙")
        self.assertEqual(codex.kind if codex is not None else None, "agent")
        self.assertEqual(pi.kind if pi is not None else None, "agent")
        self.assertEqual(pi.icon if pi is not None else None, "π")
        self.assertEqual(
            pi.restore.adapter
            if pi is not None and isinstance(pi.restore, ResumeRestore)
            else None,
            "pi",
        )
        self.assertIsNotNone(top)
        self.assertIsInstance(top.restore if top is not None else None, ConfiguredRestore)
        configured = top.restore if top is not None else None
        self.assertEqual(
            configured.argv if isinstance(configured, ConfiguredRestore) else (), ("top",)
        )
        self.assertEqual(
            (neovim.name, neovim.label, neovim.icon) if neovim is not None else None,
            ("nvim", "Neovim", ""),
        )
        self.assertEqual(
            (vim.name, vim.label, vim.icon) if vim is not None else None,
            ("vim", "Vim", ""),
        )
        self.assertIsNone(DEFAULT_APP_PROFILES.match(None))
        self.assertIsNone(DEFAULT_APP_PROFILES.match("python"))
        self.assertIsNone(DEFAULT_APP_PROFILES.named(None))

    def test_one_custom_config_drives_restore_safety_preview_and_top_bar_icons(self) -> None:
        """Use configured modes and presentation consistently across product surfaces."""
        profiles = parse_app_profiles(CUSTOM_CONFIG, source="custom.toml")
        windows = [
            _window(1, "/opt/bin/claude-nightly", "--resume", "abc"),
            _window(2, "custom-beta", "--unsafe-live-flag"),
            _window(3, "capture", "--project", "one"),
            _window(4, "note", "draft.md"),
            _window(5, "ignored", "--watch"),
            _window(6, "python", "server.py"),
        ]
        tab = LiveTab(1, 7, 0, "Applications", "splits", windows, is_focused=True)

        context = build_context([tab], profiles=profiles)
        panes = context["tabs"][0]["panes"]
        restores = [pane["restore"] for pane in panes]
        self.assertTrue(all(restore is not None for restore in (*restores[:4], restores[5])))
        claude_restore = restores[0]
        configured_restore = restores[1]
        captured_restore = restores[2]
        prefilled_restore = restores[3]
        unknown_restore = restores[5]
        assert claude_restore is not None
        assert configured_restore is not None
        assert captured_restore is not None
        assert prefilled_restore is not None
        assert unknown_restore is not None

        self.assertEqual(claude_restore["argv"], ["claude", "--resume", "abc"])
        self.assertTrue(claude_restore["auto_run"])
        self.assertEqual(panes[0]["agent"], "claude")
        self.assertEqual(
            configured_restore["argv"],
            ["custom", "--restore", "workspace"],
        )
        self.assertEqual(captured_restore["argv"], ["capture", "--project", "one"])
        self.assertFalse(prefilled_restore["auto_run"])
        self.assertIsNone(restores[4])
        self.assertEqual(unknown_restore["argv"], ["python", "server.py"])
        self.assertFalse(unknown_restore["auto_run"])

        stored = StoredSession(
            SessionManifest(name="Apps", slug="apps", project_root="/tmp/project"),
            Path("/tmp/apps"),
        )
        preview = build_session_preview(SessionView(stored, [], context), profiles)
        self.assertEqual(
            [(pane.icon, pane.label) for pane in preview.tabs[0].panes],
            [
                ("C", "Claude Custom"),
                ("H", "Custom App"),
                ("X", "Capture"),
                ("N", "Note"),
                ("I", "Ignored"),
                ("?", "python"),
            ],
        )

        with mock.patch.object(session_bar, "current_app_profiles", return_value=profiles):
            rendered = render_tab_label(
                SessionBarTab(
                    "work",
                    "session",
                    "Project",
                    "custom",
                ),
                None,
                80,
            )
        self.assertEqual(rendered, " Project │ H Custom App")

    def test_xdg_user_config_can_change_icons_and_restore_policy_without_code_edits(self) -> None:
        """Reload an edited standard config only at an explicit event boundary."""
        with tempfile.TemporaryDirectory() as temporary:
            config_home = Path(temporary)
            config = config_home / "kisesh" / "apps.toml"
            config.parent.mkdir()
            config.write_text(CUSTOM_CONFIG, encoding="utf-8")
            with (
                mock.patch.dict(
                    "os.environ",
                    {"XDG_CONFIG_HOME": str(config_home)},
                    clear=True,
                ),
                mock.patch.object(app_profiles, "_current_profiles", None),
            ):
                first = current_app_profiles()
                config.write_text(
                    CUSTOM_CONFIG.replace('icon = "H"', 'icon = "Z"'),
                    encoding="utf-8",
                )
                cached = current_app_profiles()
                refreshed = refresh_app_profiles()

        self.assertIs(first, cached)
        first_custom = first.named("custom")
        refreshed_custom = refreshed.named("custom")
        self.assertIsNotNone(first_custom)
        self.assertIsNotNone(refreshed_custom)
        self.assertEqual(first_custom.icon if first_custom is not None else None, "H")
        self.assertEqual(
            refreshed_custom.icon if refreshed_custom is not None else None,
            "Z",
        )


class AppProfileBoundaryTests(unittest.TestCase):
    """Cover malformed files and every path-resolution and cache boundary."""

    def test_closed_parser_types_reject_unrecognized_runtime_variants(self) -> None:
        """Fail closed if an internal parser contract returns an unknown variant."""
        unknown_mode = cast(app_profiles.RestoreMode, "unknown")
        with mock.patch.object(app_profiles, "_restore_mode", return_value=unknown_mode):
            for parser in (
                app_profiles._app_restore,
                app_profiles._agent_restore,
                app_profiles._v1_restore,
            ):
                with self.subTest(parser=parser.__name__), self.assertRaises(AssertionError):
                    parser({}, "profiles.invalid")

        defaults = {"restore": "prefill", "label": "App", "icon": "?"}
        unknown_document = cast(
            app_profiles._SchemaV1 | app_profiles._SchemaV2,
            SimpleNamespace(defaults=defaults),
        )
        with (
            mock.patch.object(app_profiles, "_schema_document", return_value=unknown_document),
            self.assertRaises(AssertionError),
        ):
            parse_app_profiles("version = 2")

    def test_path_precedence_and_loader_fallback_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            explicit = root / "explicit.toml"
            environment = root / "environment.toml"
            xdg = root / "xdg"
            explicit.write_text(CUSTOM_CONFIG, encoding="utf-8")
            environment.write_text(CUSTOM_CONFIG, encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {
                    "KISESH_APP_CONFIG": str(environment),
                    "XDG_CONFIG_HOME": str(xdg),
                },
                clear=True,
            ):
                self.assertEqual(app_config_path(explicit), explicit)
                self.assertEqual(app_config_path(), environment)
                custom = load_app_profiles(explicit).named("custom")
                self.assertIsNotNone(custom)
                self.assertEqual(custom.icon if custom is not None else None, "H")

            with mock.patch.dict(
                "os.environ",
                {"XDG_CONFIG_HOME": str(xdg)},
                clear=True,
            ):
                self.assertEqual(
                    app_config_path(),
                    xdg / "kisesh" / "apps.toml",
                )
                self.assertEqual(load_app_profiles(), DEFAULT_APP_PROFILES)

            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch("pathlib.Path.expanduser", return_value=root / "home-config"),
            ):
                self.assertEqual(
                    app_config_path(),
                    root / "home-config" / "kisesh" / "apps.toml",
                )

            with self.assertRaisesRegex(AppProfileError, "does not exist"):
                load_app_profiles(root / "missing.toml")

            directory = root / "directory.toml"
            directory.mkdir()
            with self.assertRaisesRegex(AppProfileError, "is not a file"):
                load_app_profiles(directory)

            with (
                mock.patch.object(Path, "is_file", return_value=True),
                mock.patch.object(Path, "read_text", side_effect=OSError("denied")),
                self.assertRaisesRegex(AppProfileError, "cannot read"),
            ):
                load_app_profiles(root / "unreadable.toml")

    def test_invalid_toml_documents_fail_with_focused_validation_errors(self) -> None:
        valid_app = (
            '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\nlabel = "Tool"\nicon = "T"\n'
        )
        cases = {
            "invalid TOML": "version = [",
            "version must be 1 or 2": _document(valid_app, version="true"),
            "unknown field": _document(valid_app) + "\nunexpected = true\n",
            "defaults must be a TOML table": "version = 1\napps = {}\n",
            "defaults has unknown field": _document(
                valid_app,
                defaults='restore = "prefill"\nlabel = "App"\nicon = "?"\nextra = 1',
            ),
            "defaults.restore": _document(
                valid_app,
                defaults='restore = "captured"\nlabel = "App"\nicon = "?"',
            ),
            "defaults.label": _document(
                valid_app,
                defaults='restore = "prefill"\nlabel = ""\nicon = "?"',
            ),
            "unsupported display text": _document(
                valid_app,
                defaults='restore = "prefill"\nlabel = "\\u0007"\nicon = "?"',
            ),
            "apps must be a TOML table": (
                'version = 1\napps = "wrong"\n'
                '[defaults]\nrestore = "prefill"\nlabel = "App"\nicon = "?"\n'
            ),
            "invalid app profile name": _document(
                '[apps."Bad Name"]\nmatch = ["tool"]\nrestore = "captured"\n'
            ),
            "invalid app profile name: 'Tool'": _document(
                '[apps.Tool]\nmatch = ["tool"]\nrestore = "captured"\n'
            ),
            "unknown field: extra": _document(valid_app + "extra = true\n"),
            "apps.tool.restore": _document('[apps.tool]\nmatch = ["tool"]\n'),
            "cannot be resume": _document('[apps.tool]\nmatch = ["tool"]\nrestore = "resume"\n'),
            "agents.tool.adapter": _document(
                '[agents.tool]\nmatch = ["tool"]\nrestore = "resume"\nadapter = "other"\n'
            ),
            "adapter is required": _document(
                '[agents.tool]\nmatch = ["tool"]\nrestore = "resume"\n'
            ),
            "adapter is only valid": _document(
                '[agents.tool]\nmatch = ["tool"]\nrestore = "captured"\nadapter = "claude"\n'
            ),
            "argv is required": _document(
                '[apps.tool]\nmatch = ["tool"]\nrestore = "configured"\n'
            ),
            "argv is only valid": _document(
                '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\nargv = ["tool"]\n'
            ),
            "unknown field: agent": _document(
                '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\nagent = true\n'
            ),
            "cannot be configured": _document(
                '[agents.tool]\nmatch = ["tool"]\nrestore = "configured"\n'
            ),
            "nonempty bounded string array": _document(
                '[apps.tool]\nmatch = []\nrestore = "captured"\n'
            ),
            "invalid string": _document('[apps.tool]\nmatch = [1]\nrestore = "captured"\n'),
            "executable basenames only": _document(
                '[apps.tool]\nmatch = ["/usr/bin/tool"]\nrestore = "captured"\n'
            ),
            "duplicate match": _document(
                '[apps.one]\nmatch = ["Tool"]\nrestore = "captured"\n'
                '[apps.two]\nmatch = ["tool"]\nrestore = "captured"\n'
            ),
            "duplicate profile name": _document(
                '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\n'
                '[agents.tool]\nmatch = ["agent"]\nrestore = "captured"\n'
            ),
            "invalid agent profile name": _document(
                '[agents."Bad Name"]\nmatch = ["agent"]\nrestore = "captured"\n'
            ),
            "agents must be a TOML table": (
                'version = 2\nagents = "wrong"\n'
                '[defaults]\nrestore = "prefill"\nlabel = "App"\nicon = "?"\n'
            ),
        }
        for message, content in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(AppProfileError, message):
                parse_app_profiles(content, source="broken.toml")

    def test_private_value_boundaries_reject_non_toml_and_oversized_values(self) -> None:
        with self.assertRaisesRegex(AppProfileError, "must be a TOML table"):
            app_profiles._table({1: "value"}, "table")
        invalid_sequences: tuple[object, ...] = (
            "scalar",
            [],
            ["x"] * 33,
            [""],
            ["x" * 129],
            ["line\nbreak"],
        )
        for value in invalid_sequences:
            with self.subTest(value=type(value).__name__), self.assertRaises(AppProfileError):
                app_profiles._string_sequence(
                    value,
                    "items",
                    maximum_items=32,
                    maximum_length=128,
                )
        for pattern in (" tool", "tool ", "tool name", r"bin\tool"):
            with (
                self.subTest(pattern=pattern),
                self.assertRaisesRegex(
                    AppProfileError,
                    "executable basenames only",
                ),
            ):
                app_profiles._match_patterns([pattern], "patterns")
        with self.assertRaisesRegex(AppProfileError, "unsupported display text"):
            app_profiles._display_text("x" * 65, "label", maximum=64)
        with self.assertRaisesRegex(AppProfileError, "invalid app profile name"):
            parse_app_profiles(_document('[apps.""]\nmatch = ["x"]\nrestore = "captured"\n'))

    def test_version_one_profiles_remain_readable_without_weakening_version_two(self) -> None:
        """Normalize version-one agents while keeping version-two namespaces strict."""
        version_one = _document(
            '[apps.claude]\nmatch = ["claude"]\nrestore = "resume"\n'
            'adapter = "claude"\nagent = true\n'
            '[apps.nvim]\nmatch = ["nvim"]\nrestore = "captured"\n'
            '[apps.top]\nmatch = ["top"]\nrestore = "configured"\nargv = ["top"]\n'
            '[apps.note]\nmatch = ["note"]\nrestore = "prefill"\n'
            '[apps.ignored]\nmatch = ["ignored"]\nrestore = "ignore"\n',
            version="1",
        )
        profiles = parse_app_profiles(version_one)

        claude = profiles.named("claude")
        nvim = profiles.named("nvim")
        top = profiles.named("top")
        note = profiles.named("note")
        ignored = profiles.named("ignored")
        self.assertEqual(claude.kind if claude is not None else None, "agent")
        self.assertEqual(nvim.kind if nvim is not None else None, "app")
        self.assertIsInstance(nvim.restore if nvim is not None else None, CapturedRestore)
        self.assertIsInstance(top.restore if top is not None else None, ConfiguredRestore)
        self.assertIsInstance(note.restore if note is not None else None, PrefillRestore)
        self.assertIsInstance(ignored.restore if ignored is not None else None, IgnoreRestore)

        invalid_v1_profiles = (
            (
                "argv is only valid",
                '[apps.tool]\nmatch = ["tool"]\nrestore = "resume"\n'
                'adapter = "claude"\nargv = ["tool"]\n',
            ),
            ("adapter is required", '[apps.tool]\nmatch = ["tool"]\nrestore = "resume"\n'),
            (
                "adapter is only valid",
                '[apps.tool]\nmatch = ["tool"]\nrestore = "configured"\n'
                'adapter = "claude"\nargv = ["tool"]\n',
            ),
            (
                "argv is required",
                '[apps.tool]\nmatch = ["tool"]\nrestore = "configured"\n',
            ),
            (
                "adapter is only valid",
                '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\nadapter = "claude"\n',
            ),
            (
                "argv is only valid",
                '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\nargv = ["tool"]\n',
            ),
            (
                "agent must be",
                '[apps.tool]\nmatch = ["tool"]\nrestore = "captured"\nagent = "yes"\n',
            ),
        )
        for expected, profile in invalid_v1_profiles:
            with self.subTest(expected=expected), self.assertRaisesRegex(AppProfileError, expected):
                parse_app_profiles(_document(profile, version="1"))

        with self.assertRaisesRegex(AppProfileError, "unknown field"):
            parse_app_profiles(
                _document(
                    '[agents.claude]\nmatch = ["claude"]\nrestore = "resume"\n'
                    'adapter = "claude"\nagent = true\n'
                )
            )

    def test_agents_can_choose_non_resuming_restore_policies_without_adapter_state(self) -> None:
        """Represent safe agent commands without inventing a resumable session adapter."""
        profiles = parse_app_profiles(
            _document(
                '[agents.capture]\nmatch = ["capture-agent"]\nrestore = "captured"\n'
                '[agents.note]\nmatch = ["note-agent"]\nrestore = "prefill"\n'
                '[agents.ignored]\nmatch = ["ignored-agent"]\nrestore = "ignore"\n'
            )
        )

        capture = profiles.named("capture")
        note = profiles.named("note")
        ignored = profiles.named("ignored")
        self.assertIsInstance(capture.restore if capture is not None else None, CapturedRestore)
        self.assertIsInstance(note.restore if note is not None else None, PrefillRestore)
        self.assertIsInstance(ignored.restore if ignored is not None else None, IgnoreRestore)
        self.assertTrue(
            all(
                profile is not None and profile.kind == "agent"
                for profile in (capture, note, ignored)
            )
        )

    def test_agent_profiles_take_precedence_over_broad_regular_app_patterns(self) -> None:
        """Keep agent identity and resume semantics when a generic app glob also matches."""
        profiles = parse_app_profiles(
            _document(
                '[apps.command]\nmatch = ["*"]\nrestore = "captured"\n'
                '[agents.claude]\nmatch = ["claude"]\nrestore = "resume"\n'
                'adapter = "claude"\n'
            )
        )

        claude = profiles.match("claude")
        shell_command = profiles.match("rg")
        self.assertEqual(claude.kind if claude is not None else None, "agent")
        self.assertIsInstance(claude.restore if claude is not None else None, ResumeRestore)
        self.assertEqual(shell_command.kind if shell_command is not None else None, "app")
        self.assertIsInstance(
            shell_command.restore if shell_command is not None else None,
            CapturedRestore,
        )

    def test_process_cache_falls_back_on_bad_user_edits_and_refreshes_on_demand(self) -> None:
        custom = parse_app_profiles(CUSTOM_CONFIG)
        with (
            mock.patch.object(app_profiles, "_current_profiles", None),
            mock.patch.object(app_profiles, "load_app_profiles", return_value=custom) as loader,
        ):
            self.assertIs(current_app_profiles(), custom)
            self.assertIs(current_app_profiles(), custom)
            loader.assert_called_once_with()

        for operation in (current_app_profiles, refresh_app_profiles):
            with (
                self.subTest(operation=operation.__name__),
                mock.patch.object(app_profiles, "_current_profiles", None),
                mock.patch.object(
                    app_profiles,
                    "load_app_profiles",
                    side_effect=AppProfileError("bad edit"),
                ),
            ):
                self.assertIs(operation(), DEFAULT_APP_PROFILES)

        signature = (Path("/tmp/apps.toml"), 123, 456)
        with (
            mock.patch.object(app_profiles, "_current_profiles", custom),
            mock.patch.object(app_profiles, "_current_signature", signature),
            mock.patch.object(app_profiles, "_profile_signature", return_value=signature),
            mock.patch.object(app_profiles, "load_app_profiles") as loader,
        ):
            self.assertIs(refresh_app_profiles(), custom)
            loader.assert_not_called()

        with mock.patch.object(Path, "stat", side_effect=OSError("unavailable")):
            self.assertIsNone(app_profiles._profile_signature())


if __name__ == "__main__":
    unittest.main()
