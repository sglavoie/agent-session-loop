"""Artifacts cases for StateCompactionTests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_session_loop import (
    artifacts,
    compact_state,
)

from .asl_compaction_state_support import StateCompactionTestsBase


class StateCompactionTests(StateCompactionTestsBase):
    def test_savings_are_net_of_retained_failure_artifacts(self) -> None:
        # Deterministic but poorly compressible failure output (hex digest
        # chain), so the retained gzip artifact stays large enough that
        # JSON-only accounting would visibly overstate the saving.
        digest = b"seed"
        chunks = []
        for _ in range(600):
            digest = hashlib.sha256(digest).digest()
            chunks.append(digest.hex())
        noisy = "".join(chunks)
        self._write_result(
            [{"name": "c1", "status": "fail", "stdout": noisy, "stderr": ""}]
        )
        disk_before = self._state_dir_disk_bytes()

        projected = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )
        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )
        disk_after = self._state_dir_disk_bytes()

        self.assertGreater(report.artifact_bytes_added, 0)
        # Reported savings equal the bytes actually reclaimed on disk,
        # which is strictly less than the JSON-only delta.
        self.assertEqual(disk_before - disk_after, report.actual_bytes_reclaimed)
        self.assertEqual(
            report.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        self.assertLess(
            report.actual_bytes_reclaimed,
            report.bytes_before - report.bytes_after,
        )
        # The dry-run projected the same net figure without writing anything.
        self.assertEqual(
            projected.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        self.assertEqual(report.artifact_bytes_added, projected.artifact_bytes_added)
        self.assertEqual(
            report.artifact_bytes_added, report.to_dict()["artifact_bytes_added"]
        )

    def test_colliding_check_names_keep_distinct_artifacts(self) -> None:
        # Each pair sanitizes to the same `a-b` stem; the second pair also
        # shares the first 8 hex chars of the SHA-256 digests of its
        # "-stdout" artifact names, so a short digest suffix still routed
        # both extractions to one path. The collision-resistant derivation
        # must keep the failure artifacts on distinct paths so the second
        # extraction cannot overwrite the first.
        pairs = (
            ("a/b", "a?b"),
            ("a$%#:??/b", "a%%#%$:/b"),
        )
        first = "first-output " + self.big
        second = "second-output " + self.big
        for first_name, second_name in pairs:
            with self.subTest(pair=(first_name, second_name)):
                result_path = self._write_result(
                    [
                        {
                            "name": first_name,
                            "status": "fail",
                            "stdout": first,
                            "stderr": "",
                        },
                        {
                            "name": second_name,
                            "status": "fail",
                            "stdout": second,
                            "stderr": "",
                        },
                    ]
                )

                projected = compact_state.compact_plan_state(
                    self.plan_dir, state_dir=self.state_dir, apply=False
                )
                report = compact_state.compact_plan_state(
                    self.plan_dir, state_dir=self.state_dir, apply=True
                )

                payload = json.loads(result_path.read_text(encoding="utf-8"))
                checks = payload["verification"]["checks"]
                paths = [Path(check["stdout_artifact_path"]) for check in checks]
                self.assertNotEqual(paths[0], paths[1])
                # Both checks' full output is retained byte-for-byte.
                self.assertEqual(first, artifacts.read_gzip(paths[0]).decode("utf-8"))
                self.assertEqual(second, artifacts.read_gzip(paths[1]).decode("utf-8"))
                # The dry-run projection matches the applied reclaim
                # byte-for-byte; before the fix both checks projected against
                # one shared target and the figures diverged.
                self.assertEqual(
                    projected.projected_bytes_reclaimed,
                    report.actual_bytes_reclaimed,
                )
                self.assertEqual(
                    projected.artifact_bytes_added, report.artifact_bytes_added
                )
                self.assertEqual(2, report.failed_output_extracted)

    def test_same_named_or_unnamed_checks_keep_distinct_artifacts(self) -> None:
        # Two checks sharing a name, or two with no name, derived the same
        # `{name}-{stream}` stem, so the second extraction overwrote the
        # first's gzip and artifact_bytes_added counted bytes no longer on
        # disk. An unrelated file already at the first candidate path must
        # survive too.
        first = "first-output " + self.big
        second = "second-output " + self.big
        for name in ("dup", None):
            with self.subTest(name=name):
                checks = [
                    {"status": "fail", "stdout": first, "stderr": ""},
                    {"status": "fail", "stdout": second, "stderr": ""},
                ]
                if name is not None:
                    for check in checks:
                        check["name"] = name
                result_path = self._write_result(checks)
                artifact_dir = result_path.parent / "verification"
                artifact_dir.mkdir(exist_ok=True)
                foreign = artifact_dir / f"{name or 'check'}-stdout.gz"
                foreign.write_bytes(b"not ours")
                disk_before = self._state_dir_disk_bytes()

                projected = compact_state.compact_plan_state(
                    self.plan_dir, state_dir=self.state_dir, apply=False
                )
                report = compact_state.compact_plan_state(
                    self.plan_dir, state_dir=self.state_dir, apply=True
                )

                payload = json.loads(result_path.read_text(encoding="utf-8"))
                paths = [
                    Path(check["stdout_artifact_path"])
                    for check in payload["verification"]["checks"]
                ]
                self.assertNotEqual(paths[0], paths[1])
                self.assertNotIn(foreign, paths)
                self.assertEqual(b"not ours", foreign.read_bytes())
                self.assertEqual(first, artifacts.read_gzip(paths[0]).decode("utf-8"))
                self.assertEqual(second, artifacts.read_gzip(paths[1]).decode("utf-8"))
                self.assertEqual(
                    sum(path.stat().st_size for path in paths),
                    report.artifact_bytes_added,
                )
                self.assertEqual(
                    disk_before - self._state_dir_disk_bytes(),
                    report.actual_bytes_reclaimed,
                )
                self.assertEqual(
                    projected.artifact_bytes_added, report.artifact_bytes_added
                )
                self.assertEqual(
                    projected.projected_bytes_reclaimed,
                    report.actual_bytes_reclaimed,
                )
                for path in paths:
                    path.unlink()

    def test_reapply_after_interrupted_apply_reuses_written_artifacts(self) -> None:
        # An apply interrupted after the gzip writes but before result.json
        # was replaced leaves identical artifacts behind; the next pass must
        # reuse them instead of adding copies or counting their bytes twice.
        first = "first-output " + self.big
        second = "second-output " + self.big
        checks = [
            {"name": "dup", "status": "fail", "stdout": first, "stderr": ""},
            {"name": "dup", "status": "fail", "stdout": second, "stderr": ""},
        ]
        result_path = self._write_result(checks)
        original = result_path.read_bytes()
        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )
        written = sorted((result_path.parent / "verification").iterdir())
        result_path.write_bytes(original)  # simulate the lost result rewrite

        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        self.assertEqual(0, report.artifact_bytes_added)
        self.assertEqual(
            written, sorted((result_path.parent / "verification").iterdir())
        )
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [first, second],
            [
                artifacts.read_gzip(check["stdout_artifact_path"]).decode("utf-8")
                for check in payload["verification"]["checks"]
            ],
        )

    def test_overlong_check_names_compact_without_aborting(self) -> None:
        # A ~192-char check name plus the "-stdout" stream suffix and the
        # full 64-hex digest used to push the gzip artifact (and its
        # ``.{name}.tmp`` atomic-write decoration) past the 255-byte
        # per-component limit, so applied compaction aborted with an
        # unhandled ArtifactError at the write. The shared byte-capped stem
        # must keep extraction bounded and recoverable, distinct per name,
        # with dry-run projected reclaim equal to the applied reclaim.
        first_name = "a" * 190 + "/x"
        second_name = "b" + "a" * 189 + "/x"
        first = "first-output " + self.big
        second = "second-output " + self.big
        result_path = self._write_result(
            [
                {
                    "name": first_name,
                    "status": "fail",
                    "stdout": first,
                    "stderr": "",
                },
                {
                    "name": second_name,
                    "status": "fail",
                    "stdout": second,
                    "stderr": "",
                },
            ]
        )

        projected = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )
        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        payload = json.loads(result_path.read_text(encoding="utf-8"))
        checks = payload["verification"]["checks"]
        paths = [Path(check["stdout_artifact_path"]) for check in checks]
        # Distinct long names keep distinct artifacts even after truncation.
        self.assertNotEqual(paths[0], paths[1])
        for path in paths:
            with self.subTest(artifact=path.name):
                self.assertTrue(path.exists())
                self.assertLessEqual(len(path.name.encode("utf-8")), 255)
                self.assertLessEqual(len(f".{path.name}.tmp".encode()), 255)
        # Both outputs stay byte-for-byte recoverable from the artifacts.
        self.assertEqual(first, artifacts.read_gzip(paths[0]).decode("utf-8"))
        self.assertEqual(second, artifacts.read_gzip(paths[1]).decode("utf-8"))
        self.assertEqual(2, report.failed_output_extracted)
        # Dry-run/apply parity holds for the long-name case.
        self.assertEqual(
            projected.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        self.assertEqual(projected.artifact_bytes_added, report.artifact_bytes_added)
