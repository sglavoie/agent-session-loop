"""Completion contract parsing for session files.

Each session file may include a ``## Completion Contract`` section with a
``### Done when`` bullet list of human-readable criteria and a ``### Verify``
JSON block describing executable checks.

The package is stdlib-only, so the machine-executable recipe is JSON rather
than YAML. The parser validates schema, session IDs, command argv lists,
timeouts, file checks, and regex checks, and raises :class:`plans_errors.PlanError`
with clear messages for any malformed contract so callers see a consistent
error surface.
"""

from __future__ import annotations

import json as json
import re as re
from collections.abc import Callable as Callable
from dataclasses import dataclass as dataclass
from dataclasses import fields as fields
from pathlib import Path as Path
from pathlib import PurePosixPath as PurePosixPath
from pathlib import PureWindowsPath as PureWindowsPath
from typing import Any as Any
from typing import Literal as Literal

from .contract_checks import _ALLOWED_CHECK_KEYS as _ALLOWED_CHECK_KEYS
from .contract_checks import _INTERNAL_CHECK_FIELDS as _INTERNAL_CHECK_FIELDS
from .contract_checks import CHECK_PARSERS as CHECK_PARSERS
from .contract_checks import CHECK_PRESENCE_SEMANTICS as CHECK_PRESENCE_SEMANTICS
from .contract_checks import _baseline_expectation as _baseline_expectation
from .contract_checks import _check_name as _check_name
from .contract_checks import _on_failure_of as _on_failure_of
from .contract_checks import _parse_check as _parse_check
from .contract_checks import _parse_command_check as _parse_command_check
from .contract_checks import _parse_file_exists_check as _parse_file_exists_check
from .contract_checks import _parse_regex_in_file_check as _parse_regex_in_file_check
from .contract_checks import _payload_keys as _payload_keys
from .contract_checks import _repository_alias as _repository_alias
from .contract_checks import _require_positive_number as _require_positive_number
from .contract_checks import _require_str as _require_str
from .contract_checks import _review_replay as _review_replay
from .contract_checks import (
    _validate_repository_relative_value as _validate_repository_relative_value,
)
from .contract_checks import check_payload as check_payload
from .contract_markdown import _extract_bullets as _extract_bullets
from .contract_markdown import _extract_json_block as _extract_json_block
from .contract_markdown import _final_handoff_start as _final_handoff_start
from .contract_markdown import _find_heading_index as _find_heading_index
from .contract_markdown import _heading_level as _heading_level
from .contract_markdown import _section_end as _section_end
from .contract_markdown import (
    completion_contract_heading_indices as completion_contract_heading_indices,
)
from .contract_markdown import fenced_line_flags as fenced_line_flags
from .contract_markdown import (
    strip_completion_contract_sections as strip_completion_contract_sections,
)
from .contract_models import _FENCE_RE as _FENCE_RE
from .contract_models import BASELINE_EXPECTATIONS as BASELINE_EXPECTATIONS
from .contract_models import COMPLETION_CONTRACT_HEADING as COMPLETION_CONTRACT_HEADING
from .contract_models import (
    DEFAULT_BASELINE_CHECK_BUDGET_SECONDS as DEFAULT_BASELINE_CHECK_BUDGET_SECONDS,
)
from .contract_models import (
    DEFAULT_BASELINE_EXPECTATION as DEFAULT_BASELINE_EXPECTATION,
)
from .contract_models import (
    DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS as DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS,
)
from .contract_models import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS as DEFAULT_COMMAND_TIMEOUT_SECONDS,
)
from .contract_models import DONE_WHEN_HEADING as DONE_WHEN_HEADING
from .contract_models import MAX_COMMAND_TIMEOUT_SECONDS as MAX_COMMAND_TIMEOUT_SECONDS
from .contract_models import VERIFY_HEADING as VERIFY_HEADING
from .contract_models import BaselineExpectation as BaselineExpectation
from .contract_models import CommandCheck as CommandCheck
from .contract_models import CommandKind as CommandKind
from .contract_models import CompletionContract as CompletionContract
from .contract_models import FileExistsCheck as FileExistsCheck
from .contract_models import FileExistsKind as FileExistsKind
from .contract_models import RegexInFileCheck as RegexInFileCheck
from .contract_models import RegexInFileKind as RegexInFileKind
from .contract_models import VerificationCheck as VerificationCheck
from .contract_models import VerifyRecipe as VerifyRecipe
from .contract_parsing import _parse_verify_payload as _parse_verify_payload
from .contract_parsing import (
    baseline_check_budget_seconds as baseline_check_budget_seconds,
)
from .contract_parsing import parse_completion_contract as parse_completion_contract
from .contract_parsing import read_completion_contract as read_completion_contract
from .contract_parsing import recipe_total_budget_seconds as recipe_total_budget_seconds
from .literals import as_literal as as_literal
from .plans_errors import PlanError as PlanError
from .plans_files import canonicalize_path as canonicalize_path
from .plans_handoff import HANDOFF_HEADING as HANDOFF_HEADING
from .plans_registry import read_repository_registry as read_repository_registry
from .plans_session_ids import SessionId as SessionId
from .plans_session_ids import parse_session_id as parse_session_id
from .plans_session_ids import session_id_to_filename as session_id_to_filename
from .workspaces import RepositoryRegistry as RepositoryRegistry
from .workspaces import WorkspaceError as WorkspaceError
from .workspaces import validate_repository_alias as validate_repository_alias
