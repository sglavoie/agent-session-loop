"""Prepare resume and cached-validation invalidation tests."""

from __future__ import annotations

import dataclasses
from unittest import mock

from agent_session_loop import (
    plans,
    prepare_models,
    prepare_packet,
    prepare_parsing,
    prepare_validation_cache,
    prepare_validation_store,
)
from tests.support import (
    _prepare_pipeline_payload,
    _recording_validator,
)
from tests.support_asl_prepare import PrepareEndToEndFixtures


class PrepareResumeAndCacheInvalidationTests(PrepareEndToEndFixtures):
    def test_malformed_validator_output_preserves_draft_with_blockers(self) -> None:
        source = self._write_source()

        result = self._prepare(
            source,
            _recording_validator([], payload_override={"bogus": True}),
        )

        self.assertFalse(result.ok)
        self.assertEqual("validation-failed", result.reason)
        self.assertFalse(result.marked_loop_ready)
        assert result.plan_dir is not None
        manifest = (result.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)
        self.assertEqual([], list(result.plan_dir.glob("*.expand.md")))
        self.assertEqual("validation", result.blockers[0].stage)
        assert result.resume_command is not None
        self.assertTrue(result.resume_command.startswith("asl prepare --resume"))
        self.assertIn(str(result.plan_dir), result.resume_command)

    def test_resume_completes_a_failed_preparation(self) -> None:
        source = self._write_source()
        failed = self._prepare(
            source,
            _recording_validator([], payload_override={"bogus": True}),
        )
        assert failed.plan_dir is not None
        calls: list[prepare_packet.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=failed.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertTrue(resumed.marked_loop_ready)
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        assert resumed.plan_dir is not None
        manifest = (resumed.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        self.assertTrue((resumed.plan_dir / "session-1.expand.md").is_file())
        # Resume never creates another split directory.
        self.assertEqual(1, len(list(self.base_dir.iterdir())))
        self.assertEqual(
            ["skipped", "skipped", "ok", "ok", "ok", "ok"],
            [step.status for step in resumed.steps],
        )

    def test_resume_refuses_to_rebind_existing_plan_metadata(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        other_repo = self.tmp_path / "other-repo"
        other_repo.mkdir()

        with self.assertRaisesRegex(plans.PlanError, "does not match"):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=other_repo,
                resume_plan_dir=first.plan_dir,
            )
        self.assertEqual(
            str(plans.canonicalize_path(self.repo)),
            plans.load_plan_metadata(first.plan_dir).cwd,
        )

    def test_resume_is_idempotent_and_reuses_cached_baselines(self) -> None:
        counter = self.tmp_path / "baseline-count.txt"
        argv = [
            "python3",
            "-c",
            (
                "import pathlib; "
                f"p = pathlib.Path({str(counter)!r}); "
                "p.write_text(p.read_text() + 'x' if p.exists() else 'x')"
            ),
        ]
        check = {
            "name": "baseline-check",
            "type": "command",
            "argv": argv,
            "timeout_seconds": 30,
            "baseline_expectation": "pass",
        }
        source = self._write_source(check_json_by_id={"1": check, "2": check})

        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.baseline is not None
        self.assertFalse(first.baseline["reused"])
        # Equivalent checks across sessions deduplicate to one execution.
        self.assertEqual("x", counter.read_text(encoding="utf-8"))

        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        # All sessions were already validated: no validator worker launched.
        self.assertEqual([], calls)
        # The recorded baseline evidence was reused: no re-execution.
        self.assertEqual("x", counter.read_text(encoding="utf-8"))
        assert resumed.baseline is not None
        self.assertTrue(resumed.baseline["reused"])
        self.assertEqual(1, resumed.baseline["check_count"])
        self.assertEqual(1, len(list(self.base_dir.iterdir())))
        assert resumed.plan_dir is not None
        manifest = (resumed.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)

    def test_validator_cache_revalidates_only_a_changed_session(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        cache = prepare_validation_cache.load_prepare_validation_cache(first.plan_dir)
        self.assertEqual(prepare_models.PREPARE_VALIDATION_CACHE_VERSION, cache.version)
        self.assertIsNotNone(cache.entry("1"))
        self.assertIsNotNone(cache.entry("2"))

        session_path = first.plan_dir / "session-2.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Implement step 2 in `demo/module.py`.",
                "Implement revised step 2 in `demo/module.py`.",
            ),
            encoding="utf-8",
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("2",)], [packet.session_ids for packet in calls])

    def test_legacy_aggregate_without_cache_refreshes_once(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        (first.plan_dir / prepare_models.PREPARE_VALIDATION_CACHE_FILENAME).unlink()
        calls: list[prepare_packet.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])
        self.assertTrue(
            (
                first.plan_dir / prepare_models.PREPARE_VALIDATION_CACHE_FILENAME
            ).is_file()
        )

    def test_cache_refresh_replaces_an_untrusted_historical_aggregate(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        replacement = prepare_parsing.parse_validator_output(
            _prepare_pipeline_payload(
                str(plans.canonicalize_path(self.repo)),
                ("1", "2", "99"),
            )
        )
        prepare_validation_store.save_prepare_validation(first.plan_dir, replacement)
        calls: list[prepare_packet.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([], calls)
        aggregate = prepare_validation_store.load_prepare_validation(first.plan_dir)
        assert aggregate is not None
        self.assertEqual(("1", "2"), aggregate.session_ids)

    def test_validator_selection_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
            validator_model="different-validator-model",
            validator_effort="high",
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_validator_harness_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
            validator_harness="codex",
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_context_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        context_path = first.plan_dir / "context.md"
        context_path.write_text(
            context_path.read_text(encoding="utf-8")
            + "\nAdditional shared validation context.\n",
            encoding="utf-8",
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_validator_schema_identity_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        cache = prepare_validation_cache.load_prepare_validation_cache(first.plan_dir)
        stale_entries: list[prepare_models.PrepareValidationCacheEntry] = []
        for entry in cache.entries:
            stale_identity = dict(entry.identity)
            stale_identity["validator_schema_version"] = 999
            stale_entries.append(
                dataclasses.replace(
                    entry,
                    identity=stale_identity,
                    key=prepare_validation_cache._canonical_json_digest(stale_identity),
                )
            )
        prepare_validation_cache.save_prepare_validation_cache(
            first.plan_dir,
            prepare_models.PrepareValidationCache(entries=tuple(stale_entries)),
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_packet_prompt_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        original_render = prepare_packet.ValidationTaskPacket.render
        changed_prompt = mock.patch.object(
            prepare_packet.ValidationTaskPacket,
            "render",
            lambda packet: original_render(packet) + "\n- Research removal rationale.",
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []
        with changed_prompt:
            resumed = self._prepare(
                None,
                _recording_validator(calls),
                resume_plan_dir=first.plan_dir,
            )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

        stable: list[prepare_packet.ValidationTaskPacket] = []
        with changed_prompt:
            reused = self._prepare(
                None,
                _recording_validator(stable),
                resume_plan_dir=first.plan_dir,
            )

        self.assertTrue(reused.ok, reused)
        self.assertEqual([], stable)

    def test_cache_identity_without_a_prompt_digest_is_stale(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        cache = prepare_validation_cache.load_prepare_validation_cache(first.plan_dir)
        stale_entries: list[prepare_models.PrepareValidationCacheEntry] = []
        for entry in cache.entries:
            self.assertIn("prompt_sha256", entry.identity)
            stale_identity = dict(entry.identity)
            del stale_identity["prompt_sha256"]
            stale_entries.append(
                dataclasses.replace(
                    entry,
                    identity=stale_identity,
                    key=prepare_validation_cache._canonical_json_digest(stale_identity),
                )
            )
        prepare_validation_cache.save_prepare_validation_cache(
            first.plan_dir,
            prepare_models.PrepareValidationCache(entries=tuple(stale_entries)),
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_repository_fingerprint_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        (self.repo / "validation-cache-input.txt").write_text(
            "repository changed\n", encoding="utf-8"
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])
