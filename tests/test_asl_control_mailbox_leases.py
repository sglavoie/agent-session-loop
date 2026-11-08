"""Mailbox size caps, input validation, and supervision leases.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import unittest

from . import (
    asl_control_mailbox_leases_file_safety,
    asl_control_mailbox_leases_validation_and_projection,
)


class MailboxTestsPart5(
    asl_control_mailbox_leases_file_safety.MailboxTestsPart5,
    asl_control_mailbox_leases_validation_and_projection.MailboxTestsPart5,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
