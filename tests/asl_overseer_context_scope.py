# ruff: noqa: F403, F405
from .asl_overseer_context_support import *


class ScopeTests(unittest.TestCase):
    """A packet is authorized against a repository set and a run."""

    def test_an_unchanged_plan_revalidates(self) -> None:
        plan = _Plan(self)
        plan.populate()

        checked = overseer_context.revalidate(
            packet=plan.build(), candidate=plan.candidate()
        )

        self.assertTrue(checked.fresh)
        self.assertIsNone(checked.reason)

    def test_a_moved_repository_root_changes_the_scope(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(
            repositories=[
                {
                    "alias": "scripts",
                    "path": "scripts",
                    "origin": "github.com/sglavoie/scripts",
                    "resolved_root": str(plan.root / "elsewhere"),
                }
            ]
        )
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertEqual("scope_changed", checked.reason)
        self.assertNotEqual(checked.recorded_scope, checked.current_scope)

    def test_a_new_registry_digest_changes_the_scope(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(registry_digest="e" * 64)
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertEqual("scope_changed", checked.reason)

    def test_a_replaced_run_is_not_the_run_the_packet_describes(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(run_id="run-2")
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertEqual("run_replaced", checked.reason)

    def test_a_pause_is_a_change_of_state_and_not_of_scope(self) -> None:
        """Every packet would be stale on arrival if lifecycle counted."""
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(paused=False, pause_reason=None)
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertTrue(checked.fresh)

    def test_an_unobservable_plan_is_unproven_rather_than_unchanged(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        (plan.plan_state / events.STATE_FILE_NAME).unlink()
        checked = overseer_context.revalidate(
            packet=packet, candidate=plan._candidate()
        )

        self.assertEqual("plan_unobserved", checked.reason)

    def test_loading_refuses_a_packet_whose_scope_moved(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        plan.write_state(registry_digest="e" * 64)
        loaded = plan.load(packet.incident_id)

        self.assertEqual("scope_changed", loaded.refusal)
        self.assertEqual((), loaded.sources)
        self.assertEqual(0, plan.mailbox_reads)

    def test_loading_refuses_a_packet_whose_run_was_replaced(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.write_state(run_id="run-2")
        loaded = plan.load(packet.incident_id)

        self.assertEqual("run_replaced", loaded.refusal)


class AuthorizationTests(unittest.TestCase):
    """Every operation asks first, and a refusal reads nothing."""

    def test_building_without_a_grant_reads_nothing_and_opens_nothing(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build(grant=None)

        self.assertEqual("unauthorized", packet.refusal)
        self.assertEqual((), packet.sources)
        self.assertEqual(0, plan.mailbox_reads)
        self.assertEqual((), plan.ledger.incidents(plan_dir=str(plan.plan_dir)))

    def test_a_grant_for_another_plan_is_not_a_grant_for_this_one(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build(grant=plan.grant(plan_dir=str(plan.root / "elsewhere")))

        self.assertEqual("unauthorized", packet.refusal)
        self.assertEqual(0, plan.mailbox_reads)

    def test_a_mutation_grant_does_not_authorize_a_context_read(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build(
            grant=plan.grant(access=overseer_policy_client.ACCESS_MUTATION)
        )

        self.assertEqual("unauthorized", packet.refusal)
        self.assertEqual(0, plan.mailbox_reads)

    def test_loading_without_a_grant_is_refused_before_any_read(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        loaded = plan.load(packet.incident_id, grant=None)

        self.assertEqual("unauthorized", loaded.refusal)
        self.assertEqual((), loaded.sources)
        self.assertEqual(0, plan.mailbox_reads)

    def test_expanding_without_a_grant_is_refused_before_any_read(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        excerpt = plan.expand(packet.incident_id, "questions", grant=None)

        self.assertEqual("unauthorized", excerpt.refusal)
        self.assertEqual("", excerpt.text)
        self.assertEqual(0, plan.mailbox_reads)

    def test_an_ineligible_run_is_never_assembled(self) -> None:
        """Identity comes before authorization, so there is nothing to ask about."""
        plan = _Plan(self)
        plan.write_state(registry_digest=None)

        with self.assertRaises(overseer_context.ContextError):
            plan.build()


class ExclusionTests(unittest.TestCase):
    """What an operator excludes cannot be loaded or expanded afterwards."""

    def test_a_revoked_plan_cannot_have_its_packet_loaded(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")
        loaded = plan.load(packet.incident_id)

        self.assertEqual("enrollment_revoked", loaded.refusal)
        self.assertEqual((), loaded.sources)
        self.assertEqual(0, plan.mailbox_reads)

    def test_a_revoked_plan_cannot_have_its_packet_expanded(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")
        excerpt = plan.expand(packet.incident_id, "status")

        self.assertEqual("enrollment_revoked", excerpt.refusal)
        self.assertEqual("", excerpt.text)

    def test_a_revoked_plan_cannot_have_a_new_packet_built(self) -> None:
        plan = _Plan(self)
        plan.populate()
        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")

        packet = plan.build()

        self.assertEqual("enrollment_revoked", packet.refusal)
        self.assertEqual(0, plan.mailbox_reads)

    def test_a_policy_change_seals_a_packet_against_its_old_grant(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.ledger.note_policy(config_fingerprint="fp-2")
        loaded = plan.load(packet.incident_id)

        self.assertEqual("policy_superseded", loaded.refusal)

    def test_a_policy_change_seals_a_packet_against_a_new_grant_too(self) -> None:
        """The new grant is valid. The evidence was gathered under the old one."""
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.ledger.note_policy(config_fingerprint="fp-2")
        loaded = plan.load(packet.incident_id, grant=plan.grant(fingerprint="fp-2"))

        self.assertEqual("evidence_missing", loaded.refusal)

    def test_an_explicit_release_is_what_makes_sealed_evidence_readable(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.ledger.note_policy(config_fingerprint="fp-2")
        fresh = plan.grant(fingerprint="fp-2")

        released = plan.ledger.release_evidence(
            plan_dir=str(plan.plan_dir), grant=fresh
        )
        loaded = plan.load(packet.incident_id, grant=fresh)

        self.assertIsNone(released.refusal)
        self.assertIsNone(loaded.refusal)
        self.assertEqual(set(overseer_context.SOURCE_KINDS), _kinds(loaded))


class PermanentExclusionTests(unittest.TestCase):
    """Exclusion outlives the policy that was in force when it happened."""

    def test_a_revoked_plan_can_never_release_its_sealed_evidence(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.ledger.note_policy(config_fingerprint="fp-2")
        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")

        released = plan.ledger.release_evidence(
            plan_dir=str(plan.plan_dir), grant=plan.grant(fingerprint="fp-2")
        )
        loaded = plan.load(packet.incident_id, grant=plan.grant(fingerprint="fp-2"))

        self.assertEqual("enrollment_revoked", released.refusal)
        self.assertEqual(0, released.released)
        self.assertEqual("enrollment_revoked", loaded.refusal)

    def test_exclusion_survives_reopening_the_ledger(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")
        plan.ledger.close()

        reopened = overseer_store_ledger.open_ledger(
            state_dir=plan.state_root, now=plan.iso_now
        )
        self.addCleanup(reopened.close)
        loaded = plan.load(packet.incident_id, ledger=reopened)

        self.assertEqual("enrollment_revoked", loaded.refusal)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
