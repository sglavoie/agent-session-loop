from .workbench_interactions_support import *


class OperationIdentityTests(InteractionDeploymentTestCase):
    """A mutation nobody has confirmed, and the id that makes it recoverable."""

    def test_a_local_control_is_recorded_rather_than_run_when_nothing_holds_it(
        self,
    ) -> None:
        # The 202 is not a remote-only shape. A control queued against a plan
        # whose runner is not listening is written down on the laptop too, and
        # a front end that drew `ok: true` as "stopped" would be wrong in the
        # deployment it was written for.
        local = self.deployments.local
        row = self.row_for(local, "alpha")
        status, body = _request(
            local,
            "/api/control/stop-after-current",
            method="POST",
            body=_plan_body(row),
        )
        self.assertEqual(202, status, body)
        self.assertTrue(body.get("ok"))
        queued = body.get("queued_command") or {}
        self.assertTrue(queued.get("id"), body)
        # And it says so plainly: nothing read this, so nothing has acted on it.
        signal = body.get("signal") or {}
        self.assertFalse(signal.get("delivered", True), body)

    def test_the_pi_records_a_mutation_under_the_id_the_client_minted(
        self,
    ) -> None:
        remote = self.deployments.remote
        row = self.row_for(remote, "charlie")
        operation_id = "interactions-remote-unlock"
        status, body = _request(
            remote,
            "/api/unlock",
            method="POST",
            body=_plan_body(row, operation_id=operation_id),
        )
        self.assertEqual(202, status, body)
        operation = body.get("operation") or {}
        self.assertEqual(operation_id, operation.get("operation_id"))
        self.assertEqual("pending", operation.get("status"))
        self.assertFalse(body.get("replay"), "the first submission is not a replay")

    def test_resubmitting_one_id_replays_it_instead_of_doing_it_twice(
        self,
    ) -> None:
        # This is what makes a retry safe after an answer went missing, and it
        # is why the front end writes the id down rather than minting a new one
        # on the second click. Two unlocks would be two, and the operator would
        # have no way to tell.
        remote = self.deployments.remote
        row = self.row_for(remote, "bravo")
        body = _plan_body(row, operation_id="interactions-replayed-unlock")
        first_status, first = _request(remote, "/api/unlock", method="POST", body=body)
        second_status, second = _request(
            remote, "/api/unlock", method="POST", body=body
        )
        self.assertEqual(202, first_status, first)
        self.assertEqual(202, second_status, second)
        self.assertFalse(first.get("replay"))
        self.assertTrue(second.get("replay"), "the second submission must replay")
        self.assertEqual(
            (first.get("operation") or {}).get("operation_id"),
            (second.get("operation") or {}).get("operation_id"),
        )

    def test_the_service_publishes_what_it_still_holds_for_a_plan(self) -> None:
        # The receipts in session storage belong to one tab. This is the part
        # that survives the tab: a mutation sent from a phone that has since
        # been closed is still visible from a laptop, because the plan read
        # carries it.
        remote = self.deployments.remote
        row = self.row_for(remote, "alpha")
        operation_id = "interactions-outstanding-unlock"
        _request(
            remote,
            "/api/unlock",
            method="POST",
            body=_plan_body(row, operation_id=operation_id),
        )
        status, body = _request(remote, "/api/plan?" + _plan_query(row))
        self.assertEqual(200, status, body)
        outstanding = body.get("outstanding_operation") or {}
        self.assertEqual(operation_id, outstanding.get("operation_id"))
        self.assertEqual("pending", outstanding.get("status"))
        # Never confused with "done": an outstanding record is the open
        # question, and the status word is the only thing entitled to close it.
        self.assertNotEqual("complete", outstanding.get("status"))


class ArchiveReviewTests(InteractionDeploymentTestCase):
    """The destructive end of the matrix, and the gates in front of it."""

    def listing(self) -> dict[str, dict[str, Any]]:
        status, body = _request(self.deployments.local, "/api/archive")
        self.assertEqual(200, status, body)
        return {
            entry["archive_id"]: entry
            for entry in body.get("entries") or []
            if isinstance(entry, dict)
        }

    def test_the_archive_says_which_entries_have_passed_their_window(
        self,
    ) -> None:
        # The review panel is built from this verdict, so an archive that
        # reported neither state would make the panel unreachable and every
        # assertion about it vacuous.
        entries = self.listing()
        expired, retained = SHELL_ARCHIVE_ENTRIES
        self.assertEqual(
            "eligible", entries[str(expired["archive_id"])]["cleanup_state"]
        )
        self.assertEqual(
            "retained", entries[str(retained["archive_id"])]["cleanup_state"]
        )

    def test_browsing_the_archive_removes_nothing_from_it(self) -> None:
        before = set(self.listing())
        _request(self.deployments.local, "/api/archive")
        self.assertEqual(before, set(self.listing()))

    def test_a_purge_without_its_confirmation_purges_nothing(self) -> None:
        # The typed count is the last gate before something is gone for good.
        # A mismatch has to leave the archive exactly as it was, rather than
        # purging the entries it could agree about.
        expired = str(SHELL_ARCHIVE_ENTRIES[0]["archive_id"])
        before = set(self.listing())
        status, body = _request(
            self.deployments.local,
            "/api/archive/delete",
            method="POST",
            body={"archive_ids": [expired], "confirm_count": 7},
        )
        self.assertEqual(400, status, body)
        self.assertEqual("confirmation-mismatch", body.get("reason"))
        self.assertEqual(before, set(self.listing()))

    def test_the_pi_lists_the_same_archive_the_mac_holds(self) -> None:
        # The archive lives on the Mac; remote mode reaches it rather than
        # serving a copy, so both deployments list exactly the same entries.
        status, body = _request(self.deployments.remote, "/api/archive")
        self.assertEqual(200, status, body)
        remote = {str(entry["archive_id"]) for entry in body.get("entries") or ()}
        self.assertEqual(set(self.listing()), remote)
