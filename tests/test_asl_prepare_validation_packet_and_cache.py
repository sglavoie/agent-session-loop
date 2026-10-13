"""Validator launch, packet, ingest, and validation cache tests."""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    plans_content_hash,
    plans_errors,
    prepare_models,
    prepare_packet,
    prepare_parsing,
    prepare_validation_cache,
    prepare_validation_store,
    prepare_validator,
)
from tests.support import (
    _validator_payload,
    _write_prepare_plan,
)
from tests.support_asl_prepare import PrepareValidatorFixtures


class PrepareValidationPacketAndCacheTests(PrepareValidatorFixtures):
    def test_launch_validator_repairs_a_rejected_shape(self) -> None:
        bad = _validator_payload(issues=[{"issue_type": "bogus"}])
        good = _validator_payload()
        (launch, packet), prompts = self._scripted_validator(
            json.dumps(bad), json.dumps(good)
        )

        with launch, redirect_stderr(io.StringIO()) as stderr:
            payload = prepare_validator._launch_validator("claude", packet)

        self.assertEqual(good, payload)
        self.assertEqual(2, len(prompts))
        self.assertIn("issue_type", prompts[1])
        self.assertIn(json.dumps(bad), prompts[1])
        self.assertIn("repair it (1/2)", stderr.getvalue())

    def test_launch_validator_gives_up_after_bounded_repairs(self) -> None:
        (launch, packet), prompts = self._scripted_validator("nope", "nope", "nope")

        with (
            launch,
            redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(plans_errors.PlanError, "invalid JSON"),
        ):
            prepare_validator._launch_validator("claude", packet)

        self.assertEqual(1 + prepare_validator.VALIDATOR_REPAIR_ATTEMPTS, len(prompts))

    def test_legacy_contract_problem_rejects_repo(self) -> None:
        payload = _validator_payload(
            contract_checked=True,
            contract_problems=[{"message": "m", "suggested_fix": "f", "repo": "SVC"}],
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "unknown keys: repo"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_unknown_issue_type(self) -> None:
        payload = _validator_payload(
            issues=[
                {
                    "issue_type": "design-change",
                    "summary": "s",
                    "session_ref": "r",
                    "actual": "a",
                    "fix": "f",
                }
            ]
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "issue_type"):
            prepare_parsing.parse_validator_output(payload)

    def test_accepts_removal_taxonomy_in_legacy_payload(self) -> None:
        payload = _validator_payload(**self._removal_overrides())

        session = prepare_parsing.parse_validator_output(payload).sessions[0]

        self.assertEqual("R", session.claims[0].claim_type)
        self.assertIsNone(session.claims[0].repo)
        self.assertEqual("removal-unjustified", session.issues[0].issue_type)

    def test_accepts_removal_taxonomy_in_registered_payload(self) -> None:
        payload = self._registered_envelope(
            _validator_payload(**self._removal_overrides(repo="SVC"))["sessions"][0]
        )

        session = prepare_parsing.parse_validator_output(payload).sessions[0]

        self.assertEqual(
            ("R", "SVC"), (session.claims[0].claim_type, session.claims[0].repo)
        )
        self.assertEqual(
            ("removal-unjustified", "SVC"),
            (session.issues[0].issue_type, session.issues[0].repo),
        )

    def test_rejects_unknown_taxonomy_in_both_versions_without_writing(self) -> None:
        mutations = (
            (
                "claim_type",
                lambda session: session["claims"][0].__setitem__("claim_type", "X"),
            ),
            (
                "issue_type",
                lambda session: session["issues"][0].__setitem__(
                    "issue_type", "removal-justified"
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                plan_dir = Path(tmp) / "plan"
                _write_prepare_plan(plan_dir)
                payload = _validator_payload(**self._removal_overrides())
                mutate(payload["sessions"][0])
                before = {
                    path.name: path.read_bytes()
                    for path in plan_dir.iterdir()
                    if path.is_file()
                }

                with self.assertRaisesRegex(plans_errors.PlanError, label):
                    prepare_validation_store.ingest_validator_output(plan_dir, payload)

                after = {
                    path.name: path.read_bytes()
                    for path in plan_dir.iterdir()
                    if path.is_file()
                }
                self.assertEqual(before, after)

                registered_session = _validator_payload(
                    **self._removal_overrides(repo="SVC")
                )["sessions"][0]
                mutate(registered_session)
                with self.assertRaisesRegex(plans_errors.PlanError, label):
                    prepare_parsing.parse_validator_output(
                        self._registered_envelope(registered_session)
                    )

    def test_rejects_more_than_three_patterns(self) -> None:
        pattern = {"name": "p", "location": "a.py:1", "reason": "r"}
        payload = _validator_payload(
            patterns=[pattern, dict(pattern), dict(pattern), dict(pattern)]
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "at most 3"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_correction_with_identical_texts(self) -> None:
        payload = _validator_payload(
            corrections=[
                {"expected_text": "same", "replacement_text": "same", "reason": "r"}
            ]
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "must differ"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_contract_problems_without_contract_checked(self) -> None:
        payload = _validator_payload(
            contract_checked=False,
            contract_problems=[{"message": "m", "suggested_fix": "f"}],
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "contract_checked"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_invalid_dependency_direction(self) -> None:
        payload = _validator_payload(
            dependencies=[
                {"direction": "blocks", "session_id": "2", "description": "d"}
            ]
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "direction"):
            prepare_parsing.parse_validator_output(payload)

    def test_batch_sessions_bounds_batch_size(self) -> None:
        batches = prepare_packet.batch_sessions(["1", "2", "2.1", "3", "4"], 2)
        self.assertEqual((("1", "2"), ("2.1", "3"), ("4",)), batches)

    def test_batch_sessions_rejects_nonpositive_batch_size(self) -> None:
        with self.assertRaisesRegex(plans_errors.PlanError, "batch size"):
            prepare_packet.batch_sessions(["1"], 0)

    def test_build_validation_packet_references_paths_not_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))

            packet = prepare_packet.build_validation_packet(
                plan_dir, ["1", "2"], codebase_root="/repo"
            )
            rendered = packet.render()

            self.assertTrue(
                rendered.startswith(
                    f"{prepare_models.VALIDATION_PACKET_HEADING_PREFIX} — sessions 1, 2"
                )
            )
            self.assertIn(str(plan_dir.resolve() / "session-1.md"), rendered)
            self.assertIn(str(plan_dir.resolve() / "context.md"), rendered)
            self.assertIn(
                f'"version": {prepare_models.LEGACY_PREPARE_VALIDATION_VERSION}',
                rendered,
            )
            self.assertIn("environment and integrations", rendered)
            self.assertIn(
                "Keep factual mismatches distinct from planning advice", rendered
            )
            # Plan file contents are referenced, never inlined.
            self.assertNotIn("Update the retry helper", rendered)
            for issue_type in prepare_models.ISSUE_TYPES:
                self.assertIn(issue_type, rendered)

    def test_validation_prompt_digest_tracks_the_rendered_packet(self) -> None:
        legacy = prepare_packet._validation_prompt_digest(
            prepare_models.LEGACY_PREPARE_VALIDATION_VERSION
        )
        registered = prepare_packet._validation_prompt_digest(
            prepare_models.PREPARE_VALIDATION_VERSION
        )

        self.assertNotEqual(legacy, registered)
        self.assertEqual(
            legacy,
            prepare_packet._validation_prompt_digest(
                prepare_models.LEGACY_PREPARE_VALIDATION_VERSION
            ),
        )

        original_render = prepare_packet.ValidationTaskPacket.render
        with mock.patch.object(
            prepare_packet.ValidationTaskPacket,
            "render",
            lambda packet: original_render(packet) + "\n- Research removal rationale.",
        ):
            changed = prepare_packet._validation_prompt_digest(
                prepare_models.LEGACY_PREPARE_VALIDATION_VERSION
            )

        self.assertNotEqual(legacy, changed)

    def test_build_validation_packet_rejects_missing_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            with self.assertRaisesRegex(plans_errors.PlanError, "missing session file"):
                prepare_packet.build_validation_packet(
                    plan_dir, ["1", "9"], codebase_root="/repo"
                )

    def test_ingest_saves_aggregate_and_merges_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))

            first = prepare_validation_store.ingest_validator_output(
                plan_dir, _validator_payload()
            )
            self.assertEqual(("1",), first.session_ids)

            second_payload = _validator_payload(session_id="2")
            second = prepare_validation_store.ingest_validator_output(
                plan_dir, second_payload
            )
            self.assertEqual(("1", "2"), second.session_ids)

            stored = prepare_validation_store.load_prepare_validation(plan_dir)
            assert stored is not None
            self.assertEqual(("1", "2"), stored.session_ids)
            self.assertTrue(
                (plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).is_file()
            )

    def test_merge_replaces_existing_session_entry(self) -> None:
        base = prepare_parsing.parse_validator_output(_validator_payload())
        update = prepare_parsing.parse_validator_output(
            _validator_payload(title="Updated title")
        )
        merged = prepare_validation_store.merge_validations(base, update)
        self.assertEqual(("1",), merged.session_ids)
        self.assertEqual("Updated title", merged.sessions[0].title)

    def test_merge_rejects_codebase_root_mismatch(self) -> None:
        base = prepare_parsing.parse_validator_output(_validator_payload())
        other = _validator_payload()
        other["codebase_root"] = "/elsewhere"
        update = prepare_parsing.parse_validator_output(other)
        with self.assertRaisesRegex(plans_errors.PlanError, "codebase root"):
            prepare_validation_store.merge_validations(base, update)

    def test_load_returns_none_when_absent_and_fails_closed_on_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            self.assertIsNone(
                prepare_validation_store.load_prepare_validation(plan_dir)
            )

            (plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).write_text(
                "{not json", encoding="utf-8"
            )
            with self.assertRaisesRegex(plans_errors.PlanError, "not valid JSON"):
                prepare_validation_store.load_prepare_validation(plan_dir)

    def test_unknown_validation_cache_version_is_a_safe_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            (plan_dir / prepare_models.PREPARE_VALIDATION_CACHE_FILENAME).write_text(
                json.dumps({"version": 0, "obsolete_entries": []}),
                encoding="utf-8",
            )

            cache = prepare_validation_cache.load_prepare_validation_cache(plan_dir)

        self.assertEqual((), cache.entries)

    def test_obsolete_cached_validator_schema_is_a_safe_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = prepare_parsing.parse_validator_output(_validator_payload())
            identity = {"validator_schema_version": validation.version}
            entry = prepare_models.PrepareValidationCacheEntry(
                session_id=validation.sessions[0].session_id,
                key=prepare_validation_cache._canonical_json_digest(identity),
                identity=identity,
                validation=validation,
            )
            prepare_validation_cache.save_prepare_validation_cache(
                plan_dir,
                prepare_models.PrepareValidationCache(entries=(entry,)),
            )
            cache_path = plan_dir / prepare_models.PREPARE_VALIDATION_CACHE_FILENAME
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["entries"][0]["validation"]["version"] = 999
            cache_path.write_text(json.dumps(payload), encoding="utf-8")

            cache = prepare_validation_cache.load_prepare_validation_cache(plan_dir)

        self.assertEqual((), cache.entries)

    def test_malformed_output_cannot_mutate_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = {path.name: path.read_bytes() for path in plan_dir.iterdir()}
            payload = _validator_payload()
            payload["sessions"][0]["claims"] = [{"claim": ""}]

            with self.assertRaises(plans_errors.PlanError):
                prepare_validation_store.ingest_validator_output(plan_dir, payload)

            after = {path.name: path.read_bytes() for path in plan_dir.iterdir()}
            self.assertEqual(before, after)
            self.assertFalse(
                (plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).exists()
            )

    def test_prepare_validation_excluded_from_readiness_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = plans_content_hash.stable_readiness_sha256(plan_dir)
            prepare_validation_store.ingest_validator_output(
                plan_dir, _validator_payload()
            )
            prepare_validation_cache.save_prepare_validation_cache(
                plan_dir, prepare_models.PrepareValidationCache()
            )
            self.assertEqual(
                before, plans_content_hash.stable_readiness_sha256(plan_dir)
            )
