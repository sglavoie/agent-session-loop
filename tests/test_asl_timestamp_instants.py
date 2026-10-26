"""Persisted timestamps compare as instants, whatever their spelling."""

from __future__ import annotations

import os
import time
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from agent_session_loop import (
    assurance_catchup,
    bridge_protocol,
    maintenance_plan,
    retention_projection,
)

ASL_TEST_PORTABLE = True

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def _report(*finished_at: str) -> SimpleNamespace:
    runs = [
        SimpleNamespace(
            reason=retention_projection.RETAIN_NEWEST_RUN,
            finished_at=stamp,
            run_id=f"run-{index}",
        )
        for index, stamp in enumerate(finished_at)
    ]
    entry = SimpleNamespace(retention=SimpleNamespace(plan_dir="/plan", runs=runs))
    return SimpleNamespace(plans=[entry])


class TimestampInstantTests(unittest.TestCase):
    def test_compaction_eligibility_reads_naive_and_unparsable_stamps(self) -> None:
        report = _report(
            "2026-01-01T00:00:00",  # naive: old enough
            "2026-01-01T00:00:00+02:00",
            "2026-10-25T11:00:00",  # naive: too recent
            "not a timestamp",
        )

        eligible = maintenance_plan._compactable_runs(
            report,  # type: ignore[arg-type]
            now=NOW,
            compaction_age_days=7,
        )

        self.assertEqual(["run-0", "run-1"], [run.run_id for _, run in eligible])

    def test_the_watermark_orders_naive_and_aware_stamps_by_instant(self) -> None:
        stamps = [
            "2026-10-25T10:00:00",
            "2026-10-25T09:30:00-01:00",  # 10:30 UTC, the latest
            "garbage",
        ]
        latest = max(stamps, key=assurance_catchup._moment)
        self.assertEqual("2026-10-25T09:30:00-01:00", latest)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_a_naive_bridge_stamp_is_utc_regardless_of_local_zone(self) -> None:
        aware = bridge_protocol._epoch("2026-10-25T12:00:00+00:00")
        try:
            with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
                time.tzset()
                naive = bridge_protocol._epoch("2026-10-25T12:00:00")
        finally:
            time.tzset()
        self.assertEqual(aware, naive)
        self.assertIsNone(bridge_protocol._epoch("nope"))


if __name__ == "__main__":
    unittest.main()
