"""Mailbox CLI outbox supervise cursors and terminal records.

Split from tests/test_asl_control.py.

Cases are grouped by responsibility in sibling modules.
"""

import unittest

from . import (
    asl_control_mailbox_cli_supervise_cursors,
    asl_control_mailbox_cli_supervise_questions,
)


class MailboxCliTestsPart8(
    asl_control_mailbox_cli_supervise_questions.MailboxCliTestsPart8,
    asl_control_mailbox_cli_supervise_cursors.MailboxCliTestsPart8,
):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
