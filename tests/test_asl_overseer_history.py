"""Focused tests for the ASL side of AgentMux's transcript-evidence contract.

``amx`` is never started here. Every test drives the client through its injected
``resolve_binary``/``run_command`` seams, which is the only way to reach the
cases that matter: the command missing, hanging, crashing, or answering
something other than what was asked.

Three properties are proved, and none of them is the happy path.

*Unavailable is never permitted.* A parametric sweep drives every transport and
protocol failure the module names and asserts the same five things of each: not
allowed, not available, no grant, no identities, and no conversations. The sweep
is driven from :data:`overseer_history.FAILURE_REASONS` itself and asserts it
covered every entry, so a failure mode added later without a test fails this
file rather than passing silently.

*An answer is evidence for one question, taken under stated bounds.* A
well-formed, internally consistent, ``allowed`` payload is rewritten one field
at a time -- the plan, the access category, a repository, the exit status, and
each of the three bounds -- and every mutation must turn a permitted answer into
a refused one. The bound cases are the ones with teeth: an answer carrying more
conversations, more messages, or longer excerpts than were asked for is refused
even though every other field agrees, because a bound that was reported is not a
bound that was applied.

*A refusal does not describe what it withheld.* A withheld entry that names the
path, directory, or project of the conversation it hid is rejected outright
rather than parsed past, and the answer carrying it yields nothing.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any

from agent_session_loop import overseer_history, overseer_policy_client
from tests.support import assert_no_runner_import

PLAN = "/state/agent-session-loop/plan-abc"
AMX = "/usr/local/bin/amx"
FINGERPRINT = "fp-0123456789abcdef"
DIGEST = "rd-fedcba9876543210"

REPOSITORIES = (
    overseer_policy_client.Repository(alias="scripts", path="/home/dev/scripts"),
    overseer_policy_client.Repository(alias="api", path="/home/dev/api"),
)

DEFAULT_LIMITS = overseer_history.Limits()


def _outcome(
    repository: overseer_policy_client.Repository, *, allowed: bool
) -> dict[str, Any]:
    return {
        "alias": repository.alias,
        "path": repository.path,
        "resolved_path": repository.path,
        "allowed": allowed,
        "reason": "opted_in" if allowed else "excluded",
        "detail": "",
    }


def _excerpt(index: int, *, role: str = "user", text: str = "hello") -> dict[str, Any]:
    return {
        "index": index,
        "role": role,
        "timestamp": "2026-01-02T03:04:05Z",
        "text": text,
        "truncated": False,
    }


def _conversation(
    *,
    session_id: str = "sess-1",
    directory: str = "/home/dev/scripts",
    excerpts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "harness": "claude",
        "id": session_id,
        "profiles": ["default"],
        "directory": directory,
        "projects": ["scripts"],
        "path": f"/home/dev/.claude/projects/scripts/{session_id}.jsonl",
        "started_at": "2026-01-02T03:00:00Z",
        "last_at": "2026-01-02T03:30:00Z",
        "session_class": "main",
        "message_count": 42,
        "truncated": True,
        "excerpts": excerpts if excerpts is not None else [_excerpt(40), _excerpt(41)],
    }


def _payload(
    *,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    allowed: bool = True,
    repositories: tuple[overseer_policy_client.Repository, ...] = REPOSITORIES,
    limits: overseer_history.Limits = DEFAULT_LIMITS,
    conversations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A well-formed answer to the question the tests ask by default."""
    return {
        "schema_version": overseer_history.HISTORY_SCHEMA_VERSION,
        "data": {
            "contract": {
                "name": overseer_history.HISTORY_CONTRACT_NAME,
                "version": overseer_history.HISTORY_CONTRACT_VERSION,
                "access_categories": list(overseer_policy_client.ACCESS_CATEGORIES),
            },
            "plan": plan_dir,
            "access": access,
            "allowed": allowed,
            "reason": "allowed" if allowed else "unenrolled",
            "summary": "authorized: 1 of 3 conversations returned, 2 withheld",
            "config_fingerprint": FINGERPRINT,
            "request_digest": DIGEST,
            "repositories": [
                _outcome(repository, allowed=allowed) for repository in repositories
            ],
            "limits": {
                "conversations": limits.conversations,
                "messages": limits.messages,
                "excerpt_chars": limits.excerpt_chars,
            },
            "harnesses": [],
            "examined": 3,
            "truncated": False,
            "conversations": (
                conversations
                if conversations is not None
                else ([_conversation()] if allowed else [])
            ),
            "withheld": (
                [
                    {"harness": "codex", "id": "sess-2", "reason": "out_of_scope"},
                    {
                        "harness": "claude",
                        "id": "sess-3",
                        "reason": "ambiguous_project",
                    },
                ]
                if allowed
                else []
            ),
            "warnings": [],
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


def _read(
    *,
    payload: Any = None,
    stdout: str | None = None,
    returncode: int = 0,
    raises: BaseException | None = None,
    amx_path: str | None = AMX,
    plan_dir: str = PLAN,
    repositories: tuple[overseer_policy_client.Repository, ...] = REPOSITORIES,
    limits: overseer_history.Limits | None = None,
    harnesses: tuple[str, ...] = (),
    since: str = "",
) -> tuple[overseer_history.History, _CannedAmx]:
    text = stdout if stdout is not None else json.dumps(payload)
    invoked = _CannedAmx(stdout=text, returncode=returncode, raises=raises)
    history = overseer_history.read_history(
        plan_dir=plan_dir,
        repositories=repositories,
        limits=limits,
        harnesses=harnesses,
        since=since,
        resolve_binary=lambda _name: amx_path,
        run_command=invoked,
    )
    return history, invoked


class HistoryRequestTests(unittest.TestCase):
    """What goes on the wire, and what is refused before anything does."""

    def test_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_history)

    def test_argv_pins_the_contract_version_the_bounds_and_the_narrowing(self) -> None:
        argv = overseer_history.history_argv(
            amx_path=AMX,
            plan_dir=PLAN,
            repositories=REPOSITORIES,
            limits=overseer_history.Limits(
                conversations=3, messages=5, excerpt_chars=400
            ),
            harnesses=("claude", "codex"),
            since="2026-01-02T03:04:05Z",
        )
        self.assertEqual(argv[:4], [AMX, "overseer", "history", "--json"])
        self.assertEqual(
            argv[argv.index("--contract-version") + 1],
            str(overseer_history.HISTORY_CONTRACT_VERSION),
        )
        self.assertEqual(argv[argv.index("--plan") + 1], PLAN)
        self.assertEqual(argv[argv.index("--limit") + 1], "3")
        self.assertEqual(argv[argv.index("--messages") + 1], "5")
        self.assertEqual(argv[argv.index("--max-chars") + 1], "400")
        self.assertEqual(argv[argv.index("--since") + 1], "2026-01-02T03:04:05Z")
        specs = [
            argv[index + 1] for index, item in enumerate(argv) if item == "--repository"
        ]
        self.assertEqual(specs, ["scripts=/home/dev/scripts", "api=/home/dev/api"])
        harnesses = [
            argv[index + 1] for index, item in enumerate(argv) if item == "--harness"
        ]
        self.assertEqual(harnesses, ["claude", "codex"])

    def test_the_request_never_names_an_access_category(self) -> None:
        """Reading a conversation is ``context`` access by construction. A flag
        would be a way to ask for evidence under some other permission."""
        argv = overseer_history.history_argv(
            amx_path=AMX,
            plan_dir=PLAN,
            repositories=REPOSITORIES,
            limits=DEFAULT_LIMITS,
        )
        self.assertNotIn("--access", argv)

    def test_the_bounds_are_always_sent_even_when_they_are_the_defaults(self) -> None:
        """The command's own default is whatever the installed ``amx`` believes,
        and the bounds are bound into the request digest; sending them makes the
        digest computable from what this side asked for."""
        _, invoked = _read(payload=_payload())
        argv = invoked.calls[0]
        self.assertEqual(
            argv[argv.index("--limit") + 1],
            str(overseer_history.DEFAULT_CONVERSATIONS),
        )
        self.assertEqual(
            argv[argv.index("--messages") + 1], str(overseer_history.DEFAULT_MESSAGES)
        )
        self.assertEqual(
            argv[argv.index("--max-chars") + 1],
            str(overseer_history.DEFAULT_EXCERPT_CHARS),
        )

    def test_a_malformed_request_is_never_sent(self) -> None:
        """Each of these is refused here; the command must not be started."""
        cases: dict[str, dict[str, Any]] = {
            "no plan": {"plan_dir": ""},
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
            "unparseable since": {"since": "yesterday"},
            "zero conversation bound": {
                "limits": overseer_history.Limits(conversations=0)
            },
            "conversations above the ceiling": {
                "limits": overseer_history.Limits(
                    conversations=overseer_history.MAX_CONVERSATIONS + 1
                )
            },
            "messages above the ceiling": {
                "limits": overseer_history.Limits(
                    messages=overseer_history.MAX_MESSAGES + 1
                )
            },
            "excerpt chars above the ceiling": {
                "limits": overseer_history.Limits(
                    excerpt_chars=overseer_history.MAX_EXCERPT_CHARS + 1
                )
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                history, invoked = _read(payload=_payload(), **overrides)
                self.assertEqual(history.failure, "request_unsendable")
                self.assertFalse(history.allowed)
                self.assertFalse(history.available)
                self.assertEqual(history.conversations, ())
                self.assertEqual(invoked.calls, [])

    def test_an_oversized_bound_is_refused_rather_than_quietly_clamped(self) -> None:
        """AgentMux would clamp it and answer. Asking anyway would mean a caller
        that wanted 500 conversations reads 100 as if it were all of them."""
        history, invoked = _read(
            payload=_payload(),
            limits=overseer_history.Limits(conversations=500),
        )
        self.assertEqual(history.failure, "request_unsendable")
        self.assertIn("ceiling", history.summary)
        self.assertEqual(invoked.calls, [])


class HistoryAnswerTests(unittest.TestCase):
    """A permitted answer and what it carries."""

    def test_an_allowed_answer_returns_located_bounded_evidence(self) -> None:
        history, invoked = _read(payload=_payload())
        self.assertTrue(history.allowed)
        self.assertTrue(history.available)
        self.assertIsNone(history.failure)
        self.assertEqual(len(invoked.calls), 1)
        self.assertEqual(history.examined, 3)
        self.assertEqual(history.limits, DEFAULT_LIMITS)

        self.assertEqual(len(history.conversations), 1)
        conversation = history.conversations[0]
        self.assertEqual(conversation.harness, "claude")
        self.assertEqual(conversation.session_id, "sess-1")
        self.assertEqual(conversation.directory, "/home/dev/scripts")
        self.assertEqual(conversation.projects, ("scripts",))
        self.assertEqual(conversation.session_class, "main")
        self.assertEqual(conversation.message_count, 42)
        self.assertTrue(conversation.truncated)
        self.assertEqual([excerpt.index for excerpt in conversation.excerpts], [40, 41])
        self.assertEqual(conversation.excerpts[0].role, "user")
        self.assertEqual(conversation.excerpts[0].timestamp, "2026-01-02T03:04:05Z")

    def test_an_allowed_answer_grants_for_exactly_what_was_asked(self) -> None:
        history, _ = _read(payload=_payload())
        grant = history.grant()
        self.assertIsNotNone(grant)
        assert grant is not None
        self.assertEqual(grant.plan_dir, PLAN)
        self.assertEqual(grant.access, overseer_policy_client.ACCESS_CONTEXT)
        self.assertEqual(grant.request_digest, DIGEST)
        self.assertEqual(grant.config_fingerprint, FINGERPRINT)

    def test_withheld_conversations_are_counted_and_explained_not_located(self) -> None:
        history, _ = _read(payload=_payload())
        self.assertEqual(
            [(entry.harness, entry.session_id) for entry in history.withheld],
            [("codex", "sess-2"), ("claude", "sess-3")],
        )
        self.assertEqual(
            history.withheld_reasons(), ("out_of_scope", "ambiguous_project")
        )
        self.assertEqual(
            [field for field in vars(history.withheld[0])],
            ["harness", "session_id", "reason"],
        )

    def test_a_refusal_is_an_answer_not_a_failure(self) -> None:
        """AgentMux exits nonzero on a refusal in JSON mode too, and that is
        still an answer: available, not allowed, carrying no evidence."""
        history, _ = _read(payload=_payload(allowed=False), returncode=1)
        self.assertFalse(history.allowed)
        self.assertTrue(history.available)
        self.assertIsNone(history.failure)
        self.assertEqual(history.exit_status, 1)
        self.assertEqual(history.reason, "unenrolled")
        self.assertEqual(history.conversations, ())
        self.assertIsNone(history.grant())
        self.assertEqual(
            [outcome.alias for outcome in history.outcomes], ["scripts", "api"]
        )

    def test_a_smaller_applied_bound_is_accepted_and_reported(self) -> None:
        """Clamping downward is the contract working. What comes back is the
        bound the evidence was actually taken under, not the one asked for."""
        payload = _payload(
            limits=overseer_history.Limits(conversations=1, messages=2, excerpt_chars=8)
        )
        history, _ = _read(payload=payload)
        self.assertTrue(history.allowed)
        self.assertEqual(history.limits.conversations, 1)
        self.assertEqual(history.limits.messages, 2)
        self.assertEqual(history.limits.excerpt_chars, 8)


class HistoryUnavailableTests(unittest.TestCase):
    """No way of failing to get an answer may produce evidence."""

    def _sweep(self) -> dict[str, overseer_history.History]:
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
                "code": "invalid_since",
                "message": "since is not an RFC 3339 timestamp",
                "candidates": [],
                "parameters": {},
            },
        }
        return {
            "request_unsendable": _read(payload=allowed, plan_dir="")[0],
            "client_missing": _read(payload=allowed, amx_path=None)[0],
            "client_timeout": _read(
                payload=allowed,
                raises=subprocess.TimeoutExpired(cmd=[AMX], timeout=30.0),
            )[0],
            "client_unusable": _read(payload=allowed, raises=OSError("no such file"))[
                0
            ],
            "response_unreadable": _read(stdout="not json at all")[0],
            "response_malformed": _read(payload=missing_digest)[0],
            "unsupported_envelope": _read(payload=wrong_envelope)[0],
            "unsupported_contract": _read(payload=wrong_contract)[0],
            "request_rejected": _read(payload=rejected, returncode=2)[0],
            "answer_mismatch": _read(payload=wrong_plan)[0],
        }

    def test_every_named_failure_refuses_and_carries_nothing(self) -> None:
        sweep = self._sweep()
        self.assertEqual(sorted(sweep), sorted(overseer_history.FAILURE_REASONS))
        for failure, history in sweep.items():
            with self.subTest(failure):
                self.assertEqual(history.failure, failure)
                self.assertFalse(history.allowed)
                self.assertFalse(history.available)
                self.assertIsNone(history.grant())
                self.assertEqual(history.config_fingerprint, "")
                self.assertEqual(history.request_digest, "")
                self.assertEqual(history.conversations, ())
                self.assertEqual(history.withheld, ())
                self.assertEqual(history.outcomes, ())
                self.assertEqual(history.examined, 0)

    def test_an_empty_answer_is_unreadable_rather_than_an_empty_corpus(self) -> None:
        """Silence and "there were no conversations" must not read alike: one is
        a broken client and the other is a fact about the corpus."""
        history, _ = _read(stdout="", returncode=0)
        self.assertEqual(history.failure, "response_unreadable")
        self.assertFalse(history.available)

    def test_a_crashed_client_that_printed_evidence_is_still_refused(self) -> None:
        history, _ = _read(payload=_payload(), returncode=3)
        self.assertEqual(history.failure, "answer_mismatch")
        self.assertEqual(history.conversations, ())


class HistoryMismatchTests(unittest.TestCase):
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
        inconsistent = _payload()
        inconsistent["data"]["repositories"][1]["allowed"] = False
        refused_with_evidence = _payload(allowed=False)
        refused_with_evidence["data"]["conversations"] = [_conversation()]

        cases = {
            "another plan": _payload(plan_dir="/state/agent-session-loop/other"),
            "another access category": _payload(
                access=overseer_policy_client.ACCESS_ACTION
            ),
            "another repository path": wrong_path,
            "a shorter repository set": short_set,
            "a longer repository set": extra_set,
            "a reordered repository set": reordered,
            "an allowance over a refused repository": inconsistent,
            "a refusal that still carried evidence": refused_with_evidence,
        }
        for label, payload in cases.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertFalse(history.allowed)
                self.assertFalse(history.available)
                self.assertEqual(history.conversations, ())

    def test_an_answer_taken_under_looser_bounds_is_not_the_answer(self) -> None:
        """A bound is part of the question. Reporting a larger one than was
        asked for means the evidence was gathered under a different request."""
        asked = overseer_history.Limits(conversations=2, messages=3, excerpt_chars=50)
        cases = {
            "more conversations": overseer_history.Limits(
                conversations=10, messages=3, excerpt_chars=50
            ),
            "more messages": overseer_history.Limits(
                conversations=2, messages=20, excerpt_chars=50
            ),
            "longer excerpts": overseer_history.Limits(
                conversations=2, messages=3, excerpt_chars=2000
            ),
        }
        for label, applied in cases.items():
            with self.subTest(label):
                history, _ = _read(payload=_payload(limits=applied), limits=asked)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertEqual(history.conversations, ())

    def test_evidence_exceeding_the_bounds_it_reports_is_refused(self) -> None:
        """The bounds must have been applied, not merely announced."""
        applied = overseer_history.Limits(
            conversations=1, messages=2, excerpt_chars=DEFAULT_LIMITS.excerpt_chars
        )
        too_many_conversations = _payload(
            limits=applied,
            conversations=[
                _conversation(session_id="sess-1"),
                _conversation(session_id="sess-9"),
            ],
        )
        too_many_messages = _payload(
            limits=applied,
            conversations=[
                _conversation(excerpts=[_excerpt(1), _excerpt(2), _excerpt(3)])
            ],
        )
        too_long_excerpt = _payload(
            limits=overseer_history.Limits(
                conversations=1, messages=2, excerpt_chars=4
            ),
            conversations=[_conversation(excerpts=[_excerpt(1, text="far too long")])],
        )

        for label, payload in {
            "more conversations than the bound": too_many_conversations,
            "more excerpts than the bound": too_many_messages,
            "a longer excerpt than the bound": too_long_excerpt,
        }.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertEqual(history.conversations, ())

    def test_a_conversation_without_a_working_directory_is_refused(self) -> None:
        """The directory is what was authorized. Evidence that cannot say where
        it came from is evidence nothing decided about."""
        payload = _payload(conversations=[_conversation(directory="")])
        history, _ = _read(payload=payload)
        self.assertEqual(history.failure, "answer_mismatch")
        self.assertEqual(history.conversations, ())

    def test_a_refusal_that_locates_what_it_withheld_is_rejected(self) -> None:
        """The path, the directory, and the project are exactly what an
        exclusion withholds. An answer that reports them under the heading of
        explaining a refusal is not read past."""
        for field in overseer_history.WITHHELD_FORBIDDEN_FIELDS:
            with self.subTest(field):
                payload = _payload()
                payload["data"]["withheld"][0][field] = "/home/dev/private"
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertFalse(history.available)
                self.assertEqual(history.conversations, ())
                self.assertEqual(history.withheld, ())
                self.assertIn("withheld", history.summary)


class HistoryMalformedAnswerTests(unittest.TestCase):
    """One unusable part rejects the whole answer rather than shrinking it."""

    def test_an_unreadable_conversation_rejects_the_whole_answer(self) -> None:
        """Dropping it would turn a broken answer into a smaller-looking good
        one, and "fewer conversations than there are" is indistinguishable from
        a legitimate bound."""
        cases: dict[str, Any] = {}
        nameless = _payload()
        nameless["data"]["conversations"][0]["id"] = ""
        cases["a conversation with no session id"] = nameless
        not_an_object = _payload()
        not_an_object["data"]["conversations"] = ["sess-1"]
        cases["a conversation that is not an object"] = not_an_object
        bad_excerpts = _payload()
        bad_excerpts["data"]["conversations"][0]["excerpts"] = [{"role": "user"}]
        cases["an excerpt with no text"] = bad_excerpts
        negative_index = _payload()
        negative_index["data"]["conversations"][0]["excerpts"] = [
            {"index": -1, "role": "user", "text": "hi", "truncated": False}
        ]
        cases["an excerpt with no place in the conversation"] = negative_index

        for label, payload in cases.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "response_malformed")
                self.assertEqual(history.conversations, ())
                self.assertIsNone(history.grant())

    def test_an_unreadable_withheld_entry_rejects_the_whole_answer(self) -> None:
        payload = _payload()
        payload["data"]["withheld"][1]["reason"] = ""
        history, _ = _read(payload=payload)
        self.assertEqual(history.failure, "response_malformed")
        self.assertEqual(history.withheld, ())

    def test_an_answer_without_readable_bounds_is_refused(self) -> None:
        """Without them there is nothing to check the evidence against, which is
        the same as having no bounds at all."""
        missing = _payload()
        del missing["data"]["limits"]
        zeroed = _payload()
        zeroed["data"]["limits"]["messages"] = 0
        for label, payload in {
            "no limits block": missing,
            "a zero bound": zeroed,
        }.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "response_malformed")
                self.assertEqual(history.conversations, ())

    def test_a_descriptive_list_may_be_absent_without_failing_the_answer(self) -> None:
        """Profiles, projects, and warnings are descriptive; nothing is
        authorized on them, so an older ``amx`` that omits one still answers."""
        payload = _payload()
        del payload["data"]["warnings"]
        del payload["data"]["conversations"][0]["profiles"]
        del payload["data"]["conversations"][0]["projects"]
        history, _ = _read(payload=payload)
        self.assertTrue(history.allowed)
        self.assertEqual(history.warnings, ())
        self.assertEqual(history.conversations[0].profiles, ())
        self.assertEqual(history.conversations[0].projects, ())


if __name__ == "__main__":
    unittest.main()
