"""WaitingReadTests: waiting."""

from __future__ import annotations

from .workbench_uplink_support import (
    CONNECTOR,
    TARGET,
    WaitingReadTestsBase,
    answer,
    receipt_envelope,
    time,
)


class WaitingReadTests(WaitingReadTestsBase):
    def test_a_detail_answer_settles_the_waiting_read(self) -> None:
        def settle(request_id: str) -> None:
            now = time.time()
            self.uplink.ingest(
                answer(request_id, at=now, content='{"ok": true}'), now=now
            )

        self.settle = settle  # type: ignore[method-assign]
        answers = self.answered()
        self.assertEqual(answers[0].outcome, "answered")
        self.assertEqual(answers[0].content, '{"ok": true}')
        self.assertEqual(answers[0].request_id, self.carried["request_id"])

    def test_a_receipt_refusing_the_read_settles_it_too(self) -> None:
        """A declined read is an answered request, and it arrives the same way.

        The alternative -- leaving a refused read to time out -- would tell the
        operator the Mac was slow when the Mac had already said no. The outcome
        is `unavailable` rather than `refused`, because on a read a receipt is
        the connector declining to look rather than the plan saying no; that
        naming is the read client's and it is asserted here so this path cannot
        start reporting a refusal as an answer.
        """

        def settle(request_id: str) -> None:
            now = time.time()
            self.uplink.ingest(
                receipt_envelope(
                    payload={
                        "outcome": "refused",
                        "observed_at": now,
                        "detail_code": "no-result",
                    },
                    capability="detail",
                    connector_id=CONNECTOR,
                    target_id=TARGET,
                    sent_at=now,
                    request_id=request_id,
                ),
                now=now,
            )

        self.settle = settle  # type: ignore[method-assign]
        answers = self.answered()
        self.assertEqual(answers[0].outcome, "unavailable")
        self.assertEqual(answers[0].detail_code, "no-result")
        self.assertEqual(answers[0].note, "the connector refused the read")
