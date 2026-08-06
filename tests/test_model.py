from __future__ import annotations

import unittest

from kisesh.model import (
    SCHEMA_VERSION,
    SessionManifest,
    SnapshotSummary,
    session_marker_name,
    slugify,
)


class ModelTests(unittest.TestCase):
    def test_session_marker_name_preserves_display_text_without_multiline_controls(self) -> None:
        self.assertEqual(
            session_marker_name("  Research\n\x1b Team  ", "fallback"), "Research Team"
        )
        self.assertEqual(session_marker_name("\n\t", "fallback"), "fallback")

    def test_slugify_is_stable_and_filesystem_safe(self) -> None:
        self.assertEqual(slugify("  Réçit Q2 / Main  "), "recit-q2-main")
        self.assertEqual(slugify("___"), "session")

    def test_manifest_round_trip_validates_uuid(self) -> None:
        manifest = SessionManifest(name="Dotfiles", slug="dotfiles", project_root="/tmp/dotfiles")
        decoded = SessionManifest.from_dict(manifest.to_dict())
        self.assertEqual(decoded, manifest)

        payload = manifest.to_dict()
        payload["id"] = "not-a-uuid"
        with self.assertRaisesRegex(ValueError, "invalid session id"):
            SessionManifest.from_dict(payload)

    def test_summary_coercion_rejects_boolean_invalid_and_scalar_values(self) -> None:
        summary = SnapshotSummary.from_dict(
            {
                "tab_count": True,
                "pane_count": "not-an-integer",
                "tab_titles": "Shell",
                "working_directories": ["/tmp", 7],
            }
        )

        self.assertEqual(summary.tab_count, 0)
        self.assertEqual(summary.pane_count, 0)
        self.assertEqual(summary.tab_titles, [])
        self.assertEqual(summary.working_directories, ["/tmp", "7"])
        self.assertEqual(SnapshotSummary.from_dict(None), SnapshotSummary())

    def test_manifest_rejects_each_invalid_persisted_identity_field(self) -> None:
        manifest = SessionManifest(name="Project", slug="project", project_root="/tmp")
        cases = (
            ("schema_version", SCHEMA_VERSION + 1, "unsupported manifest schema"),
            ("name", "   ", "session name cannot be empty"),
            ("slug", "NOT SAFE", "invalid session slug"),
            ("status", "removed", "invalid session status"),
        )
        for field, value, message in cases:
            payload = manifest.to_dict()
            payload[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                SessionManifest.from_dict(payload)

    def test_manifest_parses_optional_fields_and_ignores_non_object_summary(self) -> None:
        manifest = SessionManifest(name="Project", slug="project", project_root="/tmp")
        payload = manifest.to_dict()
        payload.update(
            {
                "summary": ["not", "an", "object"],
                "archived_at": 123,
                "snapshot_sha256": 456,
                "revision": "7",
            }
        )

        decoded = SessionManifest.from_dict(payload)

        self.assertEqual(decoded.summary, SnapshotSummary())
        self.assertEqual(decoded.archived_at, "123")
        self.assertEqual(decoded.snapshot_sha256, "456")
        self.assertEqual(decoded.revision, 7)


if __name__ == "__main__":
    unittest.main()
