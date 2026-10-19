"""Catalog vocabularies, privacy classifications, and bounds."""

from __future__ import annotations

import re

# -- Vocabularies -----------------------------------------------------------
#
# Mirrors of ASL's classification vocabularies. They are copied rather than
# imported because this package may not import `agent_session_loop`, and
# `tests/test_workbench_catalog.py` compares each one against the live ASL
# constant in both directions -- so a state added there fails here instead of
# arriving on the Pi as an unrecognised string.

PRIMARY_STATES: tuple[str, ...] = (
    "invalid",
    "verifying",
    "running",
    "paused-for-reply",
    "failed",
    "stale",
    "complete",
    "recent",
    "ready",
)


RECOMMENDED_ACTIONS: tuple[str, ...] = (
    "watch",
    "reply",
    "inspect-failure",
    "unlock",
    "view",
    "run",
    "repair-plan",
)


URGENCY_GROUPS: tuple[str, ...] = (
    "needs-action",
    "in-progress",
    "ready",
    "complete",
)


LAST_ACTIVITY_SOURCES: tuple[str, ...] = (
    "active-verification",
    "active-turn",
    "turn-record",
    "run-finished",
    "run-started",
    "lock-created",
    "progress-journal",
)


DISCOVERY_SOURCES: tuple[str, ...] = (
    "state",
    "registry",
    "plan-root",
    "agent-plans-dir",
    "tmp-default",
    "share-default",
)


#: Every activity label a summary can carry. A fixed vocabulary is what makes
#: the field cache-safe: unlike ``concise_reason`` and ``next_action_hint``,
#: which interpolate pause messages, plan paths, and recovery prose, this one
#: is chosen from a table.
ACTIVITY_LABELS: tuple[str, ...] = (
    "Plan unreadable",
    "Verifying",
    "Active turn",
    "Reply needed",
    "Last run failed",
    "Stale state",
    "All sessions complete",
    "Run finished, sessions remain",
    "Ready to run",
    "Review continuation ready",
)


#: The session-status counters a summary reports, in the order the queue shows
#: them. Named so a page can carry a total per bucket without a bare dict of
#: whatever keys happened to be present.
COUNT_KEYS: tuple[str, ...] = ("pending", "complete", "empty", "unknown")


# -- The cache-safe / private partition -------------------------------------

#: Summary fields the remote provider may retain. Each is either a member of a
#: fixed vocabulary above, a timestamp, a counter, or a digest. None of them
#: carries a filesystem path, an operator-typed string, or model prose.
CACHE_SAFE_FIELDS: tuple[str, ...] = (
    "activity_label",
    "completed_at",
    "counts",
    "exists",
    "harness",
    "last_activity_at",
    "last_activity_source",
    "next_pending_session_id",
    "pause_reason",
    "primary_state",
    "recommended_action",
    "registry_digest",
    "repository_count",
    "sources",
    "urgency_group",
    "urgency_rank",
)


#: Summary fields that stay on the Mac and are fetched on demand. Three reasons
#: appear here, and the distinction matters when a later session decides what a
#: detail read returns:
#:
#: * paths (``plan_dir``, every ``*_path``, ``artifact_dir``, ``cwd``,
#:   ``workspace_root``, ``repositories[].resolved_root``) name a filesystem
#:   layout;
#: * prose (``detail``, ``concise_reason``, ``next_action_hint``,
#:   ``pause_message``, ``last_message_preview``, ``recovery``,
#:   ``review_resume``, ``assurance``, ``warnings``) is written by an operator
#:   or a model and is unbounded in content;
#: * ``label`` and ``profile`` are what an operator typed into a registry pin,
#:   and ``run_id`` is a Mac-side identifier that :func:`run_key` replaces.
PRIVATE_FIELDS: tuple[str, ...] = (
    "artifact_dir",
    "assurance",
    "concise_reason",
    "cwd",
    "detail",
    "events_path",
    "final_review_path",
    "label",
    "last_message_path",
    "last_message_preview",
    "next_action_hint",
    "next_pending_session_path",
    "pause_message",
    "plan_dir",
    "profile",
    "progress_path",
    "recovery",
    "repositories",
    "result_path",
    "review_resume",
    "run_id",
    "searchable_text",
    "state_path",
    "stderr_path",
    "stdout_path",
    "warnings",
    "workspace_root",
)


# -- Bounds -----------------------------------------------------------------

#: Sixteen hex characters, the width ASL's ``bridge_projection.row_id`` and the
#: Companion's ``identity`` module both use.
KEY_CHARS = 16


#: Label bound, matching the Companion's ``protocol.MAX_LABEL_CHARS``. A label
#: is a name, not a story.
MAX_LABEL_CHARS = 120


#: Project names retained per plan, matching the Companion's
#: ``MAX_PROJECTS_PER_PLAN``. A workspace with more repositories than this is
#: named by the first names in order rather than refused.
MAX_PROJECTS_PER_PLAN = 32


#: The batch size a paginating transport gets when it does not name one. Not a
#: cap on the catalog: :class:`PlanCatalog` holds every plan the source found,
#: and a caller that wants all of them asks for ``limit=None``.
DEFAULT_PAGE_LIMIT = 100


#: What a plan is shown as when nothing nameable survived sanitisation, and
#: what a project is called when no repository could be named. Both match the
#: Companion's constants so a migrated cache reads the same.
UNNAMED_PLAN = "unnamed plan"


UNKNOWN_PROJECT = "Unknown project"


#: A pause reason is a slug the runner wrote, not free text. A value that does
#: not look like one is dropped rather than published, because the field is
#: cache-safe only for as long as it holds identifiers.
_PAUSE_REASON_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


#: A session id is a canonical dotted string of positive integers, matching the
#: Companion's ``SESSION_ID_RE``. ``1.2`` and ``1.20`` are different sessions,
#: so the string is retained rather than coerced through a number.
_SESSION_ID_RE = re.compile(r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")


#: The first Markdown ``# H1`` in a manifest is the plan's title.
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")


#: An ISO-8601-ish timestamp as ASL writes them. Validated by shape rather than
#: parsed: the catalog passes timestamps through and does not do arithmetic on
#: them, and a value that is not a timestamp at all should not be published.
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}")


class CatalogError(Exception):
    """Raised when the catalog is asked for something it does not carry."""
