"""Subcommand handler registration, import hygiene and control flag parsing.

Cases are grouped by responsibility in sibling modules.
"""

import unittest

from . import (
    asl_cli_parser_subcommand_registration_flags,
    asl_cli_parser_subcommand_registration_handlers,
    asl_cli_parser_subcommand_registration_help,
    asl_cli_parser_subcommand_registration_numeric,
    asl_cli_parser_subcommand_registration_spelling,
    asl_cli_parser_subcommand_registration_workbench,
)


class ParserSubcommandRegistrationTests(
    asl_cli_parser_subcommand_registration_help.ParserSubcommandRegistrationTests,
    asl_cli_parser_subcommand_registration_handlers.ParserSubcommandRegistrationTests,
    asl_cli_parser_subcommand_registration_flags.ParserSubcommandRegistrationTests,
):
    """Preserve the established test IDs."""


class ParserFlagSpellingTests(
    asl_cli_parser_subcommand_registration_spelling.ParserFlagSpellingTests,
):
    """Preserve the established test IDs."""


class NumericFlagValidationTests(
    asl_cli_parser_subcommand_registration_numeric.NumericFlagValidationTests,
):
    """Preserve the established test IDs."""


class WorkbenchGroupFlagTests(
    asl_cli_parser_subcommand_registration_workbench.WorkbenchGroupFlagTests,
):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
