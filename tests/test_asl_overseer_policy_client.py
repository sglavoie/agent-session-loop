"""Focused tests for the ASL side of AgentMux's authorization contract.

The client never runs ``amx`` here. Every test drives it through the injected
``resolve_binary``/``run_command`` seams, which is the only way to exercise the
cases that matter most: the ones where the command is missing, hangs, crashes,
or answers something other than what was asked.

Two properties are proved, and neither is about the happy path.

*Unavailable is never permitted.* A parametric sweep drives every transport and
protocol failure the module names and asserts the same four things of each: not
allowed, not available, no grant, no identities. The sweep is driven from
:data:`overseer_policy_client.FAILURE_REASONS` itself and asserts it covered
every entry, so a failure mode added later without a test fails this file rather
than passing silently.

*An answer is evidence for one question.* A well-formed, internally consistent,
``allowed`` payload is rewritten one field at a time -- the plan, the access
category, a repository path, the repository count, the exit status -- and each
mutation must turn a permitted decision into a refused one.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any

from agent_session_loop import overseer_discovery, overseer_policy_client, plans_files
from tests.support import assert_no_runner_import

PLAN = "/state/agent-session-loop/plan-abc"
AMX = "/usr/local/bin/amx"
FINGERPRINT = "fp-0123456789abcdef"
DIGEST = "rd-fedcba9876543210"

REPOSITORIES = (
    overseer_policy_client.Repository(alias="scripts", path="/home/dev/scripts"),
    overseer_policy_client.Repository(alias="api", path="/home/dev/api"),
)


def _outcome(repository: overseer_policy_client.Repository, *, allowed: bool) -> Any:
    return {
        "alias": repository.alias,
        "path": repository.path,
        "resolved_path": repository.path,
        "allowed": allowed,
        "reason": "opted_in" if allowed else "excluded",
        "detail": "",
    }


def _payload(
    *,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    allowed: bool = True,
    repositories: tuple[overseer_policy_client.Repository, ...] = REPOSITORIES,
) -> dict[str, Any]:
    """A well-formed answer to the question the tests ask by default."""
    return {
        "schema_version": overseer_policy_client.POLICY_SCHEMA_VERSION,
        "data": {
            "contract": {
                "name": overseer_policy_client.POLICY_CONTRACT_NAME,
                "version": overseer_policy_client.POLICY_CONTRACT_VERSION,
                "access_categories": list(overseer_policy_client.ACCESS_CATEGORIES),
            },
            "plan": plan_dir,
            "access": access,
            "allowed": allowed,
            "reason": "opted_in" if allowed else "excluded",
            "summary": "2 repositories",
            "config_fingerprint": FINGERPRINT,
            "request_digest": DIGEST,
            "repositories": [
                _outcome(repository, allowed=allowed) for repository in repositories
            ],
        },
    }


class _CannedAmx:
    """A ``run_command`` seam that replays one canned answer and records the argv."""

    def __init__(
        self,
        *,
        stdout: str = "",
        returncode: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout=self.stdout, stderr=""
        )


def _authorize(
    *,
    payload: Any = None,
    stdout: str | None = None,
    returncode: int = 0,
    raises: BaseException | None = None,
    amx_path: str | None = AMX,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    repositories: tuple[overseer_policy_client.Repository, ...] = REPOSITORIES,
) -> tuple[overseer_policy_client.Decision, _CannedAmx]:
    text = stdout if stdout is not None else json.dumps(payload)
    invoked = _CannedAmx(stdout=text, returncode=returncode, raises=raises)
    decision = overseer_policy_client.authorize(
        plan_dir=plan_dir,
        access=access,
        repositories=repositories,
        resolve_binary=lambda _name: amx_path,
        run_command=invoked,
    )
    return decision, invoked


class PolicyRequestTests(unittest.TestCase):
    """What goes on the wire, and what is refused before anything does."""

    def test_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_policy_client)

    def test_argv_pins_the_contract_version_and_preserves_repository_order(
        self,
    ) -> None:
        argv = overseer_policy_client.policy_argv(
            amx_path=AMX,
            plan_dir=PLAN,
            access=overseer_policy_client.ACCESS_ACTION,
            repositories=REPOSITORIES,
        )
        self.assertEqual(argv[:4], [AMX, "overseer", "policy", "--json"])
        self.assertIn("--contract-version", argv)
        self.assertEqual(
            argv[argv.index("--contract-version") + 1],
            str(overseer_policy_client.POLICY_CONTRACT_VERSION),
        )
        self.assertEqual(argv[argv.index("--plan") + 1], PLAN)
        self.assertEqual(
            argv[argv.index("--access") + 1], overseer_policy_client.ACCESS_ACTION
        )
        specs = [
            argv[index + 1] for index, item in enumerate(argv) if item == "--repository"
        ]
        self.assertEqual(specs, ["scripts=/home/dev/scripts", "api=/home/dev/api"])

    def test_repositories_for_sends_resolved_roots_in_declaration_order(self) -> None:
        declarations = (
            overseer_discovery.RepositoryDeclaration(
                alias="scripts",
                path="~/scripts",
                origin="manifest",
                resolved_root="/home/dev/scripts",
            ),
            overseer_discovery.RepositoryDeclaration(
                alias="api",
                path="../api",
                origin="manifest",
                resolved_root="/home/dev/api",
            ),
        )
        self.assertEqual(
            overseer_policy_client.repositories_for(declarations), REPOSITORIES
        )

    def test_a_malformed_request_is_never_sent(self) -> None:
        """Each of these is refused here; the command must not be started."""
        cases = {
            "no plan": {"plan_dir": ""},
            "unknown access": {"access": "read"},
            "no repositories": {"repositories": ()},
            "aliasless repository": {
                "repositories": (
                    overseer_policy_client.Repository(alias="", path="/home/dev/api"),
                )
            },
            "relative path": {
                "repositories": (
                    overseer_policy_client.Repository(alias="api", path="api"),
                )
            },
            "duplicate alias": {
                "repositories": (
                    overseer_policy_client.Repository(alias="api", path="/home/dev/a"),
                    overseer_policy_client.Repository(alias="api", path="/home/dev/b"),
                )
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                decision, invoked = _authorize(payload=_payload(), **overrides)
                self.assertEqual(decision.failure, "request_unsendable")
                self.assertFalse(decision.allowed)
                self.assertFalse(decision.available)
                self.assertEqual(invoked.calls, [])

    def test_case_differing_access_is_not_folded(self) -> None:
        """The category is bound into the request digest, so ``Context`` is not
        ``context`` -- folding it here would ask a question nobody answered."""
        decision, invoked = _authorize(payload=_payload(), access="Context")
        self.assertEqual(decision.failure, "request_unsendable")
        self.assertEqual(invoked.calls, [])


class PolicyAnswerTests(unittest.TestCase):
    """A permitted answer, and the two adapters it produces."""

    def test_an_allowed_answer_grants_for_exactly_what_was_asked(self) -> None:
        decision, invoked = _authorize(payload=_payload())
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.available)
        self.assertIsNone(decision.failure)
        self.assertEqual(decision.config_fingerprint, FINGERPRINT)
        self.assertEqual(decision.request_digest, DIGEST)
        self.assertEqual(len(invoked.calls), 1)

        grant = decision.grant()
        self.assertIsNotNone(grant)
        assert grant is not None
        self.assertEqual(grant.plan_dir, PLAN)
        self.assertEqual(grant.access, overseer_policy_client.ACCESS_CONTEXT)
        self.assertEqual(grant.request_digest, DIGEST)
        self.assertEqual(grant.config_fingerprint, FINGERPRINT)

    def test_the_isolation_adapter_permits_only_the_mutation_it_answered(self) -> None:
        """``Authorization.permits`` is the mutation question specifically, so a
        ``context`` grant converts into an authorization that permits nothing --
        which is the point of naming the category in the request at all."""
        canonical = plans_files.canonicalize_path(PLAN)

        mutation, _ = _authorize(
            payload=_payload(access=overseer_policy_client.ACCESS_MUTATION),
            access=overseer_policy_client.ACCESS_MUTATION,
        )
        authorization = mutation.authorization()
        self.assertIsNotNone(authorization)
        assert authorization is not None
        self.assertTrue(authorization.permits(canonical))
        self.assertFalse(
            authorization.permits(plans_files.canonicalize_path(f"{PLAN}-other"))
        )

        context, _ = _authorize(payload=_payload())
        context_authorization = context.authorization()
        assert context_authorization is not None
        self.assertFalse(context_authorization.permits(canonical))

    def test_a_refusal_is_a_decision_not_a_failure(self) -> None:
        """AgentMux exits nonzero on a refusal in JSON mode too, and that is
        still an answer: available, not allowed, and naming its causes."""
        decision, _ = _authorize(payload=_payload(allowed=False), returncode=1)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.available)
        self.assertIsNone(decision.failure)
        self.assertEqual(decision.exit_status, 1)
        self.assertEqual(
            [outcome.alias for outcome in decision.refused_repositories()],
            ["scripts", "api"],
        )
        self.assertIsNone(decision.grant())
        self.assertIsNone(decision.authorization())

    def test_every_access_category_round_trips(self) -> None:
        for access in overseer_policy_client.ACCESS_CATEGORIES:
            with self.subTest(access):
                decision, _ = _authorize(payload=_payload(access=access), access=access)
                self.assertTrue(decision.allowed)
                grant = decision.grant()
                assert grant is not None
                self.assertEqual(grant.access, access)


class PolicyUnavailableTests(unittest.TestCase):
    """No way of failing to get an answer may produce permission."""

    def _sweep(self) -> dict[str, overseer_policy_client.Decision]:
        allowed = _payload()
        wrong_envelope = _payload()
        wrong_envelope["schema_version"] = 2
        wrong_contract = _payload()
        wrong_contract["data"]["contract"]["version"] = 2
        missing_digest = _payload()
        del missing_digest["data"]["request_digest"]
        wrong_plan = _payload(plan_dir="/state/agent-session-loop/plan-other")
        rejected = {
            "schema_version": 1,
            "error": {
                "code": "invalid_access",
                "message": "unknown access category",
                "candidates": [],
                "parameters": {},
            },
        }
        return {
            "request_unsendable": _authorize(payload=allowed, plan_dir="")[0],
            "client_missing": _authorize(payload=allowed, amx_path=None)[0],
            "client_timeout": _authorize(
                payload=allowed,
                raises=subprocess.TimeoutExpired(cmd=[AMX], timeout=10.0),
            )[0],
            "client_unusable": _authorize(
                payload=allowed, raises=OSError("no such file")
            )[0],
            "response_unreadable": _authorize(stdout="not json at all")[0],
            "response_malformed": _authorize(payload=missing_digest)[0],
            "unsupported_envelope": _authorize(payload=wrong_envelope)[0],
            "unsupported_contract": _authorize(payload=wrong_contract)[0],
            "request_rejected": _authorize(payload=rejected, returncode=2)[0],
            "answer_mismatch": _authorize(payload=wrong_plan)[0],
        }

    def test_every_named_failure_refuses_and_carries_no_identity(self) -> None:
        sweep = self._sweep()
        self.assertEqual(sorted(sweep), sorted(overseer_policy_client.FAILURE_REASONS))
        for failure, decision in sweep.items():
            with self.subTest(failure):
                self.assertEqual(decision.failure, failure)
                self.assertFalse(decision.allowed)
                self.assertFalse(decision.available)
                self.assertIsNone(decision.grant())
                self.assertIsNone(decision.authorization())
                self.assertEqual(decision.config_fingerprint, "")
                self.assertEqual(decision.request_digest, "")
                self.assertEqual(decision.outcomes, ())

    def test_an_empty_answer_is_unreadable_rather_than_permissive(self) -> None:
        decision, _ = _authorize(stdout="", returncode=0)
        self.assertEqual(decision.failure, "response_unreadable")
        self.assertIsNone(decision.grant())

    def test_a_crashed_client_that_printed_an_allowance_is_still_refused(self) -> None:
        """A payload saying yes alongside a nonzero exit contradicts the
        command's own contract, so the yes is not read."""
        decision, _ = _authorize(payload=_payload(), returncode=3)
        self.assertEqual(decision.failure, "answer_mismatch")
        self.assertIsNone(decision.grant())


class PolicyMismatchTests(unittest.TestCase):
    """One good answer, mutated one field at a time."""

    def test_an_answer_to_another_question_is_refused(self) -> None:
        other = overseer_policy_client.Repository(
            alias="scripts", path="/home/dev/elsewhere"
        )
        wrong_path = _payload()
        wrong_path["data"]["repositories"][0] = _outcome(other, allowed=True)
        short_set = _payload()
        short_set["data"]["repositories"] = short_set["data"]["repositories"][:1]
        extra_set = _payload()
        extra_set["data"]["repositories"].append(_outcome(other, allowed=True))
        reordered = _payload()
        reordered["data"]["repositories"].reverse()
        wrong_access = _payload(access=overseer_policy_client.ACCESS_ACTION)
        inconsistent = _payload()
        inconsistent["data"]["repositories"][1]["allowed"] = False

        cases = {
            "another plan": _payload(plan_dir="/state/agent-session-loop/other"),
            "another access category": wrong_access,
            "another repository path": wrong_path,
            "a shorter repository set": short_set,
            "a longer repository set": extra_set,
            "a reordered repository set": reordered,
            "an allowance over a refused repository": inconsistent,
        }
        for label, payload in cases.items():
            with self.subTest(label):
                decision, _ = _authorize(payload=payload)
                self.assertEqual(decision.failure, "answer_mismatch")
                self.assertFalse(decision.allowed)
                self.assertFalse(decision.available)
                self.assertIsNone(decision.grant())

    def test_one_unreadable_outcome_rejects_the_whole_set(self) -> None:
        """Dropping it would shrink the answered set into one that no longer
        matches what was asked -- silently, which is the failure mode."""
        payload = _payload()
        payload["data"]["repositories"][1]["allowed"] = "yes"
        decision, _ = _authorize(payload=payload)
        self.assertEqual(decision.failure, "response_malformed")
        self.assertEqual(decision.outcomes, ())
        self.assertIsNone(decision.grant())


if __name__ == "__main__":
    unittest.main()
