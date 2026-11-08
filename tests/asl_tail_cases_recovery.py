import unittest
from unittest import mock

from . import asl_tail_cases_recovery_inline, asl_tail_cases_recovery_replay
from .asl_tail_support import *


class TailFollowerRecoveryTests(
    asl_tail_cases_recovery_replay.TailFollowerRecoveryTests,
    asl_tail_cases_recovery_inline.TailFollowerRecoveryTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
