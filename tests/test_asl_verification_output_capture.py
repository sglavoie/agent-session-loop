"""Command output capture, previews, and retained artifact tests."""

from __future__ import annotations

import os
import tempfile
import tracemalloc
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    artifacts,
    contracts,
    plans_session_ids,
    process_tree,
    verification_checks,
    verification_run,
)
from tests.support_asl_verification import VerificationExecutorFixtures


class VerificationOutputCaptureTests(VerificationExecutorFixtures):
    def test_passing_command_records_bounded_metadata_no_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys; sys.stdout.write('hello world')",
                )
            )

            result = verification_checks.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("pass", result.status)
            self.assertEqual("hello world", result.stdout)
            self.assertFalse(result.stdout_truncated)
            self.assertEqual(len(b"hello world"), result.stdout_byte_count)
            self.assertEqual(artifacts.sha256_hex(b"hello world"), result.stdout_sha256)
            # Passing output never writes a full raw artifact.
            self.assertIsNone(result.stdout_artifact_path)
            self.assertIsNone(result.stderr_artifact_path)
            self.assertFalse(artifact_dir.exists())

    def test_large_passing_output_is_previewed_not_embedded_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            # Emit far more than the 16 KiB preview window.
            size = 400 * 1024
            check = self._command(
                (
                    "python3",
                    "-c",
                    f"import sys; sys.stdout.write('a' * {size})",
                )
            )

            result = verification_checks.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertEqual(size, result.stdout_byte_count)
            self.assertTrue(result.stdout_truncated)
            # The serialized result cannot contain the full large output.
            self.assertLess(len(result.stdout), size)
            self.assertLessEqual(
                len(result.stdout),
                artifacts.PREVIEW_HEAD_BYTES
                + artifacts.PREVIEW_TAIL_BYTES
                + len(artifacts.PREVIEW_TRUNCATION_MARKER),
            )
            self.assertIn(artifacts.PREVIEW_TRUNCATION_MARKER, result.stdout)

    def test_failed_command_retains_full_output_recoverable_from_gzip(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            size = 200 * 1024
            check = self._command(
                (
                    "python3",
                    "-c",
                    f"import sys\nsys.stdout.write('x' * {size})\nsys.exit(5)",
                ),
                name="boom",
            )

            result = verification_checks.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("fail", result.status)
            self.assertEqual("command-failed", result.failure_reason)
            self.assertTrue(result.stdout_truncated)
            self.assertIsNotNone(result.stdout_artifact_path)
            recovered = artifacts.read_gzip(Path(result.stdout_artifact_path))
            self.assertEqual(b"x" * size, recovered)
            self.assertEqual(artifacts.sha256_hex(recovered), result.stdout_sha256)

    def test_colliding_check_names_write_distinct_recoverable_artifacts(
        self,
    ) -> None:
        # Each pair sanitizes to the same stem; without a collision-resistant
        # identifier the second failed check would overwrite the first
        # check's artifact via os.replace. The second pair additionally
        # shares the first 8 hex chars of the SHA-256 digests of its
        # "-stdout" artifact names, so a short digest suffix still collided.
        pairs = (
            ("a/b", "a?b"),
            ("a$%#:??/b", "a%%#%$:/b"),
        )
        for first_name, second_name in pairs:
            with (
                self.subTest(pair=(first_name, second_name)),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo_root = Path(tmp) / "repo"
                repo_root.mkdir()
                artifact_dir = Path(tmp) / "artifacts"
                results = {}
                for name, payload in (
                    (first_name, "first-output"),
                    (second_name, "second-output"),
                ):
                    check = self._command(
                        (
                            "python3",
                            "-c",
                            f"import sys; sys.stdout.write({payload!r}); sys.exit(1)",
                        ),
                        name=name,
                    )
                    result = verification_checks.run_check(
                        check, repo_root=repo_root, artifact_dir=artifact_dir
                    )
                    self.assertEqual("fail", result.status)
                    self.assertIsNotNone(result.stdout_artifact_path)
                    results[name] = (result, payload)

                first, first_payload = results[first_name]
                second, second_payload = results[second_name]
                self.assertNotEqual(
                    first.stdout_artifact_path, second.stdout_artifact_path
                )
                self.assertNotEqual(
                    first.stderr_artifact_path, second.stderr_artifact_path
                )
                self.assertEqual(
                    first_payload.encode("utf-8"),
                    artifacts.read_gzip(Path(first.stdout_artifact_path)),
                )
                self.assertEqual(
                    second_payload.encode("utf-8"),
                    artifacts.read_gzip(Path(second.stdout_artifact_path)),
                )

    def test_failed_command_without_artifact_dir_keeps_previews_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys; sys.stderr.write('nope'); sys.exit(2)",
                )
            )

            result = verification_checks.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("nope", result.stderr)
            self.assertEqual(len(b"nope"), result.stderr_byte_count)
            self.assertIsNone(result.stdout_artifact_path)
            self.assertIsNone(result.stderr_artifact_path)

    def test_timeout_retains_partial_output_under_failure_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys, time\n"
                    "sys.stdout.write('partial')\n"
                    "sys.stdout.flush()\n"
                    "time.sleep(5)",
                ),
                name="slow",
                timeout_seconds=0.3,
            )

            result = verification_checks.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("fail", result.status)
            self.assertEqual("command-timeout", result.failure_reason)
            # Whatever was flushed before the timeout is retained as a bounded
            # preview and, because timeout is a failure, an artifact was written.
            self.assertIn("partial", result.stdout)
            self.assertIsNotNone(result.stdout_artifact_path)
            recovered = artifacts.read_gzip(Path(result.stdout_artifact_path))
            self.assertEqual(b"partial", recovered)

    def test_stream_capture_fails_open_when_artifact_write_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            # Point the artifact directory at a path blocked by an existing file
            # so directory creation raises; verification must still succeed with
            # bounded metadata and no artifact path.
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a dir\n", encoding="utf-8")
            artifact_dir = blocker / "sub"
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys; sys.stdout.write('data'); sys.exit(9)",
                )
            )

            result = verification_checks.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("fail", result.status)
            self.assertEqual("data", result.stdout)
            self.assertEqual(artifacts.sha256_hex(b"data"), result.stdout_sha256)
            self.assertIsNone(result.stdout_artifact_path)

    def test_run_verification_forwards_artifact_dir_to_command_checks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            contract = contracts.CompletionContract(
                session_id=plans_session_ids.parse_session_id("1"),
                done_when=("done",),
                verify=contracts.VerifyRecipe(
                    checks=(
                        self._command(
                            (
                                "python3",
                                "-c",
                                "import sys; sys.stdout.write('boom'); sys.exit(1)",
                            ),
                            name="fail-check",
                        ),
                    )
                ),
            )

            result = verification_run.run_verification(
                contract, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertFalse(result.passed)
            check_result = result.checks[0]
            self.assertIsNotNone(check_result.stdout_artifact_path)
            self.assertTrue(Path(check_result.stdout_artifact_path).exists())

    def test_large_output_peak_memory_stays_bounded(self) -> None:
        # Regression (M1): before streaming, subprocess.run buffered the full
        # stream in memory, so the traced-heap peak was at least the output
        # size. Streaming keeps the peak far below it.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            size = 8 * 1024 * 1024
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys\n"
                    "chunk = 'a' * 65536\n"
                    f"for _ in range({size} // 65536):\n"
                    "    sys.stdout.write(chunk)\n",
                ),
                timeout_seconds=120.0,
            )

            # The sweep's process-table sample is orthogonal to streaming: it
            # holds command lines of unrelated host processes, whose combined
            # size is unbounded and varies with whatever else the machine runs.
            # Opting out keeps the traced peak attributable to the capture path.
            with mock.patch.dict(
                os.environ, {process_tree.SWEEP_DISABLED_ENV_VAR: "1"}
            ):
                tracemalloc.start()
                try:
                    result = verification_checks.run_check(check, repo_root=repo_root)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

            self.assertEqual("pass", result.status)
            self.assertEqual(size, result.stdout_byte_count)
            self.assertTrue(result.stdout_truncated)
            self.assertLess(peak, size // 2)

    def test_streamed_digest_and_preview_match_buffered_reference(self) -> None:
        # The streamed capture must be indistinguishable from the previous
        # fully-buffered implementation: same digest, preview, truncation
        # flag, and byte count as store_stream_output over the whole stream.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            # Patterned output larger than the 16 KiB preview window,
            # including bytes that are invalid UTF-8.
            expected = bytes(range(256)) * 256
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys\nsys.stdout.buffer.write(bytes(range(256)) * 256)\n",
                )
            )

            result = verification_checks.run_check(check, repo_root=repo_root)

            reference = artifacts.store_stream_output(expected, failed=False)
            self.assertEqual("pass", result.status)
            self.assertEqual(reference.byte_count, result.stdout_byte_count)
            self.assertEqual(reference.sha256, result.stdout_sha256)
            self.assertEqual(reference.preview, result.stdout)
            self.assertEqual(reference.truncated, result.stdout_truncated)
