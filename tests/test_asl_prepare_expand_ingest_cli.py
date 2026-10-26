"""`asl expand-ingest` command tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    authoring_render,
    cli,
    prepare_expand_ingest,
    prepare_models,
    prepare_validation_store,
)
from tests.support import (
    _validator_payload,
    _write_prepare_plan,
)


class ExpandIngestCliTests(unittest.TestCase):
    """`asl expand-ingest` validator-output ingestion wrapper."""

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["expand-ingest", *argv])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.resolve_handler(args)(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def _write_batch(self, tmp: Path, payload: dict) -> Path:
        batch = tmp / "batch.json"
        batch.write_text(json.dumps(payload), encoding="utf-8")
        return batch

    def test_parser_wiring(self) -> None:
        args = cli.build_parser().parse_args(
            ["expand-ingest", "/plan", "--batch-file", "/b.json", "--json"]
        )
        self.assertIs(
            prepare_expand_ingest.expand_ingest_from_args, cli.resolve_handler(args)
        )
        self.assertEqual("/plan", args.plan_dir)
        self.assertEqual("/b.json", args.batch_file)
        self.assertTrue(args.json)

    def test_ingest_renders_expansions_and_applies_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))
            payload = _validator_payload(
                corrections=[
                    {
                        "expected_text": "load_config",
                        "replacement_text": "load_settings",
                        "reason": "renamed helper",
                    }
                ]
            )
            batch = self._write_batch(tmp_path, payload)

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(0, code)
            body = json.loads(stdout)
            self.assertTrue(body["ok"])
            self.assertEqual("ok", body["reason"])
            self.assertEqual(["1"], body["sessions"])
            self.assertTrue((plan_dir / "session-1.expand.md").is_file())
            self.assertTrue(
                any(f.endswith("session-1.expand.md") for f in body["files"])
            )
            self.assertEqual(1, body["correction_summary"]["applied"])
            self.assertEqual("applied", body["corrections"][0]["status"])
            # The stored aggregate persisted the batch.
            stored = prepare_validation_store.load_prepare_validation(plan_dir)
            assert stored is not None
            self.assertEqual(("1",), stored.session_ids)
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn("load_settings", session_text)

    def test_correction_keeps_a_crlf_session_file_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            session = plan_dir / "session-1.md"
            lf_text = session.read_text(encoding="utf-8")
            session.write_bytes(lf_text.replace("\n", "\r\n").encode("utf-8"))
            payload = _validator_payload(
                corrections=[
                    {
                        "expected_text": "load_config",
                        "replacement_text": "load_settings",
                        "reason": "renamed helper",
                    }
                ]
            )
            batch = self._write_batch(tmp_path, payload)

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(0, code, stdout)
            self.assertEqual(1, json.loads(stdout)["correction_summary"]["applied"])
            raw = session.read_bytes()
            self.assertIn(b"load_settings", raw)
            # Every line still ends in CRLF; none was rewritten to a bare LF.
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_second_batch_reports_only_its_own_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))

            first = self._write_batch(tmp_path, _validator_payload())
            self._run_cli(str(plan_dir), "--batch-file", str(first), "--json")

            second = tmp_path / "batch2.json"
            second.write_text(
                json.dumps(_validator_payload(session_id="2")), encoding="utf-8"
            )
            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(second), "--json"
            )

            self.assertEqual(0, code)
            body = json.loads(stdout)
            self.assertEqual(["2"], body["sessions"])
            # The aggregate now covers both batches.
            stored = prepare_validation_store.load_prepare_validation(plan_dir)
            assert stored is not None
            self.assertEqual(("1", "2"), stored.session_ids)

    def test_malformed_payload_fails_closed_with_validator_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            payload = _validator_payload()
            payload["sessions"][0]["claims"] = [{"claim": ""}]
            batch = self._write_batch(tmp_path, payload)

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertFalse(body["ok"])
            self.assertEqual("validator-schema", body["reason"])
            self.assertIn("validator output rejected", body["error"])
            # Nothing was written.
            self.assertFalse(
                (plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).exists()
            )
            self.assertFalse((plan_dir / "session-1.expand.md").exists())

    def test_invalid_json_fails_closed_with_validator_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            batch = tmp_path / "batch.json"
            batch.write_text("{not json", encoding="utf-8")

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )
            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertEqual("validator-schema", body["reason"])

    def test_missing_plan_directory_is_plan_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            batch = self._write_batch(tmp_path, _validator_payload())
            missing = tmp_path / "nope"

            code, stdout, _stderr = self._run_cli(
                str(missing), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(1, code)
            payload = json.loads(stdout)
            self.assertEqual("plan-not-found", payload["reason"])
            self.assertIn("plan directory does not exist", payload["error"])

    def test_missing_batch_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            code, stdout, _stderr = self._run_cli(
                str(plan_dir),
                "--batch-file",
                str(Path(tmp) / "nope.json"),
                "--json",
            )
            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertEqual("input-error", body["reason"])

    def test_non_utf8_batch_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            batch = Path(tmp) / "batch.json"
            batch.write_bytes(b'{"version": "\xff"}')
            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )
            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertEqual("input-error", body["reason"])
            self.assertIn("can't decode byte", body["error"])

    def test_late_render_failure_leaves_no_partial_mutation(self) -> None:
        """A valid-schema batch that fails during expansion rendering (a session
        with no ``> **Before starting**:`` anchor) commits nothing: the
        aggregate is never persisted, no expansion file is created, and every
        pre-command file is left byte-identical."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))
            # Strip the expansion anchor from session 1 so a valid payload fails
            # closed during reference insertion, after the aggregate would have
            # been prepared.
            session_1 = plan_dir / "session-1.md"
            anchored = session_1.read_text(encoding="utf-8")
            self.assertIn(authoring_render.CONTEXT_REFERENCE_LINE, anchored)
            session_1.write_text(
                anchored.replace(authoring_render.CONTEXT_REFERENCE_LINE + "\n", ""),
                encoding="utf-8",
            )

            before = {
                path.name: path.read_bytes()
                for path in sorted(plan_dir.iterdir())
                if path.is_file()
            }
            batch = self._write_batch(tmp_path, _validator_payload())

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertFalse(body["ok"])
            self.assertEqual("validator-schema", body["reason"])
            # The aggregate was never persisted ahead of the late failure.
            self.assertFalse(
                (plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).exists()
            )
            self.assertFalse((plan_dir / "session-1.expand.md").exists())
            # No new artifact and every pre-command file is byte-identical.
            after = {
                path.name: path.read_bytes()
                for path in sorted(plan_dir.iterdir())
                if path.is_file()
            }
            self.assertEqual(before, after)
