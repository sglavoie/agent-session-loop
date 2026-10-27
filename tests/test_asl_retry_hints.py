"""Provider retry hints retain punctuation that identifies the reset instant."""

from __future__ import annotations

import os
import time
import unittest
from datetime import UTC, datetime
from unittest import mock

from agent_session_loop import retry_hints

ASL_TEST_PORTABLE = True


class RetryHintTests(unittest.TestCase):
    def test_fractional_timestamp_preserves_offset(self) -> None:
        for prefix in ("Resets at", "Reset on", "Try again after", "Will reset at"):
            with self.subTest(prefix=prefix):
                hint = retry_hints.extract_retry_hint(
                    f"{prefix} 2026-10-27T14:30:00.123+02:00. Please wait."
                )
                self.assertEqual("2026-10-27T14:30:00.123+02:00", hint)
                self.assertEqual(
                    "2026-10-27T12:30:00.123000Z",
                    retry_hints.normalized_retry_at(hint),
                )

    def test_dotted_meridiem_preserves_explicit_timezone(self) -> None:
        now = datetime(2026, 10, 27, 18, tzinfo=UTC)
        for meridiem, day, hour in (("p.m.", 27, 20), ("a.m.", 28, 8)):
            with self.subTest(meridiem=meridiem):
                hint = retry_hints.extract_retry_hint(
                    f"Resets at 2:30 {meridiem} (America/Mexico_City). Please wait."
                )
                self.assertEqual(
                    datetime(2026, 10, day, hour, 30, tzinfo=UTC),
                    retry_hints.resolve_retry_at(hint, now=now),
                )

    def test_dotted_meridiem_stops_before_following_sentence(self) -> None:
        hint = retry_hints.extract_retry_hint("Resets at 2:30 p.m. Please wait.")
        self.assertEqual("2:30 p.m", hint)
        self.assertIsNotNone(retry_hints.resolve_retry_at(hint))

    def test_plain_hints_keep_existing_sentence_and_line_boundaries(self) -> None:
        for text, expected in (
            ("Retry-after: 120", "120"),
            ("Resets in 30 minutes. Please wait.", "30 minutes"),
            ("Try again at Sep 19th, 2026 3:33 PM.", "Sep 19th, 2026 3:33 PM"),
            ("Resets at 14:30\nPlease wait.", "14:30"),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, retry_hints.extract_retry_hint(text))

    @unittest.skipUnless(hasattr(time, "tzset"), "requires local timezone control")
    def test_local_clock_uses_reset_offset_across_daylight_saving_change(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            for now, expected in (
                # The reset is tomorrow, across the spring/fall transition.
                (
                    datetime(2026, 3, 8, 4, tzinfo=UTC),
                    datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
                ),
                (
                    datetime(2026, 11, 1, 3, tzinfo=UTC),
                    datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
                ),
                # The reset is later today, after the offset changes.
                (
                    datetime(2026, 3, 8, 5, tzinfo=UTC),
                    datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
                ),
                (
                    datetime(2026, 11, 1, 4, tzinfo=UTC),
                    datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
                ),
            ):
                with self.subTest(now=now):
                    self.assertEqual(
                        expected, retry_hints.resolve_retry_at("03:30", now=now)
                    )
            # Today's missing hour must not change tomorrow's requested clock.
            self.assertEqual(
                datetime(2026, 3, 9, 6, 30, tzinfo=UTC),
                retry_hints.resolve_retry_at(
                    "02:30", now=datetime(2026, 3, 8, 8, tzinfo=UTC)
                ),
            )
