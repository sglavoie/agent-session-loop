"""Checkpoint-group tests, organized by transaction boundary."""

from .asl_overseer_checkpoint_groups_admission import *
from .asl_overseer_checkpoint_groups_reconciliation import *
from .asl_overseer_checkpoint_groups_recovery import *
from .asl_overseer_checkpoint_groups_staging import *

#: Every test here commits across three real git repositories, so the four
#: transaction-boundary groups run as parallel shards rather than one long one.
ASL_TEST_SHARDS = (
    "tests.asl_overseer_checkpoint_groups_admission",
    "tests.asl_overseer_checkpoint_groups_staging",
    "tests.asl_overseer_checkpoint_groups_recovery",
    "tests.asl_overseer_checkpoint_groups_reconciliation",
)
