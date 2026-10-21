# mypy: ignore-errors
from .asl_overseer_history_support import *
from .asl_overseer_history_support import (  # noqa: F401
    _CannedAmx,
    _conversation,
    _excerpt,
    _outcome,
    _payload,
    _read,
)


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
