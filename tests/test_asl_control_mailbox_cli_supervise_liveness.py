"""Mailbox CLI outbox supervise liveness and supervision-end wakes.

Split from tests/test_asl_control.py.

Cases are grouped by responsibility in sibling modules.
"""

import unittest

from . import (
    asl_control_mailbox_cli_supervise_liveness_probes,
    asl_control_mailbox_cli_supervise_liveness_terminal,
    asl_control_mailbox_cli_supervise_liveness_waits,
)


class MailboxCliTestsPart9(
    asl_control_mailbox_cli_supervise_liveness_terminal.MailboxCliTestsPart9,
    asl_control_mailbox_cli_supervise_liveness_probes.MailboxCliTestsPart9,
    asl_control_mailbox_cli_supervise_liveness_waits.MailboxCliTestsPart9,
):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
