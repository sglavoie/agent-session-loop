"""Mailbox CLI ask deadlines, resume, and inbox text budgets.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import unittest

from . import (
    asl_control_mailbox_cli_ask_deadlines_and_rendering,
    asl_control_mailbox_cli_ask_polling_and_input,
    asl_control_mailbox_cli_ask_resume,
)


class MailboxCliTestsPart6(
    asl_control_mailbox_cli_ask_deadlines_and_rendering.MailboxCliTestsPart6,
    asl_control_mailbox_cli_ask_resume.MailboxCliTestsPart6,
    asl_control_mailbox_cli_ask_polling_and_input.MailboxCliTestsPart6,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
