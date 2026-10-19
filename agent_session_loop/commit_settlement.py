# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Public facade for commit ledger, target resolution, and final settlement."""

from . import commit_settlement_finish as _finish
from . import commit_settlement_groups as _groups
from . import commit_settlement_ledger as _ledger
from . import commit_settlement_paused as _paused
from . import commit_settlement_targets as _targets
from . import commit_settlement_types as _types
from .commit_settlement_finish import _finish_run
from .commit_settlement_groups import _run_commit_group
from .commit_settlement_ledger import (
    COMMIT_FAILURES_KEY,
    MAX_COMMIT_GROUP_RETRIES,
    PAUSED_WORK_COMMIT_GUARD,
    PAUSED_WORK_COMMIT_KEY,
    UNRESOLVED_COMMIT_GROUPS_KEY,
    _append_commit_failure,
    _commit_failure_record,
    _unresolved_commit_group_label,
    _unresolved_commit_groups,
    _upsert_commit_group,
)
from .commit_settlement_paused import (
    _paused_work_commit_is_owed,
    _paused_work_commit_targets,
    _recovered_commit_generation,
    _settle_paused_retained_work,
)
from .commit_settlement_targets import (
    _commit_target_head,
    _commit_target_is_clean,
    _commit_targets,
    _dirty_commit_targets,
    _registered_end_commit_targets,
    _registered_session_commit_targets,
    _resolve_recovered_commit_targets,
)
from .commit_settlement_types import (
    RetainedWorkContext,
    RunFinalizer,
    STAGEABLE_CHANGE_CATEGORIES,
)

for _module in (_types, _ledger, _targets, _groups, _paused, _finish):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


_run_commit_group_impl = _run_commit_group


def _run_commit_group(*args, **kwargs):
    """Run a group while preserving the facade's established patch seam."""
    original = _groups._persist_commit_group
    _groups._persist_commit_group = _persist_commit_group
    try:
        return _run_commit_group_impl(*args, **kwargs)
    finally:
        _groups._persist_commit_group = original
