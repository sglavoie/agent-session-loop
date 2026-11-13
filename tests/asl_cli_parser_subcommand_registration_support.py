"""Subcommand handler registration, import hygiene and control flag parsing."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_session_loop import (
    bridge_protocol,
    cli,
    cli_defaults,
    cli_parser_support,
    commit_message,
    control_cli,
    control_queue,
    metrics,
    plans_cli,
    prepare_expand_ingest,
    prepare_models,
    redirect_ack,
    redirect_admission,
    redirect_store_vocab,
    repository_registration,
    retention_projection,
    review,
    turn_execution_constants,
    verification_contract_check,
)
from tests.support import (
    REPO_ROOT,
)


class ParserSubcommandRegistrationTestsBase(unittest.TestCase):
    """Subcommand handler registration, import hygiene and control flag parsing."""


class ParserFlagSpellingTestsBase(unittest.TestCase):
    """Long options never match by prefix, and wait timeouts share spellings."""

    def _parse_error(self, argv: list[str]) -> str:
        parser = cli.build_parser(prog="asl")
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(argv)
        self.assertEqual(2, raised.exception.code)
        return stderr.getvalue()

    def _help(self, argv: list[str]) -> str:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            cli.build_parser(prog="asl").parse_args([*argv, "--help"])
        return " ".join(stdout.getvalue().split())


class NumericFlagValidationTestsBase(unittest.TestCase):
    """Numeric flags refuse NaN, infinity and out-of-range values at parse time.

    Each of these used to reach its handler unchecked and either crash with a
    traceback (an OverflowError from ``socket.bind`` or ``timedelta``), print a
    bare ``NaN`` into ``--json`` output, or install a schedule launchd cannot
    honor. argparse now refuses them with exit 2 and a message naming the flag.
    """

    def _refused(self, argv: list[str]) -> str:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.build_parser(prog="asl").parse_args(argv)
        self.assertEqual(2, raised.exception.code)
        return stderr.getvalue()


__all__ = [
    "NumericFlagValidationTestsBase",
    "ParserFlagSpellingTestsBase",
    "ParserSubcommandRegistrationTestsBase",
    "REPO_ROOT",
    "argparse",
    "bridge_protocol",
    "cli",
    "cli_defaults",
    "cli_parser_support",
    "commit_message",
    "control_cli",
    "control_queue",
    "io",
    "metrics",
    "mock",
    "prepare_expand_ingest",
    "prepare_models",
    "redirect_ack",
    "redirect_admission",
    "redirect_stderr",
    "redirect_stdout",
    "redirect_store_vocab",
    "repository_registration",
    "retention_projection",
    "review",
    "subprocess",
    "sys",
    "tempfile",
    "turn_execution_constants",
    "unittest",
    "verification_contract_check",
    "plans_cli",
]
