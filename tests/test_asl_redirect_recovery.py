"""Test entry point for focused test_asl_redirect_recovery test groups."""

from __future__ import annotations

import unittest

from .support_asl_redirect_recovery_crashes import (
    CrashBeforeLaunchTests,
    CrashInsideTheLaunchWindowTests,
    NoLostCorrectionTests,
)
from .support_asl_redirect_recovery_damaged_evidence import (
    DamagedEvidenceTests,
    RunLoopRecoveryTests,
)
from .support_asl_redirect_recovery_redelivery import (
    LegacyJournalUpgradeTests,
    NoSilentRedeliveryTests,
    StrandedSettlementTests,
)
from .support_asl_redirect_recovery_status import (
    ReadingIsInertTests,
    ReconciliationRefusalTests,
    StatusProjectionTests,
)

ASL_TEST_PORTABLE = True

for _test_case in (
    StatusProjectionTests,
    ReadingIsInertTests,
    ReconciliationRefusalTests,
    NoLostCorrectionTests,
    CrashBeforeLaunchTests,
    CrashInsideTheLaunchWindowTests,
    NoSilentRedeliveryTests,
    LegacyJournalUpgradeTests,
    StrandedSettlementTests,
    DamagedEvidenceTests,
    RunLoopRecoveryTests,
):
    _test_case.__module__ = __name__
del _test_case

if __name__ == "__main__":
    unittest.main()
