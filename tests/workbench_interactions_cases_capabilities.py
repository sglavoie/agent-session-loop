from .workbench_interactions_support import *


class CapabilityContractTests(InteractionDeploymentTestCase):
    """What each mode says it can do, in the words it would refuse with."""

    def test_both_modes_report_on_every_operation_the_surface_has(self) -> None:
        for deployment in self.deployments.both:
            with self.subTest(mode=deployment.mode):
                entries = self.capabilities(deployment)
                self.assertEqual(set(workbench_http.OPERATIONS), set(entries))
                for operation, entry in entries.items():
                    # The role is what a client needs to know *before* asking:
                    # an owner route over a reader session is a refusal that
                    # did not have to be made.
                    self.assertTrue(
                        entry.get("role"),
                        f"{operation} was reported without a role",
                    )

    def test_the_laptop_declines_only_service_side_transport(self) -> None:
        # The local provider runs ASL but is not the service a connector uploads
        # to, nor does it hold remote reply bodies for redemption.
        _, body = _request(self.deployments.local, "/api/capabilities")
        self.assertEqual(
            LOCAL_UNAVAILABLE, set(body.get("unavailable_operations") or [])
        )

    def test_the_pi_declines_exactly_the_operations_it_cannot_reach(self) -> None:
        _, body = _request(self.deployments.remote, "/api/capabilities")
        self.assertEqual(
            set(REMOTE_UNAVAILABLE), set(body.get("unavailable_operations") or [])
        )

    def test_nothing_is_declined_without_a_sentence_to_show_for_it(self) -> None:
        # This is what the interface puts under a greyed-out control. A null
        # reason would render as a disabled button with no explanation, which
        # is the state this session exists to remove.
        for deployment in self.deployments.both:
            entries = self.capabilities(deployment)
            for operation, entry in entries.items():
                if entry.get("available"):
                    continue
                with self.subTest(mode=deployment.mode, operation=operation):
                    reason = entry.get("reason")
                    self.assertIsInstance(reason, str)
                    self.assertTrue(str(reason).strip())

    def test_the_advance_warning_is_the_refusal_word_for_word(self) -> None:
        # The promise the front end makes by disabling a control is "clicking
        # this would have told you *that*". The two sentences come from
        # different code paths -- a capability read and a refusal -- and the
        # only thing keeping them identical is that they are the same string.
        remote = self.deployments.remote
        entries = self.capabilities(remote)
        for operation, path, body in UNAVAILABLE_PROBES:
            with self.subTest(operation=operation):
                self.assertFalse(entries[operation]["available"])
                status, refusal = _request(remote, path, method="POST", body=body)
                self.assertEqual(503, status, refusal)
                self.assertEqual("operation-unavailable", refusal.get("reason"))
                self.assertEqual(entries[operation]["reason"], refusal.get("error"))
                self.assertEqual(
                    {"mode": "remote", "operation": operation},
                    refusal.get("details"),
                )

    def test_an_unavailable_operation_is_refused_before_its_arguments(
        self,
    ) -> None:
        # The probes above send empty bodies. If validation ran first they
        # would come back as invalid-request, the sentence would be about a
        # missing field, and the parity assertion above would be testing
        # nothing. This states that dependency rather than relying on it.
        status, refusal = _request(
            self.deployments.remote,
            "/api/plans/archive",
            method="POST",
            body={"plan_keys": "not-a-list", "confirm_count": "not-a-number"},
        )
        self.assertEqual(503, status, refusal)
        self.assertEqual("operation-unavailable", refusal.get("reason"))
