"""Parity tests extracted from test_workbench_shell."""

from __future__ import annotations

from urllib.parse import urlencode

from asl_workbench.static_sources import read_static_source
from tests.workbench_support import (
    SHELL_PLAN_NAMES,
    ShellDeployment,
)

from .workbench_shell_support import (
    LIVE_MUTATION_ROUTE,
    MODULES_ROOT,
    MUTATION_ROUTE,
    PARITY_DETAILS,
    STATIC_ROOT,
    ShellDeploymentTestCase,
    _plan_query,
    _request,
)


class ServedAssetParityTests(ShellDeploymentTestCase):
    """ "Local and Pi modes serve the same assets" -- byte for byte."""

    def test_every_static_asset_is_identical_in_both_modes(self) -> None:
        served = sorted(
            path
            for path in STATIC_ROOT.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )
        self.assertTrue(served, "the static root is empty")
        for path in served:
            route = "/static/" + path.relative_to(STATIC_ROOT).as_posix()
            with self.subTest(asset=route):
                answers = []
                for deployment in self.deployments.both:
                    status, _, raw = _request(deployment, route)
                    self.assertEqual(200, status, f"{deployment.mode} {route}")
                    answers.append(raw)
                self.assertEqual(
                    answers[0],
                    answers[1],
                    f"{route} differs between the two deployments",
                )
                self.assertEqual(read_static_source(path), answers[0])

    def test_the_document_root_serves_the_same_shell_in_both_modes(self) -> None:
        answers = []
        for deployment in self.deployments.both:
            status, _, raw = _request(deployment, "/")
            self.assertEqual(200, status, deployment.mode)
            answers.append(raw)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(read_static_source(STATIC_ROOT / "index.html"), answers[0])


class PlanIdentityParityTests(ShellDeploymentTestCase):
    """One plan, one identity, whichever deployment is answering."""

    def test_both_modes_publish_the_same_plan_and_run_keys(self) -> None:
        corpus = self.deployments.corpus
        for deployment in self.deployments.both:
            with self.subTest(mode=deployment.mode):
                published = {
                    row["plan_key"]: row.get("run_key")
                    for row in self.queue(deployment)
                }
                self.assertEqual(
                    {
                        corpus.plan_keys[name]: corpus.run_keys[name]
                        for name in SHELL_PLAN_NAMES
                    },
                    published,
                )

    def test_a_run_link_resolves_to_the_same_plan_in_both_modes(self) -> None:
        # This is the whole reason the local mode computes catalog keys it does
        # not need: an ntfy notification carries '#run=<run_key>', and the link
        # has to open the same plan on the phone and on the laptop. The router
        # resolves it against the queue, so resolving it here the same way is
        # what makes the two answers comparable.
        for name in SHELL_PLAN_NAMES:
            run_key = self.deployments.corpus.run_keys[name]
            with self.subTest(plan=name):
                resolved = []
                for deployment in self.deployments.both:
                    matches = [
                        row
                        for row in self.queue(deployment)
                        if row.get("run_key") == run_key
                    ]
                    self.assertEqual(
                        1, len(matches), f"{deployment.mode} did not resolve {run_key}"
                    )
                    resolved.append(matches[0]["plan_key"])
                self.assertEqual(resolved[0], resolved[1])
                self.assertEqual(self.deployments.corpus.plan_keys[name], resolved[0])

    def test_both_modes_describe_a_plan_with_the_same_words(self) -> None:
        # One plan is paused for a reply and the other has never run. Both
        # modes have to reach that from what they hold -- the Mac from the
        # runner state on disk, the Pi from what the Mac published -- or the
        # queue an operator triages on the phone is not the one on the laptop.
        for name in SHELL_PLAN_NAMES:
            with self.subTest(plan=name):
                described = [
                    (
                        self.row_for(deployment, name)["primary_state"],
                        self.row_for(deployment, name)["recommended_action"],
                        self.row_for(deployment, name)["urgency_group"],
                    )
                    for deployment in self.deployments.both
                ]
                self.assertEqual(described[0], described[1])
        alpha = self.row_for(self.deployments.local, "alpha")
        self.assertEqual("paused-for-reply", alpha["primary_state"])
        self.assertEqual(
            "ready", self.row_for(self.deployments.local, "bravo")["primary_state"]
        )

    def test_the_plan_detail_names_the_runs_observed_against_that_plan(
        self,
    ) -> None:
        # The remote detail pane's run list is narrowed by the store, because
        # the plan key is storage and not an exported field: a filter applied
        # after the export would match nothing and the pane would say the plan
        # has no runs.
        remote = self.deployments.remote
        for name in SHELL_PLAN_NAMES:
            with self.subTest(plan=name):
                key = self.deployments.corpus.plan_keys[name]
                status, body, _ = _request(remote, f"/api/plan?plan_key={key}")
                self.assertEqual(200, status, body)
                self.assertEqual(
                    [self.deployments.corpus.run_keys[name]],
                    [run["run_key"] for run in body["runs"]],
                )

    def test_each_mode_addresses_a_plan_the_way_it_can(self) -> None:
        # The Mac has a path and publishes it; the Pi has never seen the Mac's
        # filesystem, so a path it published would be a path it guessed. The
        # front end reads whichever is there, which only works if exactly one
        # of them is.
        local_row = self.row_for(self.deployments.local, "alpha")
        remote_row = self.row_for(self.deployments.remote, "alpha")
        self.assertEqual(
            str(self.deployments.corpus.plan_dir("alpha")), local_row["plan_dir"]
        )
        self.assertIsNone(remote_row.get("plan_dir"))


class DetailParityTests(ShellDeploymentTestCase):
    """The same material, through two wrappers the front end has to absorb."""

    def test_detail_reads_answer_in_both_modes_for_the_same_plan(self) -> None:
        for path, _ in PARITY_DETAILS:
            with self.subTest(detail=path):
                for deployment in self.deployments.both:
                    row = self.row_for(deployment, "alpha")
                    query = _plan_query(deployment, row)
                    status, body, _ = _request(deployment, f"{path}?{query}")
                    self.assertEqual(200, status, f"{deployment.mode} {path}: {body}")
                    self.assertTrue(body.get("ok"), body)

    def test_the_remote_wrapper_carries_the_local_material(self) -> None:
        # The divergence `unwrapDetail` in static/modules/api.js exists for:
        # locally a detail read answers with the artifact under its own key,
        # remotely with the transport's page. Both are the truth about the same
        # read, and this pins that they stay the same truth.
        for path, key in PARITY_DETAILS:
            with self.subTest(detail=path):
                local_row = self.row_for(self.deployments.local, "alpha")
                remote_row = self.row_for(self.deployments.remote, "alpha")
                _, local_body, _ = _request(
                    self.deployments.local,
                    f"{path}?{_plan_query(self.deployments.local, local_row)}",
                )
                _, remote_body, _ = _request(
                    self.deployments.remote,
                    f"{path}?{_plan_query(self.deployments.remote, remote_row)}",
                )
                self.assertIn(key, local_body)
                self.assertIn(
                    "detail",
                    remote_body,
                    "a remote detail answer names the kind it carries",
                )
                self.assertIn("entries", remote_body)
                local_material = local_body[key]
                entries = remote_body["entries"]
                if isinstance(local_material, list):
                    self.assertEqual(len(local_material), len(entries))
                else:
                    self.assertEqual(
                        1, len(entries), "an object detail is one page row"
                    )

    def test_a_detail_read_for_an_unknown_plan_is_refused_not_invented(
        self,
    ) -> None:
        for deployment in self.deployments.both:
            with self.subTest(mode=deployment.mode):
                query = urlencode(
                    {"plan_key": "0" * 16}
                    if deployment.mode == "remote"
                    else {"plan_dir": "/nonexistent/plan"}
                )
                status, body, _ = _request(deployment, f"/api/state?{query}")
                self.assertNotEqual(200, status, body)
                self.assertFalse(body.get("ok", False), body)
                self.assertTrue(body.get("reason"), "a refusal must say why")


class DeploymentDifferenceTests(ShellDeploymentTestCase):
    """The differences that are supposed to survive the consolidation."""

    def _without(self, deployment: ShellDeployment, header: str) -> ShellDeployment:
        return ShellDeployment(
            mode=deployment.mode,
            server=deployment.server,
            thread=deployment.thread,
            headers={
                name: value
                for name, value in deployment.headers.items()
                if name.lower() != header.lower()
            },
        )

    def test_the_local_mode_leaves_reads_open_and_holds_the_token_on_writes(
        self,
    ) -> None:
        # An asymmetry the shell depends on: the deep link out of `asl
        # workbench` carries the token for the writes, and the page it opens
        # renders before anything is written. Requiring a token on reads would
        # break every one of those links.
        local = self.deployments.local
        bare = self._without(local, "Authorization")
        status, body, _ = _request(bare, "/api/queue")
        self.assertEqual(200, status, body)
        row = self.row_for(local, "alpha")
        status, body, _ = _request(
            bare,
            MUTATION_ROUTE,
            method="POST",
            body={"plan_dir": row["plan_dir"]},
        )
        self.assertEqual(401, status, body)

    def test_the_remote_mode_refuses_a_mutation_without_the_request_header(
        self,
    ) -> None:
        # The header is the anti-CSRF check: no cross-origin form can set it.
        # The shared API layer sends it on every mutation in both modes, which
        # is only correct if the remote mode is genuinely refusing without it.
        remote = self.deployments.remote
        row = self.row_for(remote, "alpha")
        status, body, _ = _request(
            self._without(remote, "X-Workbench-Request"),
            MUTATION_ROUTE,
            method="POST",
            body={"plan_key": row["plan_key"]},
        )
        self.assertEqual(403, status, body)
        self.assertFalse(body.get("ok", True), body)

    def test_the_remote_mode_needs_an_identity_the_client_minted(self) -> None:
        # The id has to come from the browser, because it is what makes a
        # resend the same request rather than a second one, and a
        # server-assigned id would arrive after the only moment it could help.
        # The shared API layer therefore attaches one to every mutation; this
        # is the requirement that makes that necessary rather than tidy.
        remote = self.deployments.remote
        row = self.row_for(remote, "alpha")
        status, body, _ = _request(
            remote, MUTATION_ROUTE, method="POST", body={"plan_key": row["plan_key"]}
        )
        self.assertEqual(400, status, body)
        self.assertEqual("invalid-request", body.get("reason"), body)
        source = (MODULES_ROOT / "api.js").read_text(encoding="utf-8")
        self.assertIn("export function mintOperationId()", source)
        self.assertIn("operation_id: operationId", source)

    def test_the_remote_mode_records_a_mutation_rather_than_running_it(
        self,
    ) -> None:
        # 202 is the one answer the front end must never render as success:
        # the Pi wrote the request down and the Mac has not run it.
        remote = self.deployments.remote
        row = self.row_for(remote, "alpha")
        status, body, _ = _request(
            remote,
            MUTATION_ROUTE,
            method="POST",
            body={
                "plan_key": row["plan_key"],
                "run_key": row["run_key"],
                "operation_id": "shell-fixture-op-1",
            },
        )
        self.assertEqual(202, status, body)
        self.assertTrue(body.get("ok"), body)
        operation = body.get("operation")
        self.assertIsInstance(operation, dict)
        assert isinstance(operation, dict)
        self.assertEqual("shell-fixture-op-1", operation.get("operation_id"))
        self.assertEqual(
            "plan.unlock",
            operation.get("method"),
            "the recorded operation must name what was asked for",
        )
        self.assertEqual("pending", operation.get("status"), operation)

    def test_the_remote_mode_refuses_to_act_on_a_live_turn_without_the_proof(
        self,
    ) -> None:
        # Acting on the turn in flight from a view that might already be stale
        # is acting on a guess. ASL issues a freshness token with the state the
        # caller read, and a submission without it is refused -- which is why
        # the shell offers these actions only where it holds one.
        remote = self.deployments.remote
        row = self.row_for(remote, "alpha")
        status, body, _ = _request(
            remote,
            LIVE_MUTATION_ROUTE,
            method="POST",
            body={
                "plan_key": row["plan_key"],
                "run_key": row["run_key"],
                "operation_id": "shell-fixture-op-2",
            },
        )
        self.assertEqual(400, status, body)
        self.assertEqual("invalid-submission", body.get("reason"), body)
        self.assertEqual("unauthorized", body.get("details", {}).get("detail_code"))
