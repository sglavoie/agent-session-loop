"""One path for every registry and archive mutation, and reads that only read.

Session 8's claim has two halves, and they pull in opposite directions. The
first is *consolidation*: pin, unpin, retention, archive, bulk archive, restore,
purge and unlock had grown a copy per surface, and they agreed only because
someone kept them agreeing. The second is *restraint*: one of those paths used
to delete, unasked, as a side effect of a read. Consolidating a deleting read
would have made it a deleting read on every surface at once, so the two halves
had to land together.

**The rules are portable and there is one set of them.**
:class:`PortableArchiveRulesTests` holds :mod:`asl_workbench.archive`: the
traversal rule for an archive id, the two confirmation contracts, the retention
classification, and the refusal-scope table a surface with no status codes
translates through. It imports nothing from ASL, so the Pi half stays loadable.

**Retention marks; it does not remove.**
:class:`RetentionIsAThresholdTests` is the session's stated done-condition held
directly: a review of an archive full of long-expired entries returns them all,
flagged, with every byte still on disk -- and does so however many times it is
run. The deleted maintenance pass is asserted gone rather than merely unwired,
because an unwired deleter is one call site away from returning.

**Destroying takes saying so.** :class:`ExplicitCleanupTests` holds the
confirmation contracts that did *not* change, which is the half a reader of
"retention stopped deleting" could easily mis-read as "deletion got easier". A
purge without a matching count removes nothing; an archive without its path
retyped archives nothing; a plan a run still holds is refused outright.

**One service, several spellings.** :class:`SurfaceParityTests` drives the
shared service, the HTTP routes, and the ``asl workbench`` CLI over the same
registry and asserts they land in the same place: the same serialized write, the
same refusal reason, and -- for the historic ``/api/plan/delete`` route -- the
same archive the modern route produces.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import (
    plans,
    state,
    workbench,
    workbench_archive,
    workbench_server,
)
from asl_workbench import archive as portable_archive

from .support import (
    _http_request,
    _write_lock_file,
    assert_no_runner_import,
    patched_env,
    write_plan,
)

#: A pid this host is not running, so a lock file reads as stale without
#: anything real having to be killed.
DEAD_PID = 4_242_424

#: Fixed so every age in these tests is arithmetic rather than a race with the
#: clock: "expired" means expired by this instant, not by the time the assertion
#: happened to run.
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


def _iso(delta_days: float) -> str:
    """An archive timestamp ``delta_days`` before :data:`NOW`."""
    return (NOW - timedelta(days=delta_days)).isoformat()


class PortableArchiveRulesTests(unittest.TestCase):
    """The rules both halves share, proven without ASL in the room."""

    def test_the_portable_rules_do_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, portable_archive)

    def test_an_archive_id_may_not_leave_the_archive_root(self) -> None:
        # The rule has one definition; `workbench_archive._is_valid_archive_id` calls
        # it. A second copy is how a traversal one surface rejects becomes one
        # another accepts.
        for good in ("20260917T120000Z-abcdef123456", "entry-1"):
            self.assertTrue(portable_archive.valid_archive_id(good), good)
        for bad in ("", "   ", ".", "..", "../escape", "a/b", "a\\b", "a\0b", None):
            self.assertFalse(portable_archive.valid_archive_id(bad), repr(bad))

    def test_the_provider_and_the_rules_reject_the_same_ids(self) -> None:
        for candidate in ("..", "../escape", "a/b", "", "entry-1"):
            self.assertEqual(
                portable_archive.valid_archive_id(candidate),
                workbench_archive._is_valid_archive_id(candidate),
                candidate,
            )

    def test_a_retention_window_must_be_a_positive_whole_number_of_days(
        self,
    ) -> None:
        self.assertEqual(7, portable_archive.require_retention_days(7))
        for bad in (0, -1, True, False, 1.5, "7", None):
            with self.assertRaises(portable_archive.ArchiveRequestError) as ctx:
                portable_archive.require_retention_days(bad)
            self.assertEqual(
                portable_archive.REASON_INVALID_REQUEST, ctx.exception.reason
            )

    def test_a_purge_count_must_equal_the_selection(self) -> None:
        self.assertEqual(2, portable_archive.require_count_confirmation(["a", "b"], 2))
        with self.assertRaises(portable_archive.ArchiveRequestError) as ctx:
            portable_archive.require_count_confirmation(["a", "b"], 3)
        self.assertEqual(
            portable_archive.REASON_CONFIRMATION_MISMATCH, ctx.exception.reason
        )
        self.assertEqual({"confirm_count": 3, "archive_ids": 2}, ctx.exception.details)

    def test_an_archive_confirmation_compares_canonical_paths(self) -> None:
        # Comparing raw strings would let a trailing slash or a `.` segment
        # pass for a confirmation the operator never typed.
        portable_archive.require_path_confirmation("/plans/alpha", "/plans/alpha")
        with self.assertRaises(portable_archive.ArchiveRequestError) as ctx:
            portable_archive.require_path_confirmation("/plans/alpha", "/plans/beta")
        self.assertEqual(
            portable_archive.REASON_CONFIRMATION_MISMATCH, ctx.exception.reason
        )

    def test_retention_classifies_and_carries_no_removal_count(self) -> None:
        review = portable_archive.cleanup_review(
            [
                {"archive_id": "old", "archived_at": _iso(40)},
                {"archive_id": "new", "archived_at": _iso(2)},
                {"archive_id": "exact", "archived_at": _iso(30)},
                {"archive_id": "broken", "archived_at": "not-a-timestamp"},
            ],
            now=NOW,
            retention_days=30,
        )

        self.assertEqual(["old", "exact"], list(review.candidate_ids))
        self.assertEqual(1, review.undated_count)
        self.assertEqual(portable_archive.CLEANUP_RETAINED, review.verdict("new").state)
        self.assertEqual(
            portable_archive.CLEANUP_UNDATED, review.verdict("broken").state
        )
        # The absence is the point: there is no field a caller could read as
        # "the pass ran", because there is no pass.
        payload = review.to_payload()
        self.assertNotIn("removed_count", payload)
        self.assertNotIn("failed_count", payload)

    def test_only_purging_is_destructive_and_it_needs_confirming(self) -> None:
        self.assertEqual(
            {portable_archive.OPERATION_PURGE},
            set(portable_archive.DESTRUCTIVE_OPERATIONS),
        )
        for operation in portable_archive.OPERATIONS:
            if portable_archive.is_destructive(operation):
                self.assertTrue(
                    portable_archive.requires_confirmation(operation), operation
                )

    def test_every_reason_has_a_scope_and_an_unknown_one_is_not_the_clients_fault(
        self,
    ) -> None:
        for reason, scope in portable_archive.SCOPE_FOR_REASON.items():
            self.assertIn(scope, portable_archive.REFUSAL_SCOPES, reason)
            self.assertIn(scope, portable_archive.STATUS_FOR_SCOPE, reason)
        # Telling a client to rewrite a body that was fine is the worse error,
        # so an unrecognised refusal is "unavailable", not "bad request".
        self.assertEqual(
            portable_archive.SCOPE_UNAVAILABLE,
            portable_archive.scope_for_reason("something-new"),
        )


class _ArchiveFixture(unittest.TestCase):
    """A state root, a registry, and a plan ready to archive."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = self.tmp_path / workbench.REGISTRY_FILE_NAME
        self.plan_dir = self.tmp_path / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans.canonicalize_path(self.plan_dir)

    def make_plan(self, name: str) -> Path:
        plan_dir = self.tmp_path / "plans" / name
        write_plan(plan_dir, ("pending", "pending"))
        return plans.canonicalize_path(plan_dir)

    def archive_one(self, plan_dir: Path | None = None) -> str:
        target = self.canonical if plan_dir is None else plan_dir
        result = workbench_archive.archive_one(
            str(target),
            str(target),
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        return result.archive_id

    def backdate(self, archive_id: str, *, days: float) -> None:
        meta_path = (
            workbench_archive.archive_entry_dir(archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["archived_at"] = _iso(days)
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

    def archive_tree(self) -> dict[str, int]:
        """Every path under the archive root with its size, for exact compare."""
        root = workbench_archive.archive_root(self.state_dir)
        if not root.exists():
            return {}
        return {
            str(path.relative_to(root)): (path.stat().st_size if path.is_file() else -1)
            for path in sorted(root.rglob("*"))
        }

    def lock_path(self) -> Path:
        return (
            state.plan_state_dir(self.canonical, self.state_dir) / state.LOCK_FILE_NAME
        )

    def review(self, *, now: datetime = NOW) -> workbench_archive.ArchiveView:
        return workbench_archive.review_archive(
            state_dir=self.state_dir, registry_path=self.registry_path, now=now
        )


class RetentionIsAThresholdTests(_ArchiveFixture):
    """Browsing never deletes. The session's done-condition, held directly."""

    def test_the_deleting_maintenance_pass_is_gone_not_merely_unwired(self) -> None:
        # Asserted on the module rather than on a call site: a pass that still
        # exists is one import away from being called again, and the next
        # surface to grow an archive view is the one that would call it.
        self.assertFalse(hasattr(workbench, "maintain_archive"))

    def test_reviewing_a_wholly_expired_archive_removes_nothing(self) -> None:
        ids = [self.archive_one(self.make_plan(name)) for name in ("a", "b", "c")]
        for archive_id in ids:
            self.backdate(archive_id, days=400)
        before = self.archive_tree()

        view = self.review()

        self.assertEqual(sorted(ids), sorted(view.review.candidate_ids))
        self.assertEqual(len(ids), view.review.candidate_count)
        self.assertEqual(before, self.archive_tree())

    def test_reviewing_repeatedly_is_still_a_read(self) -> None:
        # Once could be luck. The Pi half polls, so the property that matters
        # is that the hundredth read is the same as the first.
        archive_id = self.archive_one()
        self.backdate(archive_id, days=400)
        before = self.archive_tree()

        for _ in range(5):
            self.review()

        self.assertEqual(before, self.archive_tree())

    def test_each_entry_reports_which_side_of_the_window_it_is_on(self) -> None:
        old = self.archive_one(self.make_plan("old"))
        fresh = self.archive_one(self.make_plan("fresh"))
        self.backdate(old, days=90)
        self.backdate(fresh, days=1)

        payloads = {
            entry["archive_id"]: entry for entry in self.review().entry_payloads()
        }

        self.assertEqual("eligible", payloads[old]["cleanup_state"])
        self.assertTrue(payloads[old]["cleanup_candidate"])
        self.assertEqual("retained", payloads[fresh]["cleanup_state"])
        self.assertFalse(payloads[fresh]["cleanup_candidate"])

    def test_shortening_retention_reclassifies_and_is_reversible(self) -> None:
        # Under the old pass this was a one-way door: shortening the window
        # emptied everything it had just expired, and widening it back restored
        # nothing. The entry is still there to widen back to.
        archive_id = self.archive_one()
        self.backdate(archive_id, days=10)
        self.assertEqual([], list(self.review().review.candidate_ids))

        workbench_archive.set_retention(1, registry_path=self.registry_path)
        self.assertEqual([archive_id], list(self.review().review.candidate_ids))

        workbench_archive.set_retention(365, registry_path=self.registry_path)
        self.assertEqual([], list(self.review().review.candidate_ids))
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )

    def test_the_retention_window_reported_is_the_live_one(self) -> None:
        self.archive_one()
        workbench_archive.set_retention(3, registry_path=self.registry_path)

        self.assertEqual(3, self.review().retention_days)

    def test_an_unreadable_archive_date_is_never_a_cleanup_candidate(self) -> None:
        # "I cannot tell how old this is" must not resolve to "old enough".
        archive_id = self.archive_one()
        meta_path = (
            workbench_archive.archive_entry_dir(archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["archived_at"] = "whenever"
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        review = self.review().review

        self.assertEqual([], list(review.candidate_ids))
        self.assertEqual(1, review.undated_count)


class ExplicitCleanupTests(_ArchiveFixture):
    """The confirmations that did not change, held so they cannot drift."""

    def test_purging_without_a_matching_count_removes_nothing(self) -> None:
        # Pinned by `changes.KEY_EXPLICIT_ARCHIVE_CLEANUP`. Retention stopping
        # deleting is not deletion getting easier: the count is checked before
        # a single entry is touched, so a mismatch is a no-op on disk.
        ids = [self.archive_one(self.make_plan(name)) for name in ("a", "b")]
        before = self.archive_tree()

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.purge_entries(ids, 1, state_dir=self.state_dir)

        self.assertEqual("confirmation-mismatch", ctx.exception.reason)
        self.assertEqual(400, ctx.exception.status)
        self.assertEqual(before, self.archive_tree())

    def test_purging_with_a_matching_count_empties_the_selection(self) -> None:
        kept = self.archive_one(self.make_plan("kept"))
        doomed = self.archive_one(self.make_plan("doomed"))

        result = workbench_archive.purge_entries([doomed], 1, state_dir=self.state_dir)

        self.assertEqual(1, result.removed_count)
        self.assertEqual(0, result.failed_count)
        self.assertFalse(
            workbench_archive.archive_entry_dir(doomed, self.state_dir).exists()
        )
        self.assertTrue(
            workbench_archive.archive_entry_dir(kept, self.state_dir).exists()
        )

    def test_one_bad_id_in_a_purge_does_not_spare_the_rest(self) -> None:
        # Past the confirmation gate each id is its own outcome: an id that
        # names nothing is a row saying so, not a reason to abandon the others.
        good = self.archive_one()

        result = workbench_archive.purge_entries(
            [good, "../escape"], 2, state_dir=self.state_dir
        )

        by_id = {outcome.archive_id: outcome for outcome in result.outcomes}
        self.assertTrue(by_id[good].removed)
        self.assertIsNotNone(by_id["../escape"].error)
        self.assertEqual(1, result.removed_count)
        self.assertEqual(1, result.failed_count)

    def test_archiving_without_the_path_retyped_archives_nothing(self) -> None:
        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.archive_one(
                str(self.canonical),
                str(self.canonical / "not-the-same"),
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                include_default_dirs=False,
            )

        self.assertEqual("confirmation-mismatch", ctx.exception.reason)
        self.assertTrue(self.canonical.exists())
        self.assertEqual({}, self.archive_tree())

    def test_a_plan_a_run_still_holds_is_refused(self) -> None:
        # The safety gate is ASL's answer, asked once. Two surfaces asking it
        # separately is how one of them ends up archiving a live run.
        _write_lock_file(self.canonical, self.state_dir, pid=os.getpid())

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.archive_one(
                str(self.canonical),
                str(self.canonical),
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                include_default_dirs=False,
            )

        self.assertEqual("active-lock", ctx.exception.reason)
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(portable_archive.SCOPE_PLAN, ctx.exception.scope)
        self.assertTrue(self.canonical.exists())

    def test_a_bulk_archive_reports_one_row_per_plan(self) -> None:
        second = self.make_plan("beta")
        blocked = self.make_plan("blocked")
        _write_lock_file(blocked, self.state_dir, pid=os.getpid())

        bulk = workbench_archive.archive_many(
            [str(self.canonical), str(blocked), str(second)],
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )

        self.assertEqual(3, len(bulk.outcomes))
        self.assertEqual(2, bulk.archived_count)
        self.assertEqual(1, bulk.failed_count)
        by_target = {outcome.target: outcome for outcome in bulk.outcomes}
        self.assertFalse(by_target[str(blocked)].ok)
        self.assertEqual("active-lock", by_target[str(blocked)].reason)
        # The blocked plan is still there; the two beside it are gone.
        self.assertTrue(blocked.exists())
        self.assertFalse(self.canonical.exists())
        self.assertFalse(second.exists())

    def test_a_restore_refuses_rather_than_overwriting_a_live_plan(self) -> None:
        archive_id = self.archive_one()
        write_plan(self.canonical, ("pending",))

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.restore_entry(
                archive_id,
                state_dir=self.state_dir,
                registry_path=self.registry_path,
            )

        self.assertEqual("restore-conflict", ctx.exception.reason)
        self.assertEqual(409, ctx.exception.status)
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )

    def test_a_restore_puts_the_plan_back_where_it_came_from(self) -> None:
        archive_id = self.archive_one()
        self.assertFalse(self.canonical.exists())

        result = workbench_archive.restore_entry(
            archive_id, state_dir=self.state_dir, registry_path=self.registry_path
        )

        self.assertTrue(result.plan_restored)
        self.assertTrue(self.canonical.exists())
        self.assertEqual({}, self.archive_tree())

    def test_restoring_something_that_is_not_there_is_a_miss_not_a_failure(
        self,
    ) -> None:
        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.restore_entry(
                "20000101T000000Z-deadbeefcafe",
                state_dir=self.state_dir,
                registry_path=self.registry_path,
            )

        self.assertEqual("not-found", ctx.exception.reason)
        self.assertEqual(404, ctx.exception.status)

    def test_unlock_is_the_one_place_reclaim_is_still_deliberate(self) -> None:
        # Every read stopped reclaiming in sessions 2 and 4. This is what is
        # left: an operator saying the lock's owner is gone.
        _write_lock_file(self.canonical, self.state_dir, pid=DEAD_PID)

        result = workbench_archive.unlock(str(self.canonical), state_dir=self.state_dir)

        self.assertTrue(result.removed)
        self.assertTrue(result.was_stale)
        self.assertFalse(self.lock_path().exists())

    def test_unlock_refuses_a_lock_whose_owner_is_alive(self) -> None:
        _write_lock_file(self.canonical, self.state_dir, pid=os.getpid())

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.unlock(str(self.canonical), state_dir=self.state_dir)

        self.assertEqual("active-lock", ctx.exception.reason)
        self.assertEqual(409, ctx.exception.status)
        self.assertTrue(self.lock_path().exists())


class SurfaceParityTests(_ArchiveFixture):
    """The HTTP routes and the CLI are renderings of the one service."""

    def setUp(self) -> None:
        super().setUp()
        self.servers: list[workbench_server.WorkbenchHTTPServer] = []

    def start_server(self) -> workbench_server.WorkbenchHTTPServer:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(),
            include_default_dirs=False,
            token=None,
        )
        thread = threading.Thread(
            target=server.serve_forever, name="workbench-archive-test", daemon=True
        )
        thread.start()

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        self.servers.append(server)
        return server

    def url(self, server: workbench_server.WorkbenchHTTPServer, path: str) -> str:
        return f"http://{server.bound_host}:{server.bound_port}{path}"

    def auth(self, server: workbench_server.WorkbenchHTTPServer) -> dict[str, str]:
        return {"Authorization": f"Bearer {server.token}"}

    def test_the_historic_delete_route_archives_exactly_as_the_modern_one_does(
        self,
    ) -> None:
        # `/api/plan/delete` predates the archive. Keeping it mapped here is
        # what stops an old bookmark or an old script from meaning "destroy".
        second = self.make_plan("beta")
        server = self.start_server()

        status_legacy, legacy, _ = _http_request(
            self.url(server, "/api/plan/delete"),
            method="POST",
            body={
                "plan_dir": str(self.canonical),
                "confirm_plan_dir": str(self.canonical),
            },
            headers=self.auth(server),
        )
        status_modern, modern, _ = _http_request(
            self.url(server, "/api/plan/archive"),
            method="POST",
            body={"plan_dir": str(second), "confirm_plan_dir": str(second)},
            headers=self.auth(server),
        )

        self.assertEqual(200, status_legacy, legacy)
        self.assertEqual(200, status_modern, modern)
        # Both produced an archive entry rather than a removal: the plans are
        # gone from their origins and restorable from the archive.
        self.assertEqual(sorted(legacy.keys()), sorted(modern.keys()))
        self.assertTrue(legacy["plan_archived"])
        self.assertEqual(
            2, len(workbench_archive.list_archive_entries(self.state_dir).entries)
        )
        workbench_archive.restore_entry(
            legacy["archive_id"],
            state_dir=self.state_dir,
            registry_path=self.registry_path,
        )
        self.assertTrue(self.canonical.exists())

    def test_the_legacy_route_keeps_its_confirmation_contract(self) -> None:
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/plan/delete"),
            method="POST",
            body={
                "plan_dir": str(self.canonical),
                "confirm_plan_dir": str(self.tmp_path),
            },
            headers=self.auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical.exists())

    def test_a_refusal_carries_the_same_reason_and_the_status_its_scope_earns(
        self,
    ) -> None:
        _write_lock_file(self.canonical, self.state_dir, pid=os.getpid())
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/plan/archive"),
            method="POST",
            body={
                "plan_dir": str(self.canonical),
                "confirm_plan_dir": str(self.canonical),
            },
            headers=self.auth(server),
        )
        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.archive_one(
                str(self.canonical),
                str(self.canonical),
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                include_default_dirs=False,
            )

        self.assertEqual(ctx.exception.reason, body["reason"])
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(portable_archive.status_for_scope(ctx.exception.scope), status)

    def test_the_route_and_the_cli_pin_into_the_same_registry(self) -> None:
        # The two surfaces reach the registry by different routes -- the server
        # holds an explicit path, the CLI resolves the default -- so the test
        # points them both at one file and checks the second pin did not land
        # on top of the first. That is the serialization claim in the small.
        config_home = self.tmp_path / "config"
        with patched_env(XDG_CONFIG_HOME=str(config_home)):
            registry_path = workbench.default_registry_path()
            self.registry_path = registry_path
            server = self.start_server()
            second = self.make_plan("beta")

            status, body, _ = _http_request(
                self.url(server, "/api/registry/pin"),
                method="POST",
                body={"plan_dir": str(self.canonical), "label": "Alpha"},
                headers=self.auth(server),
            )
            self.assertEqual(200, status, body)

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                rc = workbench_archive.workbench_pin_from_args(
                    argparse.Namespace(plan_dir=str(second), label="Beta", cwd=None)
                )
            self.assertEqual(0, rc)
            self.assertIn(str(registry_path), stdout.getvalue())

            reloaded = workbench.load_registry(registry_path)

        self.assertEqual(
            [str(self.canonical), str(second)],
            [pin.plan_dir for pin in reloaded.pins],
        )
        self.assertEqual(["Alpha", "Beta"], [pin.label for pin in reloaded.pins])

    def test_the_route_and_the_cli_refuse_a_bad_plan_for_the_same_reason(
        self,
    ) -> None:
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/registry/pin"),
            method="POST",
            body={"plan_dir": "   "},
            headers=self.auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-plan", body["reason"])

        # Same refusal, rendered as an exit code and a stderr line rather than
        # a status and a body. The service raised it once for both.
        with patched_env(XDG_CONFIG_HOME=str(self.tmp_path / "config")):
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                rc = workbench_archive.workbench_pin_from_args(
                    argparse.Namespace(plan_dir="   ", label=None, cwd=None)
                )
        self.assertEqual(1, rc)
        self.assertIn("workbench pin", stderr.getvalue())

    def test_the_archive_route_reports_the_review_it_did_not_act_on(self) -> None:
        archive_id = self.archive_one()
        self.backdate(archive_id, days=400)
        before = self.archive_tree()
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/archive"), headers=self.auth(server)
        )

        self.assertEqual(200, status)
        self.assertEqual([archive_id], body["archive_cleanup"]["candidate_ids"])
        self.assertEqual(1, body["archive_cleanup"]["candidate_count"])
        self.assertEqual(30, body["archive_retention_days"])
        self.assertEqual(before, self.archive_tree())

    def test_the_settings_route_moves_the_threshold_and_empties_nothing(
        self,
    ) -> None:
        archive_id = self.archive_one()
        self.backdate(archive_id, days=5)
        server = self.start_server()

        status, settings, _ = _http_request(
            self.url(server, "/api/archive/settings"),
            method="POST",
            body={"archive_retention_days": 1},
            headers=self.auth(server),
        )
        self.assertEqual(200, status, settings)
        self.assertEqual(1, settings["archive_retention_days"])

        _, listing, _ = _http_request(
            self.url(server, "/api/archive"), headers=self.auth(server)
        )

        self.assertEqual([archive_id], listing["archive_cleanup"]["candidate_ids"])
        self.assertEqual(
            [archive_id], [entry["archive_id"] for entry in listing["entries"]]
        )
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )

    def test_the_purge_route_keeps_its_typed_count(self) -> None:
        archive_id = self.archive_one()
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/archive/delete"),
            method="POST",
            body={"archive_ids": [archive_id], "confirm_count": 2},
            headers=self.auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
