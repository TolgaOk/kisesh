"""Exact agent-session discovery and process-boundary tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from kisesh.agent_resume import (
    _claude_root_session,
    _codex_root_session,
    _proc_open_files,
    exact_resume_argv,
    process_open_files,
    resolve_agent_resumes,
    resume_argv_for_session,
)
from kisesh.app_profiles import DEFAULT_APP_PROFILES
from kisesh.kitty_client import LiveTab
from kisesh.model import KittyProcess, KittyWindow

CODEX_ID = "019fd808-918d-7481-b526-c4da01513c42"
CODEX_OTHER_ID = "019fd767-cf0f-7ce2-9e0a-855046828fc6"
CODEX_SUBAGENT_ID = "019fd808-91c5-7a83-8a50-16e72394cbe2"
CLAUDE_ID = "7f676817-c49e-459c-86de-17382e2170ef"


def _tab(*windows: KittyWindow) -> LiveTab:
    """Build one live tab around representative foreground processes."""
    return LiveTab(1, 7, 0, "Agents", "splits", list(windows), is_focused=True)


def _window(window_id: int, pid: int | None, *argv: str) -> KittyWindow:
    """Build one pane with an optional process identifier."""
    process: KittyProcess = {"cmdline": list(argv), "cwd": "/tmp/project"}
    if pid is not None:
        process["pid"] = pid
    return {
        "id": window_id,
        "title": argv[0],
        "cwd": "/tmp/project",
        "foreground_processes": [process],
        "at_prompt": False,
    }


def _write_codex(path: Path, session_id: str, source: object = "cli") -> None:
    """Write the metadata record used to distinguish a root Codex thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "session_meta",
        "payload": {"id": session_id, "source": source, "cwd": "/tmp/project"},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


class AgentResumeTests(unittest.TestCase):
    def test_hook_session_ids_become_only_exact_validated_resume_commands(self) -> None:
        self.assertEqual(
            resume_argv_for_session("claude", CLAUDE_ID.upper()),
            ["claude", "--resume", CLAUDE_ID],
        )
        self.assertEqual(
            resume_argv_for_session("codex", CODEX_ID.upper()),
            ["codex", "resume", CODEX_ID],
        )
        for value in (None, "", "not-a-session", True, 123):
            with self.subTest(value=value):
                self.assertIsNone(resume_argv_for_session("claude", value))

    def test_two_plain_agents_resolve_to_distinct_exact_sessions(self) -> None:
        """Model the reported work session without relying on global latest state."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "sessions" / f"rollout-now-{CODEX_ID}.jsonl"
            guardian = root / "sessions" / f"rollout-now-{CODEX_SUBAGENT_ID}.jsonl"
            claude = root / "projects" / "-tmp-project" / f"{CLAUDE_ID}.jsonl"
            claude.parent.mkdir(parents=True)
            claude.write_text("{}\n", encoding="utf-8")
            _write_codex(codex, CODEX_ID)
            _write_codex(guardian, CODEX_SUBAGENT_ID, {"subagent": {"other": "guardian"}})
            observed: list[tuple[int, ...]] = []

            def open_files(pids: Sequence[int]) -> dict[int, list[Path]]:
                """Return root and subagent files exactly as one OS lookup would."""
                observed.append(tuple(pids))
                return {101: [guardian, codex], 202: [claude]}

            resumes = resolve_agent_resumes(
                [
                    _tab(
                        _window(11, 101, "codex"),
                        _window(12, 202, "claude", "--model", "sonnet"),
                    )
                ],
                DEFAULT_APP_PROFILES,
                open_files,
            )

        self.assertEqual(observed, [(101, 202)])
        self.assertEqual(resumes[11], ["codex", "resume", CODEX_ID])
        self.assertEqual(resumes[12], ["claude", "--resume", CLAUDE_ID])

    def test_explicit_agent_ids_need_no_process_lookup_and_are_canonicalized(self) -> None:
        windows = (
            _window(1, 11, "codex", "resume", CODEX_ID.upper()),
            _window(2, 12, "claude", "--session-id", CLAUDE_ID.upper()),
            _window(3, None, "claude"),
            _window(4, 14, "nvim", "."),
            _window(5, True, "codex"),
            _window(6, 16, "claude", f"--resume={CLAUDE_ID}"),
            _window(7, 17, "claude", f"--session-id={CLAUDE_ID}"),
        )

        def unexpected(_pids: Sequence[int]) -> dict[int, list[Path]]:
            raise AssertionError("explicit resumes must not inspect process files")

        resumes = resolve_agent_resumes([_tab(*windows)], DEFAULT_APP_PROFILES, unexpected)

        self.assertEqual(resumes[1], ["codex", "resume", CODEX_ID])
        self.assertEqual(resumes[2], ["claude", "--resume", CLAUDE_ID])
        self.assertNotIn(3, resumes)
        self.assertNotIn(4, resumes)
        self.assertNotIn(5, resumes)
        self.assertEqual(resumes[6], ["claude", "--resume", CLAUDE_ID])
        self.assertEqual(resumes[7], ["claude", "--resume", CLAUDE_ID])
        self.assertEqual(
            exact_resume_argv("claude", ["claude", "--resume", CLAUDE_ID]),
            ["claude", "--resume", CLAUDE_ID],
        )
        self.assertEqual(
            exact_resume_argv("codex", ["codex", "resume", CODEX_ID]),
            ["codex", "resume", CODEX_ID],
        )

    def test_ambiguous_or_unavailable_process_state_keeps_safe_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "sessions" / f"rollout-one-{CODEX_ID}.jsonl"
            second = root / "sessions" / f"rollout-two-{CODEX_OTHER_ID}.jsonl"
            _write_codex(first, CODEX_ID)
            _write_codex(second, CODEX_OTHER_ID)
            windows = [_tab(_window(1, 10, "codex"))]
            ambiguous = resolve_agent_resumes(
                windows,
                DEFAULT_APP_PROFILES,
                lambda _pids: {10: [first, second]},
            )

        self.assertEqual(ambiguous, {})
        for failure in (OSError("denied"), subprocess.SubprocessError("failed")):
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(
                    resolve_agent_resumes(
                        windows,
                        DEFAULT_APP_PROFILES,
                        mock.Mock(side_effect=failure),
                    ),
                    {},
                )
        invalid = (
            ["claude", "--resume", "not-a-uuid"],
            ["codex", "resume", "not-a-uuid"],
            ["codex", "resume", CODEX_ID, "extra"],
        )
        self.assertTrue(all(exact_resume_argv("codex", argv) is None for argv in invalid))

    def test_transcript_validation_rejects_non_root_and_corrupt_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "sessions" / f"rollout-now-{CODEX_ID}.jsonl"
            _write_codex(valid, CODEX_ID)
            self.assertEqual(_codex_root_session(valid), CODEX_ID)

            cases = (
                root / "not-a-rollout.jsonl",
                root / f"rollout-now-{CODEX_ID}.jsonl",
                root / "sessions" / f"rollout-missing-{CODEX_OTHER_ID}.jsonl",
            )
            self.assertTrue(all(_codex_root_session(path) is None for path in cases))

            corrupt = root / "sessions" / f"rollout-corrupt-{CODEX_OTHER_ID}.jsonl"
            corrupt.write_bytes(b"\xff\n")
            self.assertIsNone(_codex_root_session(corrupt))
            for index, record in enumerate(
                (
                    [],
                    {"type": "event", "payload": {}},
                    {"type": "session_meta", "payload": []},
                    {"type": "session_meta", "payload": {"id": 123}},
                    {"type": "session_meta", "payload": {"id": CODEX_ID}},
                )
            ):
                malformed = root / "sessions" / f"rollout-{index}-{CODEX_OTHER_ID}.jsonl"
                malformed.write_text(json.dumps(record) + "\n", encoding="utf-8")
                self.assertIsNone(_codex_root_session(malformed))

            claude = root / "projects" / "encoded" / f"{CLAUDE_ID}.jsonl"
            claude.parent.mkdir(parents=True)
            claude.touch()
            nested = claude.parent / CLAUDE_ID / "subagents" / f"{CLAUDE_ID}.jsonl"
            nested.parent.mkdir(parents=True)
            nested.touch()
            self.assertEqual(_claude_root_session(claude), CLAUDE_ID)
            self.assertIsNone(_claude_root_session(nested))
            self.assertIsNone(_claude_root_session(root / "invalid.jsonl"))


class ProcessOpenFilesTests(unittest.TestCase):
    def test_real_child_open_files_resolve_root_agents_without_terminal_output(self) -> None:
        """Exercise the host process boundary with main and subagent transcripts open."""
        if not Path("/proc").is_dir() and shutil.which("lsof") is None:
            self.skipTest("the host exposes neither procfs nor lsof")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "sessions" / f"rollout-now-{CODEX_ID}.jsonl"
            guardian = root / "sessions" / f"rollout-now-{CODEX_SUBAGENT_ID}.jsonl"
            claude = root / "projects" / "-tmp-project" / f"{CLAUDE_ID}.jsonl"
            claude.parent.mkdir(parents=True)
            claude.write_text("{}\n", encoding="utf-8")
            _write_codex(codex, CODEX_ID)
            _write_codex(guardian, CODEX_SUBAGENT_ID, {"subagent": {"other": "guardian"}})
            helper = (
                "import sys\n"
                "files = [open(path, encoding='utf-8') for path in sys.argv[1:]]\n"
                "print('ready', flush=True)\n"
                "sys.stdin.readline()\n"
                "assert files\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", helper, str(codex), str(guardian), str(claude)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "ready")
                resumes = resolve_agent_resumes(
                    [
                        _tab(
                            _window(11, process.pid, "codex"),
                            _window(12, process.pid, "claude"),
                        )
                    ],
                    DEFAULT_APP_PROFILES,
                )
            finally:
                process.communicate("\n", timeout=5)

        self.assertEqual(resumes[11], ["codex", "resume", CODEX_ID])
        self.assertEqual(resumes[12], ["claude", "--resume", CLAUDE_ID])

    def test_procfs_reads_available_descriptors_and_tolerates_races(self) -> None:
        descriptors = (Path("/proc/7/fd/3"), Path("/proc/7/fd/4"))
        with mock.patch.object(Path, "is_dir", return_value=False):
            self.assertIsNone(_proc_open_files(7))
        with (
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(Path, "iterdir", side_effect=OSError("gone")),
        ):
            self.assertEqual(_proc_open_files(7), [])
        with (
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(Path, "iterdir", return_value=iter(descriptors)),
            mock.patch("os.readlink", side_effect=["/tmp/one", OSError("closed")]),
        ):
            self.assertEqual(_proc_open_files(7), [Path("/tmp/one")])

    def test_lsof_is_batched_and_parsed_without_trusting_unrequested_pids(self) -> None:
        result = subprocess.CompletedProcess(
            ["lsof"],
            1,
            stdout="p2\nn/tmp/two\npbad\nn/tmp/ignored\np99\nn/tmp/other\np3\nn/tmp/three\n",
            stderr="partial",
        )
        with (
            mock.patch(
                "kisesh.agent_resume._proc_open_files", side_effect=[[Path("/tmp/one")], None, None]
            ),
            mock.patch("shutil.which", return_value="/usr/bin/lsof"),
            mock.patch("subprocess.run", return_value=result) as run,
        ):
            paths = process_open_files((1, 2, 2, -1, 3))

        self.assertEqual(
            paths, {1: [Path("/tmp/one")], 2: [Path("/tmp/two")], 3: [Path("/tmp/three")]}
        )
        self.assertEqual(run.call_args.args[0][3], "2,3")

    def test_missing_or_failed_lsof_returns_only_available_procfs_paths(self) -> None:
        with (
            mock.patch("kisesh.agent_resume._proc_open_files", return_value=None),
            mock.patch("shutil.which", return_value=None),
        ):
            self.assertEqual(process_open_files((4,)), {})
        for failure in (OSError("missing"), subprocess.TimeoutExpired("lsof", 2)):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch("kisesh.agent_resume._proc_open_files", return_value=None),
                mock.patch("shutil.which", return_value="lsof"),
                mock.patch("subprocess.run", side_effect=failure),
            ):
                self.assertEqual(process_open_files((4,)), {})
        with (
            mock.patch("kisesh.agent_resume._proc_open_files", return_value=[]),
            mock.patch("shutil.which", return_value="lsof"),
            mock.patch("subprocess.run") as run,
        ):
            self.assertEqual(process_open_files((4,)), {4: []})
            run.assert_not_called()
