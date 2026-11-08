"""Durable journal transitions, plan locks, and interruption accounting."""

import unittest

from . import (
    workbench_operations_journal_durability,
    workbench_operations_journal_reconciliation,
)

ASL_TEST_LANE = "integration"


class ReconciliationTests(
    workbench_operations_journal_reconciliation.ReconciliationTests
):
    """Preserve the established test IDs."""


class PlanLockTests(workbench_operations_journal_reconciliation.PlanLockTests):
    """Preserve the established test IDs."""


class InterruptionTests(workbench_operations_journal_reconciliation.InterruptionTests):
    """Preserve the established test IDs."""


class DurableJournalTests(workbench_operations_journal_durability.DurableJournalTests):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
