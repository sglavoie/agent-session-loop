"""Test entry point for focused test_asl_redirect_recovery test groups."""

from __future__ import annotations

import unittest

from .support_asl_redirect_recovery_1 import (
    ReadingIsInertTests,
    ReconciliationRefusalTests,
    StatusProjectionTests,
)
from .support_asl_redirect_recovery_2 import (
    CrashBeforeLaunchTests,
    CrashInsideTheLaunchWindowTests,
    NoLostCorrectionTests,
)
from .support_asl_redirect_recovery_3 import (
    LegacyJournalUpgradeTests,
    NoSilentRedeliveryTests,
    StrandedSettlementTests,
)
from .support_asl_redirect_recovery_4 import (
    DamagedEvidenceTests,
    RunLoopRecoveryTests,
)

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
