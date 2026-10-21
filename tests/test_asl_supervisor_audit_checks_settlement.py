"""Deterministic checks for commit settlement and turn results."""

import unittest

from .asl_supervisor_audit_checks_settlement_cases_lanes import (
    DeterministicCheckLaneTests,
)
from .asl_supervisor_audit_checks_settlement_cases_records import (
    DeterministicCheckSettlementTests,
)

if __name__ == "__main__":
    unittest.main()
