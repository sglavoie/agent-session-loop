"""Materialize validated structured review findings into repair sessions.

Session 16 made review findings machine-readable (``final-review.json``,
schema :data:`agent_session_loop.review.REVIEW_ARTIFACT_VERSION`). This module
turns those validated findings into *serial child repair sessions* appended
beneath the generated review session ``N`` as ``N.1``, ``N.2``, ... inside the
**same** plan directory — no second plan directory is ever created.

Planning is read-only, and materialization is deterministic and atomic:

- :func:`plan_review_repair_sessions` resolves the source review, validates the
  v2 artifacts, records full artifact digests and stable finding fingerprints,
  detects existing-child conflicts, and renders every proposed child in memory.
- Findings are grouped by their ``repair_group`` (every finding belongs to
  exactly one group; grouping preserves first-appearance order). At most
  :data:`MAX_REPAIR_GROUPS` groups are allowed per cycle — larger finding sets
  must be grouped by subsystem upstream, so an over-large set fails closed with
  the stable ``malformed-review-repair`` reason.
- Each group becomes one repair child whose body renders lineage, the finding
  references (evidence + explanation), the child scope, and the merged done
  criteria, and whose ``## Completion Contract`` embeds the findings'
  ``verification_checks`` verbatim (deduped by name and by identical command
  execution).
- The child scope states the same verification ownership the task packet does:
  the contract must end up passing, but ASL runs it authoritatively after the
  turn, so the child does not rerun it as its own final gate. Repeating or
  widening a check is justified by an invalidated result, a failure that needs
  diagnosis, or an uncovered correctness concern — never by habit — and a
  failure the supplied evidence already attributes to another repair group is
  that group's to fix unless the child observes something contradicting it.
- A group whose evidence or verification checks point the child at a session
  artifact in this plan directory other than its own is refused outright: the
  plan artifact guard makes exactly one session file writable per turn, so such
  a child is launched against work it is forbidden to do (ASI-134).
- Every child carries the versioned review-repair marker so later cycles can
  recognize generated repair sessions.
- Before anything is staged, the rendered children are judged against the bound
  repository root *and* against the plan's own verification context: a child
  whose repair targets reach an unregistered repository, or whose verification
  checks name a ``repo``/``cwd`` the contract cannot resolve, is refused while
  the plan is still untouched rather than committed and then rejected by the
  post-authoring gate.
- All child files, the manifest table/count refresh, and any predecessor
  reference repoint are staged in a single
  :class:`~agent_session_loop.authoring_transaction.PlanTransaction` and committed
  atomically, so a failed materialization leaves no partially authored plan.
- Every durable write happens inside a publication window
  (:data:`REPAIR_PUBLICATION_DIR_NAME`) whose rollback image is fsynced before
  the first destination byte moves. A publication that cannot finish — because
  the executability gate refused the children, or because the process died —
  is undone rather than left behind, so the plan is always either the one the
  publication started from or a fully loop-ready one with the children in it.
- After authoring, readiness is refreshed to ``draft``. When a repository
  ``cwd`` is supplied, targeted baselines plus ``asl preflight`` run and, if
  clean, mark the plan loop-ready so the new children become executable. A
  blocked gate withdraws the children through the publication window and
  reports its blockers, because a pending child no run may select is worse
  than no child at all (ASI-136).

The canonical review-finding grammar lives in
:mod:`agent_session_loop.review`; contract construction/validation and the
atomic transaction live in :mod:`agent_session_loop.authoring_transaction`."""

from .review_repair_materialization import (
    _malformed,
    _materialize,
    materialize_review_repair_sessions,
)
from .review_repair_models import (
    _REPAIR_GROUP_LINE_RE,
    _REVIEW_REPAIR_SESSION_MARKER_RE,
    _WRITE_REPOSITORY_SOURCE_LABELS,
    _WRITE_REPOSITORY_SOURCE_RANK,
    GENERATED_REVIEW_REPAIR_SESSION_VERSION,
    MAX_REPAIR_GROUPS,
    REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION,
    REVIEW_REPAIR_REASON_MALFORMED,
    REVIEW_REPAIR_REASON_NO_FINDINGS,
    REVIEW_REPAIR_REASON_NOT_LOOP_READY,
    REVIEW_REPAIR_REASON_OK,
    REVIEW_REPAIR_SESSION_MARKER,
    REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE,
    WRITE_REPOSITORY_SOURCE_CHECK,
    WRITE_REPOSITORY_SOURCE_EVIDENCE,
    WRITE_REPOSITORY_SOURCE_REPO,
    RepairChild,
    RepairGroupPlan,
    ReviewRepairError,
    ReviewRepairPlan,
    ReviewRepairResult,
    contains_review_repair_session_marker,
    is_generated_review_repair_session_file,
    review_repair_session_marker_version,
    review_repair_session_version_marker,
)
from .review_repair_planning import (
    _artifact_sha256_or_none,
    _build_repair_group_plans,
    _existing_children,
    _immediate_successor,
    _invalid_plan,
    find_generated_review_session,
    plan_review_repair_sessions,
)
from .review_repair_publication import (
    PUBLICATION_PHASE_AUTHORING,
    PUBLICATION_PHASE_PUBLISHING,
    PUBLICATION_PHASE_VALIDATING,
    REPAIR_PUBLICATION_DIR_NAME,
    REPAIR_PUBLICATION_JOURNAL_NAME,
    REPAIR_PUBLICATION_SCHEMA_VERSION,
    _advance_publication_phase,
    _close_publication_window,
    _open_publication_window,
    _PublicationWindow,
    _read_bytes_or_none,
    _restore_publication_image,
    _rollback_publication_window,
    _write_publication_journal,
    recover_review_repair_publication,
)
from .review_repair_render import (
    _merge_verification_checks,
    _render_child_body,
)
from .review_repair_scope import (
    _dedupe_preserving_order,
    _foreign_plan_artifacts_in,
    _plan_artifact_pattern,
    _render_write_repository_sources,
    foreign_plan_artifact_references,
    group_findings_by_repair_group,
    repair_group_from_session_text,
    repair_group_write_repository_sources,
    required_write_repositories,
)
from .review_repair_validation import (
    _REPOSITORY_REVIEW_BASE_HEADING,
    _REPOSITORY_REVIEW_BASE_RE,
    _child_footprint_targets,
    _make_children_executable,
    _read_repository_review_bases,
    _run_targeted_baselines,
    _strip_self_lock_findings,
    _validate_children_before_authoring,
    _validate_children_contract,
    _validate_children_footprint,
)

__all__ = [
    "GENERATED_REVIEW_REPAIR_SESSION_VERSION",
    "MAX_REPAIR_GROUPS",
    "PUBLICATION_PHASE_AUTHORING",
    "PUBLICATION_PHASE_PUBLISHING",
    "PUBLICATION_PHASE_VALIDATING",
    "REPAIR_PUBLICATION_DIR_NAME",
    "REPAIR_PUBLICATION_JOURNAL_NAME",
    "REPAIR_PUBLICATION_SCHEMA_VERSION",
    "REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION",
    "REVIEW_REPAIR_REASON_MALFORMED",
    "REVIEW_REPAIR_REASON_NOT_LOOP_READY",
    "REVIEW_REPAIR_REASON_NO_FINDINGS",
    "REVIEW_REPAIR_REASON_OK",
    "REVIEW_REPAIR_SESSION_MARKER",
    "REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE",
    "RepairChild",
    "RepairGroupPlan",
    "ReviewRepairError",
    "ReviewRepairPlan",
    "ReviewRepairResult",
    "WRITE_REPOSITORY_SOURCE_CHECK",
    "WRITE_REPOSITORY_SOURCE_EVIDENCE",
    "WRITE_REPOSITORY_SOURCE_REPO",
    "_PublicationWindow",
    "_REPAIR_GROUP_LINE_RE",
    "_REPOSITORY_REVIEW_BASE_HEADING",
    "_REPOSITORY_REVIEW_BASE_RE",
    "_REVIEW_REPAIR_SESSION_MARKER_RE",
    "_WRITE_REPOSITORY_SOURCE_LABELS",
    "_WRITE_REPOSITORY_SOURCE_RANK",
    "_advance_publication_phase",
    "_artifact_sha256_or_none",
    "_build_repair_group_plans",
    "_child_footprint_targets",
    "_close_publication_window",
    "_dedupe_preserving_order",
    "_existing_children",
    "_foreign_plan_artifacts_in",
    "_immediate_successor",
    "_invalid_plan",
    "_make_children_executable",
    "_malformed",
    "_materialize",
    "_merge_verification_checks",
    "_open_publication_window",
    "_plan_artifact_pattern",
    "_read_bytes_or_none",
    "_read_repository_review_bases",
    "_render_child_body",
    "_render_write_repository_sources",
    "_restore_publication_image",
    "_rollback_publication_window",
    "_run_targeted_baselines",
    "_strip_self_lock_findings",
    "_validate_children_before_authoring",
    "_validate_children_contract",
    "_validate_children_footprint",
    "_write_publication_journal",
    "contains_review_repair_session_marker",
    "find_generated_review_session",
    "foreign_plan_artifact_references",
    "group_findings_by_repair_group",
    "is_generated_review_repair_session_file",
    "materialize_review_repair_sessions",
    "plan_review_repair_sessions",
    "recover_review_repair_publication",
    "repair_group_from_session_text",
    "repair_group_write_repository_sources",
    "required_write_repositories",
    "review_repair_session_marker_version",
    "review_repair_session_version_marker",
]
