"""Every action the shared interface offers, and what it is allowed to claim.

Session 15 put one front end in front of both deployments and session 16 made
it read the same way on a laptop and on a phone. What was still missing is the
other half of the matrix: the *mutations*. Nine of them, across launch, reply,
the run controls, the redirect journal, pins, recovery and the archive -- and
each one is offered by a mode that may not carry it out, aimed at a run that
may have moved on, and answered by a service that may only have written it
down.

The failure this module exists to catch is a front end that quietly overclaims.
A ``202`` drawn as success is the worst version of it: the operator reads "done"
about work nobody has confirmed ran, and the whole reason the remote provider
hands down rather than executes is that nobody can confirm it yet. A disabled
button with no sentence under it is the same failure in a smaller way -- it
tells the operator that something is impossible without telling them which of
the three reasons applies.

So the checks here come in three kinds:

* **Contract.** Read off the source, because "every control in the markup is a
  control this application knows about" is a fact about the tree and is not
  observable from any single interaction.
* **Capability.** Asked of both live deployments: what each mode says it can
  do, and whether the sentence it hands the interface in *advance* is the same
  sentence it would have given on refusal. A greyed-out control whose tooltip
  disagrees with the refusal is a control explaining the wrong thing.
* **Identity and archive.** That a recorded mutation carries an id, that
  resubmitting it replays rather than duplicates, that the service publishes
  what it still holds, and that the destructive end of the archive refuses
  without its confirmation.

The Playwright harness in ``tests/workbench_browser/interactions.spec.js``
drives the same two servers through a real browser, at phone and desktop
widths, for the parts that are only true once a human clicks them: focus after
a confirm dialog, a receipt that survives a reload, a disabled control's reason
being readable without a hover. This module is what runs everywhere, including
where no browser is installed.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from asl_workbench import http as workbench_http
from tests import workbench_support
from tests.workbench_support import (
    SHELL_ARCHIVE_ENTRIES,
    ShellDeployment,
    ShellDeployments,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = Path(workbench_http.STATIC_ROOT)
MODULES_ROOT = STATIC_ROOT / "modules"
INDEX_HTML = STATIC_ROOT / "index.html"
OPERATIONS_MODULE = MODULES_ROOT / "operations.js"

#: The one route the shared mutation path does not carry, and why.
#:
#: A correction is submitted against a freshness proof ASL issued, it has a
#: conflict answer nothing else has (the journal already holds this id under
#: different words), and the draft has to survive every one of those answers.
#: It writes the same receipts and obeys the same in-flight rule; what it does
#: not do is route through :func:`runMutation`, and pinning that here is what
#: keeps the exemption a decision rather than an oversight.
BESPOKE_MUTATIONS: frozenset[str] = frozenset({"/api/control/redirect"})

#: The keys a stored receipt is allowed to have.
#:
#: This is a privacy boundary, not a schema convenience. Reply text and
#: correction drafts are memory-only by design; a receipt that carried the
#: words alongside the id would put them in session storage and undo that
#: promise on the first refresh.
RECEIPT_FIELDS: frozenset[str] = frozenset(
    {"operation_id", "action", "plan", "outcome", "status", "reason", "settled"}
)

#: What the Pi does not carry out, exactly. Twelve operations, and the list is
#: spelled rather than derived: the point of asserting it is that a mutation
#: silently becoming available (or unavailable) on the remote deployment is a
#: change to what the Pi is for, and should have to be typed here.
REMOTE_UNAVAILABLE: frozenset[str] = frozenset(
    {
        "archive-delete",
        "archive-list",
        "archive-restore",
        "archive-settings",
        "conversation-recovery",
        "launch",
        "plan-archive",
        "plans-archive",
        "redirect-status",
        "registry",
        "registry-pin",
        "registry-unpin",
    }
)

#: An unavailable operation with a route that takes no other argument, so the
#: refusal under test is the capability one rather than a validation error
#: reached first. Mapped to the body a caller would have sent.
UNAVAILABLE_PROBES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("launch", "/api/launch", {}),
    ("registry-pin", "/api/registry/pin", {}),
    ("registry-unpin", "/api/registry/unpin", {}),
    ("plan-archive", "/api/plan/archive", {}),
    ("plans-archive", "/api/plans/archive", {}),
    ("archive-restore", "/api/archive/restore", {}),
    ("archive-delete", "/api/archive/delete", {}),
    ("archive-settings", "/api/archive/settings", {}),
)


def _request(
    deployment: ShellDeployment,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
) -> tuple[int, dict[str, Any]]:
    """One request to one deployment, with the credentials that mode wants.

    Refusals come back rather than raise, because a refusal is an answer and
    most of the tests below are about which one was given.
    """
    headers = dict(deployment.headers)
    payload: bytes | None = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        deployment.url(path), data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as error:
        # `HTTPError` is the response object and holds the socket until it is
        # closed; left to the collector it surfaces as a ResourceWarning
        # attributed to whatever ran next.
        with error:
            raw = error.read()
            status = int(error.code)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _plan_query(row: dict[str, Any]) -> str:
    """Address a plan the way the loaded deployment addresses it."""
    if row.get("plan_dir"):
        return urlencode({"plan_dir": row["plan_dir"]})
    return urlencode({"plan_key": row["plan_key"]})


def _plan_body(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The plan identity a mutation body carries, in this mode's spelling."""
    body: dict[str, Any] = {}
    if row.get("plan_dir"):
        body["plan_dir"] = row["plan_dir"]
    else:
        body["plan_key"] = row["plan_key"]
    if row.get("run_key"):
        body["run_key"] = row["run_key"]
    body.update(extra)
    return body


def _module_source(name: str) -> str:
    return (MODULES_ROOT / name).read_text(encoding="utf-8")


def _declared_actions() -> dict[str, dict[str, str]]:
    """The ``ACTIONS`` table in ``operations.js``, read as data.

    Parsed out of the source rather than mirrored in a constant here, because
    a copy would let the table and the checks drift apart and still pass. The
    shape it parses is deliberately narrow: an entry that stopped matching
    would fail the count assertion below rather than be skipped.
    """
    source = OPERATIONS_MODULE.read_text(encoding="utf-8")
    table = re.search(r"export const ACTIONS = \{(.*?)\n\};", source, re.DOTALL)
    assert table is not None, "operations.js must export an ACTIONS table"
    entries: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        r'\n  "?(?P<name>[a-z-]+)"?: \{(?P<fields>.*?)\n  \},', re.DOTALL
    )
    for match in pattern.finditer(table.group(1) + "\n  },"):
        fields = dict(
            re.findall(
                r'(operation|subject|requires|stale): "(.*?)"', match.group("fields")
            )
        )
        entries[match.group("name")] = fields
    return entries


class InteractionContractTests(unittest.TestCase):
    """What the source has to be true of, before any of it is clicked."""

    def test_every_control_in_the_markup_names_an_action_this_app_knows(
        self,
    ) -> None:
        # A button wired to a name nothing answers for is a button that cannot
        # be greyed out, cannot be guarded against a second click, and leaves
        # no receipt. It would look identical to a working one.
        markup = INDEX_HTML.read_text(encoding="utf-8")
        declared = _declared_actions()
        used = set(re.findall(r'data-operation-action="([^"]+)"', markup))
        self.assertTrue(used, "index.html must mark its mutating controls")
        self.assertEqual(
            set(),
            used - set(declared),
            "a control names an action with no entry in ACTIONS",
        )

    def test_every_action_names_an_operation_the_service_publishes(self) -> None:
        # The whole availability mechanism rests on this: the word the table
        # uses is the word `/api/capabilities` answers about. A typo here would
        # mean a control that is *always* enabled, because an unknown operation
        # is reported available.
        declared = _declared_actions()
        self.assertGreaterEqual(
            len(declared), 14, "the ACTIONS table parsed as suspiciously small"
        )
        for name, fields in declared.items():
            with self.subTest(action=name):
                self.assertIn(
                    fields.get("operation"),
                    workbench_http.OPERATIONS,
                    "ACTIONS names an operation the HTTP surface does not have",
                )
                self.assertTrue(
                    fields.get("subject"),
                    "every action needs a subject to be described by",
                )

    def test_an_action_with_a_precondition_carries_its_own_explanation(
        self,
    ) -> None:
        # "Disabled" is not an explanation. The three preconditions are
        # different facts about the run -- it is live, it is idle, it is
        # waiting for you -- and each has to say which one failed.
        for name, fields in _declared_actions().items():
            with self.subTest(action=name):
                if fields.get("requires"):
                    self.assertTrue(
                        fields.get("stale"),
                        "an action with a precondition needs a stale sentence",
                    )

    def test_every_mutation_goes_through_the_one_guarded_path(self) -> None:
        # One submit path is what makes the double-submit guard, the receipt,
        # the focus restoration and the capability re-read universal rather
        # than something each caller remembers. The exemption is named, so a
        # second one has to be argued for rather than added.
        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(MODULES_ROOT.glob("*.js"))
            if path.name not in {"api.js", "operations.js"}
        }
        corpus = "\n".join(sources.values())
        for route in workbench_http.ROUTES:
            if not route.mutating or route.path in BESPOKE_MUTATIONS:
                continue
            if f'"{route.path}"' not in corpus:
                # Not every route the surface offers is reachable from a
                # control; `/api/plan/delete` is the pre-archive spelling the
                # shell no longer calls. Absence is fine, a second path is not.
                continue
            with self.subTest(route=route.path):
                self.assertIn(
                    f'path: "{route.path}"',
                    corpus,
                    "a mutating route must be submitted as a runMutation spec",
                )
        # And never as a plain read. `apiRequest` returns a parsed body with no
        # outcome attached, so a mutation sent through it cannot tell a result
        # from a 202 -- which is exactly the overclaim this session removed.
        for name, source in sources.items():
            for target in re.findall(r'apiRequest\(\s*"([^"]+)"', source):
                with self.subTest(module=name, target=target):
                    self.assertNotIn(
                        target,
                        {
                            route.path
                            for route in workbench_http.ROUTES
                            if route.mutating
                        }
                        - BESPOKE_MUTATIONS,
                        "a mutating route may not be sent as an unclassified read",
                    )

    def test_only_the_shared_path_and_the_redirect_composer_submit(self) -> None:
        # `submit` is the classifier that separates a result from a recorded
        # 202 from an unreachable link. A module calling it directly would be a
        # module deciding for itself what an answer means.
        for path in sorted(MODULES_ROOT.glob("*.js")):
            if path.name in {"api.js", "operations.js"}:
                continue
            with self.subTest(module=path.name):
                self.assertNotIn(
                    "\n  submit,\n",
                    path.read_text(encoding="utf-8"),
                    "only operations.js may classify a submission",
                )

    def test_a_recovery_card_that_acts_is_the_same_action_as_the_bar(
        self,
    ) -> None:
        # The Recovery Center offers a second door onto two of the controls.
        # A door that skipped the availability pass would be a button offered
        # to someone who has just been told something went wrong, refused only
        # after they pressed it -- the worst place to make that mistake.
        source = _module_source("recovery.js")
        declared = _declared_actions()
        table = re.search(
            r"const RECOVERY_CARD_ACTIONS = \{(.*?)\n\};", source, re.DOTALL
        )
        self.assertIsNotNone(table, "recovery cards must name the actions they are")
        assert table is not None
        for name in set(re.findall(r':\s*"([a-z-]+)"', table.group(1))):
            with self.subTest(action=name):
                self.assertIn(name, declared)
        self.assertIn('setAttribute("data-operation-action", name)', source)
        self.assertIn("applyActionAvailability(host)", source)

    def test_a_stored_receipt_carries_an_identity_and_no_prose(self) -> None:
        # The privacy promise and the recovery promise pull in opposite
        # directions, and this is where they are reconciled: the id is written
        # down because a reload cannot reconstruct it, and nothing else is,
        # because a reload losing the words is the point.
        source = OPERATIONS_MODULE.read_text(encoding="utf-8")
        receipt = re.search(r"const receipt = \{(.*?)\n  \};", source, re.DOTALL)
        self.assertIsNotNone(receipt, "rememberReceipt must build one flat record")
        assert receipt is not None
        fields = set(re.findall(r"\n    ([a-z_]+):", receipt.group(1)))
        self.assertEqual(
            RECEIPT_FIELDS,
            fields,
            "a receipt may carry an identity and an outcome, and no prose",
        )

    def test_receipts_are_the_only_thing_written_to_session_storage_here(
        self,
    ) -> None:
        source = OPERATIONS_MODULE.read_text(encoding="utf-8")
        keys = set(re.findall(r"sessionStorage\.setItem\(\s*([A-Z_]+)", source))
        self.assertEqual({"OPERATION_RECEIPTS_STORAGE_KEY"}, keys)
        self.assertNotIn("localStorage", source)

    def test_the_browser_harness_names_the_archive_this_fixture_seeds(
        self,
    ) -> None:
        # The Playwright spec asserts against archive ids by name. They come
        # from one seeder, so a fixture that renamed an entry would otherwise
        # fail there as a mysterious missing row rather than here as a drift.
        manifest = json.loads(
            (REPO_ROOT / "tests" / "workbench_browser" / "scenarios.json").read_text(
                encoding="utf-8"
            )
        )
        archive = manifest["archive"]
        expired, retained = SHELL_ARCHIVE_ENTRIES
        self.assertEqual(expired["archive_id"], archive["expired"]["archive_id"])
        self.assertEqual(retained["archive_id"], archive["retained"]["archive_id"])
        self.assertEqual(len(SHELL_ARCHIVE_ENTRIES), archive["entry_count"])
        self.assertEqual(1, archive["expired_count"])

    def test_the_interface_has_somewhere_to_draw_an_open_operation(self) -> None:
        # A receipt with nothing to render into is a receipt nobody reads. Both
        # panels are asserted as markup rather than as behaviour because the
        # browser suite cannot run everywhere and an absent panel would make
        # every recovery assertion below vacuously pass there.
        markup = INDEX_HTML.read_text(encoding="utf-8")
        for element_id in (
            "wb-operations-panel",
            "wb-operations-list",
            "wb-archive-review",
            "wb-archive-review-list",
            "wb-archive-review-ack",
        ):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', markup)


class InteractionDeploymentTestCase(unittest.TestCase):
    """Both deployments, over one corpus, started once for the class."""

    deployments: ShellDeployments
    _tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="workbench-interactions-")
        cls.deployments = workbench_support.start_shell_deployments(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployments.shutdown()
        cls._tmp.cleanup()

    def row_for(self, deployment: ShellDeployment, name: str) -> dict[str, Any]:
        key = self.deployments.corpus.plan_keys[name]
        status, body = _request(deployment, "/api/queue")
        self.assertEqual(200, status, f"{deployment.mode} queue: {body}")
        for row in body.get("plans") or []:
            if isinstance(row, dict) and row.get("plan_key") == key:
                return row
        self.fail(f"{deployment.mode} queue has no row for {name}")

    def capabilities(self, deployment: ShellDeployment) -> dict[str, dict[str, Any]]:
        status, body = _request(deployment, "/api/capabilities")
        self.assertEqual(200, status, f"{deployment.mode} capabilities: {body}")
        return {
            entry["operation"]: entry
            for entry in body.get("operations") or []
            if isinstance(entry, dict)
        }


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

    def test_the_laptop_carries_out_everything_it_offers(self) -> None:
        # The local provider is the one that actually runs ASL, so an
        # unavailable operation there would be a capability the consolidated
        # application lost rather than one it never had.
        _, body = _request(self.deployments.local, "/api/capabilities")
        self.assertEqual([], body.get("unavailable_operations"))

    def test_the_pi_declines_exactly_the_operations_it_cannot_reach(self) -> None:
        _, body = _request(self.deployments.remote, "/api/capabilities")
        self.assertEqual(
            REMOTE_UNAVAILABLE, set(body.get("unavailable_operations") or [])
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
            "/api/archive/delete",
            method="POST",
            body={"archive_ids": "not-a-list", "confirm_count": "not-a-number"},
        )
        self.assertEqual(503, status, refusal)
        self.assertEqual("operation-unavailable", refusal.get("reason"))


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

    def test_the_pi_never_offers_the_archive_at_all(self) -> None:
        # Not "refuses the purge": the archive lives on the Mac, so even the
        # listing is unavailable, and the interface has to say that rather than
        # render an empty archive that reads as an archive with nothing in it.
        status, body = _request(self.deployments.remote, "/api/archive")
        self.assertEqual(503, status, body)
        self.assertEqual("operation-unavailable", body.get("reason"))
        self.assertTrue(str(body.get("error") or "").strip())


if __name__ == "__main__":
    unittest.main()
