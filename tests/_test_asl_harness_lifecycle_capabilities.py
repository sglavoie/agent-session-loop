# mypy: ignore-errors
"""Which shipped adapters capture or resume conversations."""

# ruff: noqa: F403, F405
from ._test_asl_harness_lifecycle_support import *


class ShippedAdapterCapabilityTests(unittest.TestCase):
    """No shipped adapter claims continuity it has not been wired for."""

    #: The adapters whose transports actually capture and resume today. Named
    #: rather than derived, so wiring a harness is a deliberate edit here and
    #: not something a flipped class flag can do quietly.
    WIRED = frozenset({"codex", "claude", "copilot"})

    def test_only_wired_adapters_capture_or_resume(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                wired = name in self.WIRED
                self.assertEqual(wired, adapter.captures_conversations)
                self.assertEqual(wired, adapter.resumes_conversations)
                self.assertEqual(wired, harnesses.continues_conversations(name))

    def test_both_halves_are_wired_together(self) -> None:
        """Capture without resume records what nothing can use; resume without
        capture continues a conversation the run never wrote down."""
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                self.assertEqual(
                    adapter.captures_conversations, adapter.resumes_conversations
                )

    def test_an_unknown_harness_does_not_continue_conversations(self) -> None:
        self.assertFalse(harnesses.continues_conversations("nonesuch"))

    def test_the_capability_report_follows_the_adapters(self) -> None:
        """One producer: the report cannot promise what the turn would refuse."""
        report = conversations.capability_report()

        reported = {
            capability.harness: capability.state for capability in report.capabilities
        }
        self.assertEqual(set(harnesses.HARNESS_NAMES), set(reported))
        for name, state in reported.items():
            with self.subTest(harness=name):
                expected = (
                    "available" if harnesses.continues_conversations(name) else "staged"
                )
                self.assertEqual(expected, state)

    def test_every_adapter_accepts_a_lifecycle_on_open_transport(self) -> None:
        """The seam is on the boundary, not on one adapter that opted in."""
        lifecycle = harnesses.TurnLifecycle(conversation=harnesses.TurnConversation())
        process = _FakeProcess(running=False)
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                invocation = harnesses.HarnessInvocation(
                    name=adapter.name, argv=[adapter.executable], cwd=Path.cwd()
                )
                transport = adapter.open_transport(
                    process=process,  # type: ignore[arg-type]
                    invocation=invocation,
                    prompt="do the work",
                    lifecycle=lifecycle,
                )
                # An exec invocation opens no protocol conversation; copilot's
                # transport exists only to decline a live steer.
                self.assertIsNone(lifecycle.conversation.native_id)
                if transport is not None:
                    self.assertIsInstance(transport, harnesses.HarnessTransport)
