"""The one browser shell, checked against both deployments at once.

Session 15's claim is that there is now a single front end -- one source, one
router, one API layer -- and that the laptop and the Pi serve it unchanged. A
test that started one provider could not fail that claim: every divergence
this consolidation is meant to remove lives in the gap between the two, and a
gap needs both sides to be visible at the same time.

So the fixture here (:func:`tests.workbench_support.start_shell_deployments`)
seeds one plan corpus and serves it twice -- through the real local server and
through the remote provider over a seeded observation store -- and every test
below asks the same question of both: *is this the same shell, over the same
plans?*

Three kinds of check:

* **Source.** One static root, no build step, no runtime dependency. Read off
  the filesystem, because "there is one front end" is a fact about the tree.
* **Parity.** Byte-identical assets, identical plan and run identities, and
  detail reads that carry the same material through two different wrappers.
* **Difference that is supposed to exist.** The Pi refuses a mutation it has
  not been asked for properly and records rather than runs the ones it takes.
  Those are not divergences to remove; they are the reason the remote mode is
  a separate provider, and a parity suite that did not pin them would read as
  though the two modes were interchangeable.

The Playwright harness in ``tests/workbench_browser`` drives the same two
servers through a real browser. This module is what runs everywhere, including
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
    SHELL_PLAN_NAMES,
    ShellDeployment,
    ShellDeployments,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = Path(workbench_http.STATIC_ROOT)
MODULES_ROOT = STATIC_ROOT / "modules"
BROWSER_HARNESS = REPO_ROOT / "tests" / "workbench_browser"

#: The detail reads the two providers answer differently enough to be worth
#: pinning: the local one hands back the artifact, the remote one hands back
#: the transport's page. ``key`` is where the material lives locally.
PARITY_DETAILS: tuple[tuple[str, str], ...] = (
    ("/api/state", "state"),
    ("/api/artifacts", "artifacts"),
    ("/api/progress", "entries"),
    ("/api/events", "events"),
)

#: A mutation both modes route. Chosen because it needs nothing but the plan's
#: address: acting on a *live* turn additionally needs the freshness proof ASL
#: issued, which is a separate boundary, pinned separately below.
MUTATION_ROUTE = "/api/unlock"

#: A mutation that acts on the turn in flight, and so may not be carried on a
#: view that might already be stale.
LIVE_MUTATION_ROUTE = "/api/control/stop-after-current"


def _request(
    deployment: ShellDeployment,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
) -> tuple[int, dict[str, Any], bytes]:
    """One request to one deployment, with the credentials that mode wants.

    Refusals come back rather than raise, because a refusal is an answer and
    several tests below are about exactly which one was given.
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
        # `HTTPError` is itself the response object, so it holds the socket
        # until it is closed. Left to the collector it surfaces as a
        # ResourceWarning attributed to whatever ran next.
        with error:
            raw = error.read()
            status = int(error.code)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}, raw


def _plan_query(deployment: ShellDeployment, row: dict[str, Any]) -> str:
    """Address a plan the way the loaded deployment addresses it.

    This mirrors ``planQuery`` in ``static/modules/identity.js``: a path where
    the mode has one, the digest otherwise. Duplicated here rather than
    imported because the point is that the two agree, and a shared constant
    would hide a disagreement instead of catching it.
    """
    if row.get("plan_dir"):
        return urlencode({"plan_dir": row["plan_dir"]})
    return urlencode({"plan_key": row["plan_key"]})


class ShellSourceTests(unittest.TestCase):
    """One front end, in one place, that a browser can run as it stands."""

    def test_the_shell_has_exactly_one_source_in_the_tree(self) -> None:
        # The consolidation's failure mode is two front ends that drift: a copy
        # under the local server package, or a second entry point beside the
        # first. The bundle both modes serve is the portable one, and there is
        # no other entry module anywhere in this repository.
        self.assertEqual(
            STATIC_ROOT,
            Path(workbench_http.shell_bundle().root),
            "both deployments must serve the bundle rooted at STATIC_ROOT",
        )
        self.assertEqual(
            "asl_workbench",
            STATIC_ROOT.parent.name,
            "the single front-end source belongs to the portable package",
        )
        entries = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in REPO_ROOT.glob("*/static/app.js")
        )
        self.assertEqual(["asl_workbench/static/app.js"], entries)

    def test_the_shell_is_divided_into_modules_rather_than_one_script(
        self,
    ) -> None:
        modules = sorted(path.name for path in MODULES_ROOT.glob("*.js"))
        self.assertGreater(
            len(modules),
            4,
            "the shell should be several cohesive modules, not one script",
        )
        # Nothing is allowed to have grown back into the old single file. The
        # bound is deliberately generous: this is a check against one module
        # quietly becoming the application again, not a style rule.
        for path in [STATIC_ROOT / "app.js", *MODULES_ROOT.glob("*.js")]:
            with self.subTest(module=path.name):
                lines = path.read_text(encoding="utf-8").count("\n")
                self.assertLess(
                    lines,
                    2000,
                    f"{path.name} has grown back towards one undivided script",
                )

    def test_the_shell_runs_with_no_build_step_and_no_runtime_dependency(
        self,
    ) -> None:
        # Native modules, loaded by the browser as served. Any bare specifier
        # would need an import map or a bundler, and either one would make the
        # Pi's deployment depend on a toolchain it does not have.
        index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertRegex(
            index,
            r'<script\s+type="module"\s+src="/static/app\.js"',
            "index.html must load the entry module as a native ES module",
        )
        specifier = re.compile(
            r'^\s*(?:import|export)\b[^;]*?\bfrom\s+"([^"]+)"', re.MULTILINE
        )
        for path in [STATIC_ROOT / "app.js", *sorted(MODULES_ROOT.glob("*.js"))]:
            source = path.read_text(encoding="utf-8")
            for target in specifier.findall(source):
                with self.subTest(module=path.name, imports=target):
                    self.assertTrue(
                        target.startswith("./") or target.startswith("../"),
                        "a bare specifier needs a bundler or an import map",
                    )
                    self.assertTrue(
                        target.endswith(".js"),
                        "an extensionless specifier is not resolvable by a browser",
                    )
        for path in sorted(STATIC_ROOT.rglob("*")):
            with self.subTest(path=path.name):
                self.assertNotIn(
                    path.name,
                    {"package.json", "node_modules", "package-lock.json"},
                    "the served front end must carry no package manifest",
                )

    def test_the_browser_harness_is_development_only_and_lives_in_scripts(
        self,
    ) -> None:
        # The harness is a test tool, so it lives with the tests and the
        # shipped package must not reach for it. `npm test` has to be the entry
        # point the completion contract runs, which is not what the Companion's
        # own harness named its script.
        manifest = json.loads(
            (BROWSER_HARNESS / "package.json").read_text(encoding="utf-8")
        )
        self.assertEqual("playwright test", manifest["scripts"]["test"])
        self.assertTrue(manifest.get("private"), "a test harness is not published")
        self.assertEqual(
            {},
            manifest.get("dependencies", {}),
            "the harness must add no runtime dependency, only a dev one",
        )
        self.assertIn("@playwright/test", manifest.get("devDependencies", {}))
        self.assertTrue((BROWSER_HARNESS / "shell.spec.js").is_file())
        self.assertTrue((BROWSER_HARNESS / "playwright.config.js").is_file())
        # The shipped packages may *say* the harness covers them -- that is
        # what a coverage reference in the capability matrix is for -- but they
        # may not reach for it. The distinction is the whole point: a named
        # path in a matrix row is documentation, while an import or a spawned
        # `npm` would make a Node toolchain part of what the Pi has to have.
        for package in ("asl_workbench", "agent_session_loop"):
            for path in (REPO_ROOT / package).rglob("*.py"):
                for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if "workbench_browser" not in line:
                        continue
                    with self.subTest(module=path.name, line=number):
                        self.assertIn(
                            '"scripts:tests/workbench_browser/',
                            line,
                            "the shipped packages may cite the harness as "
                            "coverage but must not depend on it",
                        )


class ShellDeploymentTestCase(unittest.TestCase):
    """Both deployments, over one corpus, started once for the class."""

    deployments: ShellDeployments
    _tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="workbench-shell-")
        cls.deployments = workbench_support.start_shell_deployments(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployments.shutdown()
        cls._tmp.cleanup()

    def queue(self, deployment: ShellDeployment) -> list[dict[str, Any]]:
        status, body, _ = _request(deployment, "/api/queue")
        self.assertEqual(200, status, f"{deployment.mode} queue: {body}")
        self.assertTrue(body.get("ok"), body)
        rows = body.get("plans")
        self.assertIsInstance(rows, list)
        return [row for row in rows if isinstance(row, dict)]

    def row_for(self, deployment: ShellDeployment, name: str) -> dict[str, Any]:
        key = self.deployments.corpus.plan_keys[name]
        for row in self.queue(deployment):
            if row.get("plan_key") == key:
                return row
        self.fail(f"{deployment.mode} queue has no row for {name}")


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
                self.assertEqual(path.read_bytes(), answers[0])

    def test_the_document_root_serves_the_same_shell_in_both_modes(self) -> None:
        answers = []
        for deployment in self.deployments.both:
            status, _, raw = _request(deployment, "/")
            self.assertEqual(200, status, deployment.mode)
            answers.append(raw)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual((STATIC_ROOT / "index.html").read_bytes(), answers[0])


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


if __name__ == "__main__":  # pragma: no cover - entry point
    unittest.main()
