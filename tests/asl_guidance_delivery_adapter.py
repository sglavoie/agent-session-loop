# ruff: noqa: F403, F405
from .asl_guidance_delivery_harness import _FakeProcess
from .asl_guidance_delivery_support import *


class ProductionAdapterDeferralTests(unittest.TestCase):
    """Without an open protocol conversation, no adapter claims live delivery.

    All three harnesses now answer a steer through a transport: codex over the
    app-server, claude over stream JSON, copilot by declining. What they share
    is this floor -- an adapter handed no transport has no live channel and
    must say so, so a turn whose conversation never opened defers instead of
    reporting a delivery it did not make. The transports themselves are covered
    in tests/test_asl_codex_app_server.py and tests/test_asl_claude_stream.py.
    """

    def test_every_adapter_reports_no_live_transport(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                invocation = harnesses.HarnessInvocation(
                    name=name,
                    argv=[adapter.executable],
                    cwd=Path("/tmp"),
                )

                self.assertIsNone(
                    adapter.live_delivery_transport(
                        process=_FakeProcess(),
                        invocation=invocation,
                    )
                )

    def test_an_endpoint_opened_by_a_shipped_adapter_defers(self) -> None:
        turn = turn_identity()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(turn)
        adapter = harnesses.get_adapter("codex")
        invocation = harnesses.HarnessInvocation(
            name="codex", argv=[adapter.executable], cwd=Path("/tmp")
        )

        endpoint.open(
            adapter.live_delivery_transport(
                process=_FakeProcess(), invocation=invocation
            )
        )

        outcome = endpoint.submit(request_for(turn))
        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_HARNESS_UNSUPPORTED, outcome.reason)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
