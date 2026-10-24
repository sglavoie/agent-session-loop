# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: F401
"""Shared final-review artifact contract for the runner and the autopilot.

This module is the single source of truth for the review verdict grammar and
the versioned structured review artifact:

- ``final-review.md`` — the human review document. Its final non-empty line
  must be exactly one machine-readable ``Status:`` verdict line.
- ``final-review.json`` — the machine-readable structured review artifact
  (schema :data:`REVIEW_ARTIFACT_VERSION`). Required for review sessions
  whose marker version is >= :data:`STRUCTURED_REVIEW_MIN_VERSION`; v1 review
  sessions remain Markdown-only compatible. Enforcement is pinned to that
  minimum rather than to :data:`GENERATED_REVIEW_SESSION_VERSION` so bumping
  the generated version never retroactively exempts pending review sessions
  written at an older version.

Both the runner (``agent_session_loop.runner``) and the autopilot helper
(``skills/asl/scripts/asl_autopilot.py``) build their verdict payloads through
:func:`load_review_verdict`, so they report the same payload by construction.
Parsing is fail-closed: a malformed, contradictory, or incomplete structured
artifact never yields an ``ok`` verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import (
    contracts,
    plans_errors,
    plans_files,
    plans_models,
    plans_registry,
    plans_session_ids,
    plans_session_scope,
    plans_snapshot,
    workspaces,
)
