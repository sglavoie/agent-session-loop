"""Test entry point for focused test_asl_repository_evidence test groups."""

from __future__ import annotations

import unittest

from .support_asl_repository_evidence_1 import (
    RepositoryEvidenceParityTests,
    WorkspaceInventoryTests,
)
from .support_asl_repository_evidence_2 import (
    RegisteredSnapshotTests,
)
from .support_asl_repository_evidence_3 import (
    WorkspaceWriteGuardClassificationTests,
)
from .support_asl_repository_evidence_4 import (
    LegacyUnobservedRepositoryTests,
)
from .support_asl_repository_evidence_5 import (
    ExternalWriteEvidenceTests,
)

for _test_case in (
    RepositoryEvidenceParityTests,
    WorkspaceInventoryTests,
    RegisteredSnapshotTests,
    WorkspaceWriteGuardClassificationTests,
    LegacyUnobservedRepositoryTests,
    ExternalWriteEvidenceTests,
):
    _test_case.__module__ = __name__
del _test_case

if __name__ == "__main__":
    unittest.main()
