from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    handoff_guards,
    review_continuation,
    review_cycle,
    review_delta,
    review_transitions,
    run_persistence,
    runner,
    session_verification,
)
from tests.support import assert_no_runner_import, called_names, read_jsonl
from tests.support_base import call_receivers as _call_receivers

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_prior_review_cap",
    "_review_reopen_record",
    "_record_review_reopen",
    "_has_successful_review_reopen",
    "_set_review_resume",
    "_terminal_review_reason",
    "_set_existing_review_verdict",
    "_attest_executed_review",
    "_record_review_cycle",
    "_advance_review_cycle",
)


def _resume(**overrides: Any) -> review_continuation.ReviewResume:
    """A continuation projection with only the fields a caller cares about."""
    fields: dict[str, Any] = {
        "source_cycle": 1,
        "review_session_id": "2",
        "source_disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        "action": "materialize-repairs",
        "eligible": True,
        "blocker": None,
        "historical_materializations": 1,
        "minimum_required_cap": 2,
        "selected_cap": 2,
    }
    fields.update(overrides)
    return review_continuation.ReviewResume(**fields)
