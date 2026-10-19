"""Test entry point for focused adversarial redirect test groups."""

from __future__ import annotations

import unittest

from .support_asl_redirect_adversarial_1 import SlowStorageTests as _SlowStorageTests
from .support_asl_redirect_adversarial_2 import (
    BlockedPipeTests as _BlockedPipeTests,
)
from .support_asl_redirect_adversarial_2 import (
    CompletionRaceTests as _CompletionRaceTests,
)
from .support_asl_redirect_adversarial_2 import (
    ConcurrentFeedbackTests as _ConcurrentFeedbackTests,
)
from .support_asl_redirect_adversarial_3 import (
    PartialMultiRepositoryTests as _PartialMultiRepositoryTests,
)
from .support_asl_redirect_adversarial_3 import (
    ReviewRepairTests as _ReviewRepairTests,
)
from .support_asl_redirect_adversarial_3 import (
    StaleProcessIdentityTests as _StaleProcessIdentityTests,
)
from .support_asl_redirect_adversarial_3 import (
    StoppedSupervisorTests as _StoppedSupervisorTests,
)
from .support_asl_redirect_adversarial_4 import (
    ExpiredSubmissionTests as _ExpiredSubmissionTests,
)
from .support_asl_redirect_adversarial_4 import (
    MissingHistoryTests as _MissingHistoryTests,
)


class SlowStorageTests(_SlowStorageTests):
    pass


class BlockedPipeTests(_BlockedPipeTests):
    pass


class CompletionRaceTests(_CompletionRaceTests):
    pass


class ConcurrentFeedbackTests(_ConcurrentFeedbackTests):
    pass


class StaleProcessIdentityTests(_StaleProcessIdentityTests):
    pass


class PartialMultiRepositoryTests(_PartialMultiRepositoryTests):
    pass


class ReviewRepairTests(_ReviewRepairTests):
    pass


class StoppedSupervisorTests(_StoppedSupervisorTests):
    pass


class MissingHistoryTests(_MissingHistoryTests):
    pass


class ExpiredSubmissionTests(_ExpiredSubmissionTests):
    pass


del (
    _BlockedPipeTests,
    _CompletionRaceTests,
    _ConcurrentFeedbackTests,
    _ExpiredSubmissionTests,
    _MissingHistoryTests,
    _PartialMultiRepositoryTests,
    _ReviewRepairTests,
    _SlowStorageTests,
    _StaleProcessIdentityTests,
    _StoppedSupervisorTests,
)


if __name__ == "__main__":
    unittest.main()
