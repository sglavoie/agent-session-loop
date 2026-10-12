#!/usr/bin/env python3
"""Tests for quota reset parsing and supervise_run quota waits."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from asl_autopilot_test_payloads import _status_payload
from asl_autopilot_test_support import (  # noqa: F401
    QUOTA_NOW,
    _counter_clock,
    _RunScript,
    _StatusScript,
    _WallClock,
    autopilot,
    setUpModule,
    tearDownModule,
)


class ParseQuotaResetAtTest(unittest.TestCase):
    """Every `retry_at` shape `harness_failures._parse_retry_at` hands over."""

    def _parse(self, value, *, now=QUOTA_NOW):
        return autopilot._parse_quota_reset_at(value, now=now)

    def test_bare_digits_are_seconds_from_now(self) -> None:
        self.assertEqual(self._parse("3600"), QUOTA_NOW + timedelta(hours=1))
        self.assertEqual(self._parse(" 120 "), QUOTA_NOW + timedelta(minutes=2))

    def test_iso_timestamp_tolerates_trailing_z(self) -> None:
        expected = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
        self.assertEqual(self._parse("2026-07-31T06:00:00Z"), expected)
        self.assertEqual(self._parse("2026-07-31T06:00:00+00:00"), expected)

    def test_naive_iso_timestamp_is_read_as_local_time(self) -> None:
        parsed = self._parse("2026-07-31T06:00:00")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNotNone(parsed.tzinfo)
        # Local time, so the instant depends on the host zone; the wall-clock
        # reading is what must survive.
        self.assertEqual(parsed.replace(tzinfo=None), datetime(2026, 7, 31, 6, 0))

    def test_relative_hours_and_minutes(self) -> None:
        self.assertEqual(self._parse("3 hours"), QUOTA_NOW + timedelta(hours=3))
        self.assertEqual(self._parse("1 hour"), QUOTA_NOW + timedelta(hours=1))
        self.assertEqual(self._parse("45 minutes"), QUOTA_NOW + timedelta(minutes=45))
        self.assertEqual(self._parse("1 minute"), QUOTA_NOW + timedelta(minutes=1))

    def test_time_of_day_with_zone_resolves_to_next_occurrence(self) -> None:
        # 12:00Z is 08:00 in New York, so 10:30pm is later the same day.
        parsed = self._parse("10:30pm (America/New_York)")
        self.assertEqual(parsed, datetime(2026, 7, 31, 2, 30, tzinfo=UTC))
        # A time already past today rolls to tomorrow.
        parsed = self._parse("6:00am (America/New_York)")
        self.assertEqual(parsed, datetime(2026, 7, 31, 10, 0, tzinfo=UTC))

    def test_time_of_day_without_zone_uses_local_wall_clock(self) -> None:
        local_now = QUOTA_NOW.astimezone()
        target_hour = (local_now.hour + 2) % 24
        parsed = self._parse(f"{target_hour:02d}:15")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.astimezone().hour, target_hour)
        self.assertEqual(parsed.astimezone().minute, 15)
        self.assertGreater(parsed, QUOTA_NOW)

    def test_unparseable_hints_return_none(self) -> None:
        for value in (
            None,
            "",
            "   ",
            "soon",
            "when the quota resets",
            "99:99",
            "13:00pm (America/New_York)",
            "2026-13-45T99:00:00Z",
            "10:30pm (Not/AZone)",
        ):
            self.assertIsNone(self._parse(value), value)

    def test_out_of_range_hints_decline_instead_of_raising(self) -> None:
        # `harness_failures._parse_retry_at` bounds the hint to 60 characters and
        # not at all in magnitude, so syntactically accepted provider text can name an
        # instant off the end of the calendar. Both overflow shapes appear: a
        # span no `timedelta` can hold, and one no `datetime` can reach.
        for value in (
            "9" * 60,
            "9999999999999",
            "999999999999999 hours",
            "99999999999 minutes",
            # A naive timestamp is read as local time, and converting the last
            # day of year 9999 overflows in every zone.
            "9999-12-31T23:59:59",
        ):
            self.assertIsNone(self._parse(value), value)


class WaitForQuotaResetTest(unittest.TestCase):
    """The wait sleeps in bounded slices measured against the wall clock."""

    def _wait(self, deadline, clock, **kwargs):
        beats: list = []
        slept = autopilot._wait_for_quota_reset(
            deadline,
            now_fn=clock,
            sleep=clock.sleep,
            heartbeat=beats.append,
            **kwargs,
        )
        return slept, beats

    def test_sleeps_in_slices_summing_to_the_remaining_time(self) -> None:
        clock = _WallClock()
        slept, beats = self._wait(QUOTA_NOW + timedelta(seconds=95), clock)
        self.assertEqual(clock.sleeps, [30.0, 30.0, 30.0, 5.0])
        self.assertEqual(slept, 95.0)
        self.assertEqual(clock.now, QUOTA_NOW + timedelta(seconds=95))
        # Every slice heartbeats the remaining time.
        self.assertEqual(len(beats), 4)
        self.assertIn("95s remaining", beats[0])
        self.assertIn("5s remaining", beats[-1])

    def test_past_deadline_returns_without_sleeping(self) -> None:
        clock = _WallClock()
        slept, beats = self._wait(QUOTA_NOW - timedelta(seconds=1), clock)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(slept, 0.0)
        self.assertEqual(beats, [])

    def test_suspend_past_the_deadline_returns_after_one_slice(self) -> None:
        # The machine sleeps through the reset: the next slice reads a past
        # deadline off the wall clock and returns instead of serving out the
        # remaining hour.
        clock = _WallClock(suspend_after=1, suspend_seconds=7200.0)
        slept, _ = self._wait(QUOTA_NOW + timedelta(hours=1), clock)
        self.assertEqual(clock.sleeps, [30.0])
        self.assertEqual(slept, 30.0)

    def test_slice_length_is_configurable(self) -> None:
        clock = _WallClock()
        self._wait(QUOTA_NOW + timedelta(seconds=25), clock, slice_seconds=10.0)
        self.assertEqual(clock.sleeps, [10.0, 10.0, 5.0])


class SuperviseRunQuotaWaitTest(unittest.TestCase):
    """`--wait-quota-reset` sleeps a quota pause out and relaunches."""

    def _quota_pause(self, *, retry_at="3600", reason="quota-exhausted"):
        return {
            "ok": False,
            "paused": True,
            "reason": reason,
            "pause_reason": reason,
            "pause_message": f"session paused: {reason}",
            "pause_details": {
                "failure_classification": {
                    "reason": reason,
                    "retryable": False,
                    "retry_at": retry_at,
                }
            },
        }

    def _supervise(self, statuses, payloads, *, clock=None, beats=None, **kwargs):
        wall = clock or _WallClock()
        run = _RunScript(payloads)
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=_StatusScript(statuses),
            run_invoker=run,
            clock=_counter_clock(),
            sleep=wall.sleep,
            heartbeat=(beats.append if beats is not None else (lambda *_: None)),
            wait_quota_reset=True,
            wall_clock=wall,
            **kwargs,
        )
        return result, run, wall

    def test_waits_reset_plus_buffer_then_relaunches_and_completes(self) -> None:
        beats: list = []
        result, run, wall = self._supervise(
            [_status_payload(1), _status_payload(1), _status_payload(0)],
            [self._quota_pause(retry_at="3600"), {"ok": True, "reason": "complete"}],
            beats=beats,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # Two chunks: the quota pause slept, then the same plan relaunched.
        self.assertEqual(run.chunks, [1, 2])
        # Sleeps total the parsed reset plus the buffer, in 30s slices.
        expected = 3600.0 + autopilot.DEFAULT_QUOTA_RESET_BUFFER_SECONDS
        self.assertEqual(sum(wall.sleeps), expected)
        self.assertEqual(set(wall.sleeps[:-1]), {autopilot.QUOTA_WAIT_SLICE_SECONDS})
        self.assertEqual(wall.now, QUOTA_NOW + timedelta(seconds=expected))
        joined = "\n".join(beats)
        self.assertIn("quota exhausted; waiting 3900s for reset (resume 1/", joined)
        self.assertIn("quota reset wait: 3900s remaining", joined)

    def test_each_retry_at_form_relaunches(self) -> None:
        for retry_at, wait_seconds in (
            ("3600", 3600.0),
            ("2026-07-30T13:00:00Z", 3600.0),
            ("2 hours", 7200.0),
            ("30 minutes", 1800.0),
            ("9:00am (America/New_York)", 3600.0),
        ):
            with self.subTest(retry_at=retry_at):
                result, run, wall = self._supervise(
                    [_status_payload(1), _status_payload(1), _status_payload(0)],
                    [
                        self._quota_pause(retry_at=retry_at),
                        {"ok": True, "reason": "complete"},
                    ],
                )
                self.assertEqual(result["reason"], "complete", result)
                self.assertEqual(run.chunks, [1, 2])
                self.assertEqual(
                    sum(wall.sleeps),
                    wait_seconds + autopilot.DEFAULT_QUOTA_RESET_BUFFER_SECONDS,
                )

    def test_reset_already_past_waits_only_the_buffer(self) -> None:
        result, run, wall = self._supervise(
            [_status_payload(1), _status_payload(1), _status_payload(0)],
            [
                self._quota_pause(retry_at="2026-07-30T11:00:00Z"),
                {"ok": True, "reason": "complete"},
            ],
        )
        self.assertEqual(result["reason"], "complete", result)
        self.assertEqual(run.chunks, [1, 2])
        self.assertEqual(sum(wall.sleeps), autopilot.DEFAULT_QUOTA_RESET_BUFFER_SECONDS)

    def test_suspend_past_the_reset_relaunches_promptly(self) -> None:
        clock = _WallClock(suspend_after=1, suspend_seconds=86400.0)
        result, run, wall = self._supervise(
            [_status_payload(1), _status_payload(1), _status_payload(0)],
            [self._quota_pause(retry_at="3600"), {"ok": True, "reason": "complete"}],
            clock=clock,
        )
        self.assertEqual(result["reason"], "complete", result)
        self.assertEqual(run.chunks, [1, 2])
        self.assertEqual(wall.sleeps, [autopilot.QUOTA_WAIT_SLICE_SECONDS])

    def test_missing_and_unparseable_reset_hints_skip_the_wait(self) -> None:
        for retry_at, skipped in (
            (None, "no-reset-hint"),
            ("some time later today", "unparseable-reset-hint"),
        ):
            with self.subTest(retry_at=retry_at):
                result, run, wall = self._supervise(
                    [_status_payload(1)], [self._quota_pause(retry_at=retry_at)]
                )
                self.assertEqual(result["reason"], "user_required")
                self.assertEqual(run.chunks, [1])
                self.assertEqual(wall.sleeps, [])
                self.assertEqual(result["quota_wait"]["skipped"], skipped)

    def test_out_of_range_reset_hints_fail_closed(self) -> None:
        # Extreme but syntactically accepted hints stop `user_required` with a
        # named guard rather than raising out of `supervise_run`. A hint no
        # `timedelta` or `datetime` can represent is unparseable; one that
        # resolves to a real instant eight millennia out is declined by the
        # ceiling, which reports the span it refused.
        for retry_at, skipped in (
            ("9" * 60, "unparseable-reset-hint"),
            ("999999999999999 hours", "unparseable-reset-hint"),
            ("9999-12-31T23:59:59Z", "wait-exceeds-ceiling"),
        ):
            with self.subTest(retry_at=retry_at):
                result, run, wall = self._supervise(
                    [_status_payload(1)], [self._quota_pause(retry_at=retry_at)]
                )
                self.assertEqual(result["reason"], "user_required")
                self.assertEqual(run.chunks, [1])
                self.assertEqual(wall.sleeps, [])
                self.assertEqual(result["quota_wait"]["skipped"], skipped)

    def test_wait_beyond_the_ceiling_is_skipped(self) -> None:
        result, run, wall = self._supervise(
            [_status_payload(1)],
            [self._quota_pause(retry_at="2027-07-30T12:00:00Z")],
        )
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1])
        self.assertEqual(wall.sleeps, [])
        quota_wait = result["quota_wait"]
        self.assertEqual(quota_wait["skipped"], "wait-exceeds-ceiling")
        self.assertGreater(
            quota_wait["wait_seconds"], autopilot.DEFAULT_QUOTA_WAIT_CEILING_SECONDS
        )
        self.assertEqual(
            quota_wait["ceiling_seconds"],
            autopilot.DEFAULT_QUOTA_WAIT_CEILING_SECONDS,
        )

    def test_resume_budget_converges_a_permanently_exhausted_provider(self) -> None:
        # Every relaunch re-fails with quota, so each attempt spends a resume
        # and the loop stops at the cap instead of sleeping forever.
        result, run, wall = self._supervise(
            [_status_payload(1)],
            [self._quota_pause(retry_at="600")],
            max_quota_resumes=2,
        )
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(sum(wall.sleeps), 2 * (600.0 + 300.0))
        quota_wait = result["quota_wait"]
        self.assertEqual(quota_wait["skipped"], "max-resumes-spent")
        self.assertEqual(quota_wait["resumes"], 2)
        self.assertEqual(quota_wait["max_resumes"], 2)

    def test_chunk_budget_bounds_the_wait_independently(self) -> None:
        result, run, _ = self._supervise(
            [_status_payload(1)],
            [self._quota_pause(retry_at="600")],
            max_chunks=1,
            max_quota_resumes=8,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "capped")
        self.assertEqual(run.chunks, [1])

    def test_auth_and_invalid_model_never_wait(self) -> None:
        for reason in ("auth-failed", "invalid-model"):
            with self.subTest(reason=reason):
                result, run, wall = self._supervise(
                    [_status_payload(1)],
                    [self._quota_pause(retry_at="3600", reason=reason)],
                )
                self.assertEqual(result["reason"], "user_required")
                self.assertEqual(run.chunks, [1])
                self.assertEqual(wall.sleeps, [])
                self.assertEqual(result["quota_wait"]["skipped"], "reason-not-quota")

    def test_relaunch_after_the_wait_carries_no_operator_reply(self) -> None:
        # Reply injection is chunk-1 only, so the chunk resuming after a quota
        # wait must not re-deliver the operator's reply. Exercised through the
        # default run invoker, which is where that rule lives.
        replies: list = []
        payloads = iter(
            [
                self._quota_pause(retry_at="600"),
                {"ok": True, "reason": "complete"},
            ]
        )

        def fake_invoke_run(target, **kwargs):
            replies.append(kwargs.get("reply_text"))
            return next(payloads)

        wall = _WallClock()
        original = autopilot._invoke_run
        autopilot._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {
                    "plan_dir": "/tmp/plan",
                    "argv": ["asl", "run", "/tmp/plan", "--json"],
                    "no_mailbox": True,
                },
                status_invoker=_StatusScript(
                    [_status_payload(1), _status_payload(1), _status_payload(0)]
                ),
                reply_text="use the cached parser",
                clock=_counter_clock(),
                sleep=wall.sleep,
                heartbeat=lambda *_: None,
                wait_quota_reset=True,
                wall_clock=wall,
            )
        finally:
            autopilot._invoke_run = original
        self.assertEqual(result["reason"], "complete", result)
        self.assertEqual(replies, ["use the cached parser", None])


if __name__ == "__main__":
    unittest.main()
