"""Mailbox CLI outbox supervise cursors and terminal records.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest import mock

from agent_session_loop import (
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_models,
    mailbox_cli_outbox,
    mailbox_cli_waits,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    mailbox_validation,
    mailbox_vocabulary,
    mailbox_waits,
    state,
    supervision_liveness,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase


class MailboxCliTestsPart8Base(MailboxCliTestsBase):
    """Shared base for MailboxCliTestsPart8."""


__all__ = [
    "MailboxCliTestsBase",
    "MailboxCliTestsPart8Base",
    "UTC",
    "datetime",
    "io",
    "json",
    "mailbox_appenders",
    "mailbox_cli_constants",
    "mailbox_cli_models",
    "mailbox_cli_outbox",
    "mailbox_cli_waits",
    "mailbox_constants",
    "mailbox_cursors",
    "mailbox_models",
    "mailbox_snapshot",
    "mailbox_validation",
    "mailbox_vocabulary",
    "mailbox_waits",
    "mock",
    "os",
    "redirect_stdout",
    "state",
    "supervision_liveness",
    "threading",
    "time",
    "timedelta",
    "unittest",
    "uuid",
]
