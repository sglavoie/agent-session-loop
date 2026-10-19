from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    assurance_models_findings,
    assurance_models_summary,
    contracts,
    events,
    handoff_correction,
    pause_policy,
    plans_discovery,
    plans_handoff,
    plans_session_ids,
    plans_snapshot,
    review,
    review_continuation,
    review_session_body,
    review_session_contract,
    review_transitions,
    run_coordination_terminal,
    run_persistence,
    run_termination,
    supervisor_audit,
    turn_execution_artifacts,
    turn_execution_commit,
    turn_settlement,
    verification_checks,
    verification_transitions,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import (
    DEFAULT_MULTI_REPO_SPECS,
    write_completed_review_plan,
    write_multi_repo_plan,
    write_plan_with_ids,
)

ALIASES = tuple(alias for alias, _path, _origin in DEFAULT_MULTI_REPO_SPECS)

#: Read at its owning module rather than spelled inline: the reason is 62
#: characters and the fixture payloads that carry it nest five levels deep.
DEFERRED_REASON = verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON


def set_handoff(session_path: Path, *, status: str, files_modified: str | None) -> None:
    """Rewrite one session's handoff status and Files modified line."""
    text = session_path.read_text(encoding="utf-8")
    text = re.sub(r"- \*\*Status\*\*: \w+", f"- **Status**: {status}", text, count=1)
    if files_modified is None:
        text = re.sub(r"- \*\*Files modified\*\*: .*\n", "", text, count=1)
    else:
        text = re.sub(
            r"- \*\*Files modified\*\*: .*",
            f"- **Files modified**: {files_modified}",
            text,
            count=1,
        )
    session_path.write_text(text, encoding="utf-8")


# fmt: off
__all__ = ("inspect", "json", "re", "subprocess", "sys", "tempfile", "unittest", "Path", "Any", "assurance_models_findings", "assurance_models_summary", "contracts", "events", "handoff_correction", "pause_policy", "plans_discovery", "plans_handoff", "plans_session_ids", "plans_snapshot", "review", "review_continuation", "review_session_body", "review_session_contract", "review_transitions", "run_coordination_terminal", "run_persistence", "run_termination", "supervisor_audit", "turn_execution_artifacts", "turn_execution_commit", "turn_settlement", "verification_checks", "verification_transitions", "run_loop_view", "DEFAULT_MULTI_REPO_SPECS", "write_completed_review_plan", "write_multi_repo_plan", "write_plan_with_ids", "ALIASES", "DEFERRED_REASON", "set_handoff")
# fmt: on
