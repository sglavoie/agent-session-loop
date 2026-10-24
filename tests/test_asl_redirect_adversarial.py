"""Test entry point for focused adversarial redirect test groups."""

from __future__ import annotations

import unittest

from .support_asl_redirect_adversarial_missing_history import (
    ExpiredSubmissionTests as _ExpiredSubmissionTests,
)
from .support_asl_redirect_adversarial_missing_history import (
    MissingHistoryTests as _MissingHistoryTests,
)
from .support_asl_redirect_adversarial_races import (
    BlockedPipeTests as _BlockedPipeTests,
)
from .support_asl_redirect_adversarial_races import (
    CompletionRaceTests as _CompletionRaceTests,
)
from .support_asl_redirect_adversarial_races import (
    ConcurrentFeedbackTests as _ConcurrentFeedbackTests,
)
from .support_asl_redirect_adversarial_slow_storage import (
    SlowStorageTests as _SlowStorageTests,
)
from .support_asl_redirect_adversarial_stale_owners import (
    PartialMultiRepositoryTests as _PartialMultiRepositoryTests,
)
from .support_asl_redirect_adversarial_stale_owners import (
    ReviewRepairTests as _ReviewRepairTests,
)
from .support_asl_redirect_adversarial_stale_owners import (
    StaleProcessIdentityTests as _StaleProcessIdentityTests,
)
from .support_asl_redirect_adversarial_stale_owners import (
    StoppedSupervisorTests as _StoppedSupervisorTests,
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
