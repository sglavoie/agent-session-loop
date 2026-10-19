# ruff: noqa: F403, F405
from .asl_overseer_context_support import *


class PacketDescribesTheIncidentTests(unittest.TestCase):
    """A packet names every family the run published, and identifies the run."""

    def test_a_populated_run_yields_every_evidence_family(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        self.assertIsNone(packet.refusal)
        self.assertEqual(set(overseer_context.SOURCE_KINDS), _kinds(packet))
        self.assertEqual({}, _gaps(packet))
        self.assertTrue(packet.complete)

    def test_a_packet_carries_the_identities_that_scope_it(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        self.assertEqual(str(plan.plan_dir), packet.plan_dir)
        self.assertEqual(plan.run_id, packet.run_id)
        self.assertEqual((QUESTION,), packet.questions)
        self.assertEqual("rd-1", packet.request_digest)
        self.assertEqual(FINGERPRINT, packet.config_fingerprint)
        self.assertEqual(overseer_context.scope_digest(plan.candidate()), packet.scope)

    def test_every_source_names_where_it_came_from(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        for source in packet.sources:
            with self.subTest(kind=source.kind):
                self.assertEqual(overseer_context.STATE_SPACE, source.space)
                self.assertTrue(source.digest.startswith("sha256:"))
                self.assertTrue((plan.plan_state / source.locator).is_file())
                self.assertFalse(source.changed)

    def test_the_status_summary_names_the_pause_and_what_it_waits_on(self) -> None:
        plan = _Plan(self)
        plan.populate()

        summary = plan.build().source("status")

        assert summary is not None
        self.assertIn("paused (blocked)", summary.summary)
        self.assertIn("waiting for a reply", summary.summary)

    def test_a_run_with_no_artifacts_reports_gaps_rather_than_five_sources(
        self,
    ) -> None:
        """An absent family is a fact about the run, not a shorter packet."""
        plan = _Plan(self)

        packet = plan.build()

        self.assertEqual({"status", "run_config"}, _kinds(packet))
        self.assertEqual(
            {
                "questions": "absent",
                "verification": "absent",
                "worker_changes": "absent",
                "supervision": "absent",
            },
            _gaps(packet),
        )
        self.assertFalse(packet.complete)

    def test_a_mailbox_with_no_open_question_is_empty_rather_than_absent(self) -> None:
        plan = _Plan(self)
        plan.ask("which way?", question_id=QUESTION)
        plan.answer(QUESTION)

        packet = plan.build()

        self.assertEqual("empty", _gaps(packet)["questions"])
        self.assertEqual((), packet.questions)

    def test_a_run_that_has_not_committed_falls_back_to_its_status_changes(
        self,
    ) -> None:
        plan = _Plan(self)
        plan.write_turn(1)

        source = plan.build().source("worker_changes")

        assert source is not None
        self.assertIn("turn-0001", source.locator)
        self.assertIn("8: pending -> complete", source.summary)

    def test_a_commit_wins_over_a_turn_for_worker_changes(self) -> None:
        plan = _Plan(self)
        plan.write_turn(1)
        plan.write_commit(1)

        source = plan.build().source("worker_changes")

        assert source is not None
        self.assertIn("commit-0001", source.locator)
        self.assertIn("a" * 40, source.summary)

    def test_verification_is_read_from_the_last_turn_that_recorded_one(self) -> None:
        """A turn that paused before verifying does not erase the last verdict."""
        plan = _Plan(self)
        plan.write_turn(
            1, verification={"session": "7", "passed": True, "reason": "passed"}
        )
        plan.write_turn(2)

        source = plan.build().source("verification")

        assert source is not None
        self.assertIn("turn-0001", source.locator)
        self.assertIn("verification of 7", source.summary)

    def test_an_incident_is_opened_with_one_evidence_row_per_source(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        view = plan.ledger.read_evidence(
            plan_dir=str(plan.plan_dir), grant=plan.grant()
        )
        self.assertEqual(
            {(source.kind, source.locator) for source in packet.sources},
            {
                (row.kind, row.locator)
                for row in view.records
                if row.kind != overseer_context.SCOPE_KIND
            },
        )
        self.assertEqual(
            [packet.incident_id],
            [row.incident_id for row in view.records][:1],
        )

    def test_the_authorized_scope_is_recorded_alongside_the_sources(self) -> None:
        """The repository set is not a file, so nothing else could recover it."""
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        view = plan.ledger.read_evidence(
            plan_dir=str(plan.plan_dir), grant=plan.grant()
        )
        scope_rows = [
            row for row in view.records if row.kind == overseer_context.SCOPE_KIND
        ]
        self.assertEqual([packet.scope], [row.digest for row in scope_rows])
        self.assertNotIn(overseer_context.SCOPE_KIND, _kinds(packet))
        self.assertNotIn(overseer_context.SCOPE_KIND, set(_gaps(packet)))


class EvidenceIsReferencedNotStoredTests(unittest.TestCase):
    """The ledger holds locators and digests. Content stays where it was."""

    def test_no_source_content_reaches_the_ledger_file(self) -> None:
        plan = _Plan(self)
        plan.populate()

        plan.build()

        plan.ledger.close()
        blob = overseer_store_ledger.ledger_path(plan.state_root).read_bytes()
        self.assertNotIn(MARKER.encode("utf-8"), blob)

    def test_the_digest_is_the_digest_of_the_source_as_it_was_read(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        source = packet.source("status")
        assert source is not None
        path = plan.plan_state / source.locator
        self.assertEqual(path.stat().st_size, source.size)
        self.assertEqual(
            source.digest,
            plan.load(packet.incident_id).source("status").digest,  # type: ignore[union-attr]
        )


class PacketsDescribeTheIncidentNowTests(unittest.TestCase):
    """A loaded packet re-reads. It never replays what it was told once."""

    def test_a_question_answered_after_the_build_leaves_the_packet(self) -> None:
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        plan.answer(QUESTION)
        loaded = plan.load(built.incident_id)

        self.assertEqual((QUESTION,), built.questions)
        self.assertEqual((), loaded.questions)

    def test_a_source_that_moved_is_reported_as_changed(self) -> None:
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        plan.write_state(pause_reason="quota_exhausted")
        loaded = plan.load(built.incident_id)

        status = loaded.source("status")
        assert status is not None
        self.assertTrue(status.changed)
        self.assertIn("quota_exhausted", status.summary)
        # Both families are projections of the one state record, so a write to
        # it moves both. Naming only `status` here would assert that a digest is
        # per-family when it is per-file.
        self.assertEqual({"status", "run_config"}, {item.kind for item in loaded.stale})

    def test_a_source_deleted_after_the_build_becomes_a_gap(self) -> None:
        """A recorded reference that no longer resolves is reported, not dropped."""
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        locator = built.source("verification").locator  # type: ignore[union-attr]
        (plan.plan_state / locator).unlink()
        loaded = plan.load(built.incident_id)

        self.assertEqual("unreadable", _gaps(loaded)["verification"])
        self.assertNotIn("verification", _kinds(loaded))
        self.assertFalse(loaded.complete)

    def test_a_plan_discovery_can_no_longer_read_is_refused_outright(self) -> None:
        """Not a gap: without a record there is no scope to check the packet against."""
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        (plan.plan_state / events.STATE_FILE_NAME).unlink()
        loaded = plan.load(built.incident_id)

        self.assertEqual("plan_unobserved", loaded.refusal)
        self.assertEqual((), loaded.sources)


class BoundsTests(unittest.TestCase):
    """A packet is bounded by construction, and says when it left something out."""

    def test_fifty_open_questions_stay_inside_the_summary_cap(self) -> None:
        plan = _Plan(self)
        for index in range(50):
            plan.ask(f"question {index} {MARKER}", question_id=question_uuid(index))

        source = plan.build().source("questions")

        assert source is not None
        self.assertLessEqual(len(source.summary), overseer_context.MAX_SUMMARY_CHARS)
        self.assertTrue(source.truncated)
        self.assertIn("50 open questions", source.summary)

    def test_the_identities_follow_the_same_cap(self) -> None:
        plan = _Plan(self)
        for index in range(50):
            plan.ask(f"question {index}", question_id=question_uuid(index))

        packet = plan.build()

        self.assertEqual(overseer_context.MAX_RECORDS, len(packet.questions))

    def test_a_huge_guidance_never_reaches_the_summary(self) -> None:
        plan = _Plan(self)
        plan.guide(MARKER * 5000)

        source = plan.build().source("supervision")

        assert source is not None
        self.assertLessEqual(len(source.summary), overseer_context.MAX_SUMMARY_CHARS)
        self.assertNotIn(MARKER, source.summary)

    def test_expansion_is_capped_and_says_it_was_capped(self) -> None:
        plan = _Plan(self)
        plan.guide(MARKER * 5000)
        packet = plan.build()

        excerpt = plan.expand(packet.incident_id, "supervision", limit=200)

        self.assertLessEqual(len(excerpt.text), 200)
        self.assertTrue(excerpt.truncated)
        self.assertIsNone(excerpt.refusal)

    def test_expansion_shows_more_than_the_summary_did(self) -> None:
        plan = _Plan(self)
        for index in range(10):
            plan.ask(f"question {index}", question_id=question_uuid(index))
        packet = plan.build()

        excerpt = plan.expand(packet.incident_id, "questions")

        self.assertIn(question_uuid(9), excerpt.text)
        self.assertNotIn(question_uuid(9), packet.source("questions").summary)  # type: ignore[union-attr]
        self.assertFalse(excerpt.truncated)

    def test_expanding_reports_a_source_that_moved(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.write_state(pause_reason="quota_exhausted")
        excerpt = plan.expand(packet.incident_id, "status")

        self.assertTrue(excerpt.changed)
        self.assertIn("quota_exhausted", excerpt.text)

    def test_expanding_a_family_the_incident_does_not_carry_is_refused(self) -> None:
        plan = _Plan(self)

        packet = plan.build()
        excerpt = plan.expand(packet.incident_id, "verification")

        self.assertEqual("evidence_missing", excerpt.refusal)
        self.assertEqual("", excerpt.text)

    def test_a_locator_that_escapes_the_state_directory_is_not_followed(self) -> None:
        """A stored row is data. Data that names ``../`` is not a reference."""
        plan = _Plan(self)
        outside = plan.root / "outside.json"
        outside.write_text(json.dumps({"marker": MARKER}), encoding="utf-8")
        plan.ledger.note_policy(config_fingerprint=FINGERPRINT)
        incident = plan.ledger.open_incident(
            grant=plan.grant(),
            plan_dir=str(plan.plan_dir),
            run_id=plan.run_id,
            kind="blocked",
            summary="hand-written evidence",
            evidence=(
                overseer_store_models.EvidenceInput(
                    alias=overseer_context.STATE_SPACE,
                    kind="status",
                    locator="../../../outside.json",
                    digest="sha256:whatever",
                ),
            ),
        )

        excerpt = plan.expand(incident.incident_id, "status")

        self.assertEqual("source_unreadable", excerpt.refusal)
        self.assertNotIn(MARKER, excerpt.text)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
