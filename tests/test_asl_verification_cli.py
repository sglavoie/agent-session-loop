"""`asl verify` command tests."""

from __future__ import annotations

import unittest

from . import asl_verification_cli_errors_and_artifacts, asl_verification_cli_outcomes


class VerifyCliTests(
    asl_verification_cli_outcomes.VerifyCliTests,
    asl_verification_cli_errors_and_artifacts.VerifyCliTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
