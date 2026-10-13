"""The canonical plan catalog: one enumeration, split by what may be cached.

Three separate things in the two applications answer the question "what plans
exist": Workbench discovery plus its summary builder, the Companion's
``ProjectCatalog``, and the bridge's ``runs.snapshot`` projection. Only the
first is complete, only the second carries titles and project membership, and
only the third is shaped to cross a network. This module is the one enumeration
the unified application reads, and it is here rather than in
``agent_session_loop`` because the Pi has to be able to hold it.

Four decisions make that possible, and each is data in this module rather than
a convention a reviewer has to remember:

* **The cache-safe / private split is a total partition of the summary.**
  :data:`CACHE_SAFE_FIELDS` and :data:`PRIVATE_FIELDS` name every key a
  ``WorkbenchSummary`` payload carries, and
  :func:`unclassified_summary_fields` reports any key on neither list. A field
  added to the summary therefore fails a test until somebody decides whether it
  may be persisted, which is the opposite of the default that would let a new
  prose field reach the Pi by accident. ``searchable_text`` is the field that
  motivated the split: it concatenates the plan path, the pin label, the pause
  message, and the last-message preview, so it is useful locally and
  unpublishable. :attr:`CatalogEntry.search_text` is its cache-safe
  replacement, built from classified fields only.

* **Identity is a digest, not a path.** :func:`plan_key` and :func:`run_key`
  reproduce the Companion's ``identity`` module exactly -- same JSON encoding,
  same SHA-256, same sixteen characters -- so migrating to this catalog keeps
  every plan and run identity the Pi has already stored. A plan directory names
  a user account and a filesystem layout; the digest is what travels.

* **The catalog is uncapped and delivered in pages.** The bridge's projection
  bounds itself to ``MAX_ROWS`` rows of active and actionable work, which is
  right for an intervention frame and wrong for a catalog: it drops every
  healthy ``ready`` and ``complete`` plan. Nothing here truncates. A bounded
  transport calls :meth:`PlanCatalog.page` instead, and every page carries the
  generation digest it was cut from.

* **An incomplete generation says so.** When the source cannot project a
  discovered plan -- it vanished mid-scan, or its files stopped parsing --
  the catalog is built with ``complete=False``. A consumer must not read a plan
  missing from an incomplete generation as a plan that was deleted, so the flag
  travels on every page rather than being inferred from a warning list.

Nothing in this module touches the filesystem. It is given summary payloads and
manifest text by a provider (``agent_session_loop.workbench_catalog`` on the
Mac) and returns records, which is what keeps the catalog loadable on a host
that has no plans of its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

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

#: Sixteen hex characters, the width ASL's ``bridge.row_id`` and the
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


# -- Identity ---------------------------------------------------------------


def digest_of(material: Sequence[object]) -> str:
    """Digest a list of components without inventing a delimiter.

    JSON encoding is the delimiter, so a component containing a comma, a quote,
    or a path separator cannot shift the boundary between components. Byte-for-
    byte the Companion's ``identity.digest_of``; the cross-repository test in
    ``tests/test_workbench_catalog.py`` holds them equal.
    """
    encoded = json.dumps(list(material), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:KEY_CHARS]


def plan_key(plan_dir: object) -> str:
    """The stable, opaque identity of one plan directory."""
    if not isinstance(plan_dir, str) or not plan_dir.strip():
        raise CatalogError("a plan key needs a plan directory")
    return digest_of([plan_dir])


def run_key(plan: str, run_id: object) -> str:
    """The stable, opaque identity of one run of one plan.

    ``plan`` is a plan key rather than a plan directory, so no identifier the
    catalog publishes is a digest of a Mac path that could be confirmed by
    guessing the path. A plan with no run yet still gets a key: ``None`` is a
    distinct component from the string ``"None"``.
    """
    if not isinstance(plan, str) or not plan:
        raise CatalogError("a run key needs a plan key")
    identifier = run_id if isinstance(run_id, str) and run_id else None
    return digest_of([plan, identifier])


# -- Sanitisers -------------------------------------------------------------


def safe_label(value: object, *, limit: int = MAX_LABEL_CHARS) -> str | None:
    """Reduce an arbitrary string to something safe to store and to render.

    The last path component is kept because the leading components are the part
    that leaks a filesystem layout; control characters become spaces because a
    dashboard that renders a newline as a newline renders a crafted label as
    two rows; and the result is bounded.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if "/" in text:
        components = [part for part in text.split("/") if part]
        if not components:
            return None
        text = components[-1]
    flattened = "".join(
        " " if character < " " or character == "\x7f" else character
        for character in text
    )
    collapsed = " ".join(flattened.split())
    if not collapsed:
        return None
    return collapsed[:limit]


def manifest_title(manifest_text: object) -> str | None:
    """The plan title from a manifest's first ``# H1``, or ``None``.

    ``None`` rather than the directory name, which is what ASL's
    ``plans_snapshot.inspect_plan`` falls back to: that fallback is a path component,
    and a catalog field that is usually a title and occasionally a directory
    name is not a field that can be published.
    """
    if not isinstance(manifest_text, str):
        return None
    for line in manifest_text.splitlines():
        match = _TITLE_RE.match(line)
        if match:
            return safe_label(match.group(1).replace("/", " "))
    return None


def display_label(
    *,
    title: object = None,
    label: object = None,
    workspace_root: object = None,
    plan_dir: object = None,
    repository_count: object = 0,
) -> str:
    """The name one plan is shown under, chosen from the safest thing available.

    The plan's own title first, because it is the name the plan gave itself and
    the only candidate that is not derived from a path or from whatever an
    operator typed. Then the Companion's existing order: the pin label, the
    workspace root, the plan directory. Each candidate is sanitised before it
    is considered, so an unusable one falls through rather than producing an
    unusable name.

    A multi-repository plan says so. Its workspace root is a container holding
    several repositories, and a name that looks like a single repository would
    make a cross-repository run indistinguishable from a run in the one
    repository that happens to share the container's name. With ``title=None``
    this agrees with the Companion's ``identity.project_label`` exactly.
    """
    count = repository_count if isinstance(repository_count, int) else 0
    if isinstance(repository_count, bool):
        count = 0
    suffix = f" ({count} repositories)" if count > 1 else ""
    room = MAX_LABEL_CHARS - len(suffix)
    for candidate in (title, label, workspace_root, plan_dir):
        name = safe_label(candidate, limit=room)
        if name is not None:
            return f"{name}{suffix}"
    return f"{UNNAMED_PLAN[:room]}{suffix}"


def origin_name(origin: object) -> str | None:
    """The repository name in a remote URL, without the URL."""
    if not isinstance(origin, str):
        return None
    name = safe_label(origin.rstrip("/").rsplit(":", 1)[-1])
    return name.removesuffix(".git") if name else None


def project_names(
    repositories: Iterable[object],
    *,
    workspace_root: object = None,
) -> tuple[str, ...]:
    """The repository names one plan works in, sorted and bounded.

    Derived from the origin URL, then the registered alias, then the registered
    relative path -- never from ``resolved_root``, which is the absolute Mac
    path and the reason ``repositories`` is a private field. A plan with no
    registered repositories is named by its workspace root's last component,
    which is the single-repository case every legacy plan is in.
    """
    names: set[str] = set()
    for repository in repositories:
        if isinstance(repository, Mapping):
            name = (
                origin_name(repository.get("origin"))
                or safe_label(repository.get("alias"))
                or safe_label(repository.get("path"))
            )
        else:
            name = safe_label(repository)
        if name and name != ".":
            names.add(name)
    if not names:
        fallback = safe_label(workspace_root)
        if fallback and fallback != ".":
            names.add(fallback)
    if not names:
        return (UNKNOWN_PROJECT,)
    return tuple(sorted(names)[:MAX_PROJECTS_PER_PLAN])


def group_label(names: Sequence[str]) -> str:
    """The one name a plan spanning several repositories is grouped under."""
    return " + ".join(names)[:MAX_LABEL_CHARS]


def _vocabulary_value(value: object, vocabulary: Sequence[str]) -> str | None:
    return value if isinstance(value, str) and value in vocabulary else None


def _slug(value: object) -> str | None:
    return value if isinstance(value, str) and _PAUSE_REASON_RE.match(value) else None


def _session_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _SESSION_ID_RE.match(text) else None


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        return None
    return value[:64]


def _counts(value: object) -> dict[str, int]:
    source = value if isinstance(value, Mapping) else {}
    counts: dict[str, int] = {}
    for key in COUNT_KEYS:
        raw = source.get(key)
        counts[key] = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    return counts


def _sources(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    order = {source: index for index, source in enumerate(DISCOVERY_SOURCES)}
    found = {item for item in value if isinstance(item, str) and item in order}
    return tuple(sorted(found, key=lambda item: order[item]))


# -- Field partition --------------------------------------------------------


def unclassified_summary_fields(keys: Iterable[str]) -> tuple[str, ...]:
    """Summary keys that are neither cache-safe nor private, sorted.

    The check that keeps the partition total. A field added to
    ``WorkbenchSummary.to_payload`` shows up here until somebody classifies it,
    which is how a new prose field is stopped from reaching the Pi by default.
    """
    classified = set(CACHE_SAFE_FIELDS) | set(PRIVATE_FIELDS)
    return tuple(sorted({key for key in keys if key not in classified}))


def partition_summary(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Split a summary payload into its cache-safe and private halves.

    Refuses a payload carrying an unclassified field rather than guessing which
    half it belongs in: publishing it would be a privacy decision nobody made,
    and dropping it would be a data loss nobody noticed.
    """
    unclassified = unclassified_summary_fields(payload.keys())
    if unclassified:
        raise CatalogError(
            "summary payload carries unclassified fields: " + ", ".join(unclassified)
        )
    cache_safe = {key: payload[key] for key in CACHE_SAFE_FIELDS if key in payload}
    private = {key: payload[key] for key in PRIVATE_FIELDS if key in payload}
    return cache_safe, private


# -- Records ----------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """One plan as the remote provider may retain it.

    Every field is either a digest, a timestamp, a counter, or a member of a
    vocabulary declared in this module. There is no path, no operator-typed
    string, and no prose, so the whole entry may be persisted and served from a
    cache without asking the Mac.
    """

    plan_key: str
    run_key: str
    display_label: str
    primary_state: str
    recommended_action: str
    urgency_rank: int
    urgency_group: str
    activity_label: str
    projects: tuple[str, ...]
    project_group: str
    counts: dict[str, int]
    sources: tuple[str, ...]
    exists: bool
    pinned: bool
    title: str | None = None
    next_pending_session_id: str | None = None
    last_activity_at: str | None = None
    last_activity_source: str | None = None
    completed_at: str | None = None
    pause_reason: str | None = None
    harness: str | None = None
    registry_digest: str | None = None
    repository_count: int = 0
    warning_count: int = 0

    @property
    def multi_repository(self) -> bool:
        """Whether the plan is bound to more than one registered repository."""
        return self.repository_count > 1

    @property
    def search_text(self) -> str:
        """Search text built from cache-safe fields only.

        The Workbench's own ``searchable_text`` concatenates the plan path, the
        pin label, the pause message, and the last-message preview, which is
        why it is a private field. This is what the Pi searches instead: the
        title, the project names, and the classification.
        """
        parts: list[str] = [
            self.display_label,
            self.primary_state,
            self.recommended_action,
            self.urgency_group,
            self.activity_label,
            *self.projects,
            *self.sources,
        ]
        if self.title:
            parts.append(self.title)
        if self.next_pending_session_id:
            parts.extend(["session", self.next_pending_session_id])
        for optional in (self.pause_reason, self.harness):
            if optional:
                parts.append(optional)
        joined = " ".join(part for part in parts if part)
        return " ".join(joined.lower().split())

    def to_payload(self) -> dict[str, object]:
        return {
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "title": self.title,
            "display_label": self.display_label,
            "primary_state": self.primary_state,
            "recommended_action": self.recommended_action,
            "urgency_rank": self.urgency_rank,
            "urgency_group": self.urgency_group,
            "activity_label": self.activity_label,
            "projects": list(self.projects),
            "project_group": self.project_group,
            "counts": dict(self.counts),
            "sources": list(self.sources),
            "exists": self.exists,
            "pinned": self.pinned,
            "next_pending_session_id": self.next_pending_session_id,
            "last_activity_at": self.last_activity_at,
            "last_activity_source": self.last_activity_source,
            "completed_at": self.completed_at,
            "pause_reason": self.pause_reason,
            "harness": self.harness,
            "registry_digest": self.registry_digest,
            "repository_count": self.repository_count,
            "multi_repository": self.multi_repository,
            "warning_count": self.warning_count,
            "search_text": self.search_text,
        }


@dataclass(frozen=True)
class PlanDetail:
    """The private half of one plan, held for a local or on-demand read.

    A mapping rather than a second field-by-field mirror of the summary: the
    partition in :data:`PRIVATE_FIELDS` is the schema, and restating it here
    would give it two places to drift. What this type adds is the rule --
    detail is fetched, never published -- and the plan key that ties it back to
    an entry the Pi may already hold.
    """

    plan_key: str
    fields: dict[str, object]

    def get(self, name: str) -> object:
        """One private field, or ``None`` when the summary did not carry it."""
        if name in CACHE_SAFE_FIELDS:
            raise CatalogError(f"{name} is a cache-safe field, not a detail field")
        if name not in PRIVATE_FIELDS:
            raise CatalogError(f"{name} is not a classified summary field")
        return self.fields.get(name)

    def to_payload(self) -> dict[str, object]:
        return {"plan_key": self.plan_key, "fields": dict(self.fields)}


@dataclass(frozen=True)
class CatalogRecord:
    """One plan, both halves, paired by plan key."""

    entry: CatalogEntry
    detail: PlanDetail

    @property
    def plan_key(self) -> str:
        return self.entry.plan_key


def record_from_summary(
    payload: Mapping[str, object],
    *,
    title: object = None,
    pinned: bool = False,
) -> CatalogRecord:
    """Build one catalog record from a ``WorkbenchSummary`` payload.

    ``title`` comes from the plan's manifest, which the summary does not carry;
    pass :func:`manifest_title` of the manifest text. ``pinned`` says whether
    the registry holds a pin for the plan -- derivable from ``sources`` for a
    discovered plan, but not for a single-plan read, so the provider states it.
    """
    cache_safe, private = partition_summary(payload)
    plan_dir = private.get("plan_dir")
    if not isinstance(plan_dir, str) or not plan_dir.strip():
        raise CatalogError("summary payload carries no plan_dir")
    key = plan_key(plan_dir)
    primary_state = _vocabulary_value(cache_safe.get("primary_state"), PRIMARY_STATES)
    if primary_state is None:
        raise CatalogError(f"summary payload has no known primary_state: {plan_dir}")
    repository_count = cache_safe.get("repository_count")
    count = (
        repository_count
        if isinstance(repository_count, int) and not isinstance(repository_count, bool)
        else 0
    )
    urgency_rank = cache_safe.get("urgency_rank")
    # A payload arrives from JSON or from a dataclass, so `repositories` is
    # whatever the caller had; anything that is not a list of rows names no
    # project rather than being iterated a character at a time.
    raw_repositories = private.get("repositories")
    repositories: tuple[object, ...] = (
        tuple(raw_repositories)
        if isinstance(raw_repositories, Sequence)
        and not isinstance(raw_repositories, str | bytes)
        else ()
    )
    projects = project_names(
        repositories,
        workspace_root=private.get("workspace_root") or private.get("cwd"),
    )
    resolved_title = safe_label(title)
    warnings = private.get("warnings")
    entry = CatalogEntry(
        plan_key=key,
        run_key=run_key(key, private.get("run_id")),
        display_label=display_label(
            title=resolved_title,
            label=private.get("label"),
            workspace_root=private.get("workspace_root") or private.get("cwd"),
            plan_dir=plan_dir,
            repository_count=count,
        ),
        primary_state=primary_state,
        recommended_action=(
            _vocabulary_value(cache_safe.get("recommended_action"), RECOMMENDED_ACTIONS)
            or "view"
        ),
        urgency_rank=(
            urgency_rank
            if isinstance(urgency_rank, int) and not isinstance(urgency_rank, bool)
            else 0
        ),
        urgency_group=(
            _vocabulary_value(cache_safe.get("urgency_group"), URGENCY_GROUPS)
            or "needs-action"
        ),
        activity_label=(
            _vocabulary_value(cache_safe.get("activity_label"), ACTIVITY_LABELS) or ""
        ),
        projects=projects,
        project_group=group_label(projects),
        counts=_counts(cache_safe.get("counts")),
        sources=_sources(cache_safe.get("sources")),
        exists=bool(cache_safe.get("exists")),
        pinned=pinned or "registry" in _sources(cache_safe.get("sources")),
        title=resolved_title,
        next_pending_session_id=_session_id(cache_safe.get("next_pending_session_id")),
        last_activity_at=_timestamp(cache_safe.get("last_activity_at")),
        last_activity_source=_vocabulary_value(
            cache_safe.get("last_activity_source"), LAST_ACTIVITY_SOURCES
        ),
        completed_at=_timestamp(cache_safe.get("completed_at")),
        pause_reason=_slug(cache_safe.get("pause_reason")),
        harness=safe_label(cache_safe.get("harness")),
        registry_digest=_slug(cache_safe.get("registry_digest")),
        repository_count=count,
        warning_count=(
            len(warnings)
            if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes))
            else 0
        ),
    )
    return CatalogRecord(entry=entry, detail=PlanDetail(plan_key=key, fields=private))


# -- The catalog ------------------------------------------------------------


@dataclass(frozen=True)
class CatalogProject:
    """One repository and the plans that work in it."""

    name: str
    plan_keys: tuple[str, ...]
    state_counts: dict[str, int]

    @property
    def plan_count(self) -> int:
        return len(self.plan_keys)

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "plan_keys": list(self.plan_keys),
            "plan_count": self.plan_count,
            "state_counts": dict(self.state_counts),
        }


@dataclass(frozen=True)
class CatalogPage:
    """One batch of catalog entries, and where it sits in the generation.

    ``complete`` is the generation's property and ``final`` is the page's: a
    last page of a generation whose source could not read every plan is
    ``final`` and not ``complete``, and a consumer must not reconcile a
    disappearance against it.
    """

    entries: tuple[CatalogEntry, ...]
    offset: int
    limit: int | None
    total: int
    generation: str
    complete: bool
    final: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "entries": [entry.to_payload() for entry in self.entries],
            "offset": self.offset,
            "limit": self.limit,
            "count": len(self.entries),
            "total": self.total,
            "generation": self.generation,
            "complete": self.complete,
            "final": self.final,
        }


@dataclass(frozen=True)
class PlanCatalog:
    """Every plan the source found, in discovery order, with nothing dropped.

    The contrast with the bridge's ``runs.snapshot`` is the point of the type:
    that projection keeps only active and actionable states and stops at
    ``MAX_ROWS`` rows, so a corpus with a few hundred plans loses every healthy
    ``ready`` and ``complete`` one. This holds all of them and pages on
    request.
    """

    records: tuple[CatalogRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    complete: bool = True

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def generation(self) -> str:
        """A digest of every entry in the catalog, in order.

        Wall-clock free, so an unchanged corpus produces one generation
        forever, and a page can name the generation it was cut from without
        the caller having to trust that two pages came from one read.
        """
        material = [record.entry.to_payload() for record in self.records]
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(record.entry for record in self.records)

    def record(self, key: str) -> CatalogRecord:
        for record in self.records:
            if record.plan_key == key:
                return record
        raise CatalogError(f"no plan with key {key!r} in this catalog")

    def detail(self, key: str) -> PlanDetail:
        return self.record(key).detail

    def page(self, *, offset: int = 0, limit: int | None = None) -> CatalogPage:
        """One batch of entries, starting at ``offset``.

        ``limit=None`` returns everything from ``offset`` on, which is what a
        local caller wants; a transport passes its frame budget instead. An
        offset past the end is an empty final page rather than an error, so a
        client that polls a shrinking catalog converges instead of failing.
        """
        if offset < 0:
            raise CatalogError("a catalog page cannot start before the first plan")
        if limit is not None and limit <= 0:
            raise CatalogError("a catalog page limit must be positive")
        selected = (
            self.records[offset:] if limit is None else self.records[offset:][:limit]
        )
        entries = tuple(record.entry for record in selected)
        return CatalogPage(
            entries=entries,
            offset=offset,
            limit=limit,
            total=self.total,
            generation=self.generation,
            complete=self.complete,
            final=offset + len(entries) >= self.total,
        )

    def pages(self, *, limit: int = DEFAULT_PAGE_LIMIT) -> tuple[CatalogPage, ...]:
        """Every page of the catalog at ``limit`` entries each.

        An empty catalog is one empty page rather than none: a generation that
        found nothing still has to be delivered, or the consumer cannot tell it
        apart from a delivery that never happened.
        """
        if limit <= 0:
            raise CatalogError("a catalog page limit must be positive")
        offsets = range(0, self.total, limit) if self.total else (0,)
        return tuple(self.page(offset=offset, limit=limit) for offset in offsets)

    def state_counts(self) -> dict[str, int]:
        """How many plans sit in each primary state, every state present."""
        counts = dict.fromkeys(PRIMARY_STATES, 0)
        for record in self.records:
            counts[record.entry.primary_state] += 1
        return counts

    def projects(self) -> tuple[CatalogProject, ...]:
        """Project membership across the catalog, one row per repository name.

        A plan bound to several repositories is a member of each, which is what
        makes this a membership map rather than a partition: the Companion's
        overview asks "what is happening in this repository", and a
        cross-repository run is happening in all of them.
        """
        keys_by_name: dict[str, list[str]] = {}
        states_by_name: dict[str, dict[str, int]] = {}
        for record in self.records:
            for name in record.entry.projects:
                keys_by_name.setdefault(name, []).append(record.plan_key)
                states = states_by_name.setdefault(
                    name, dict.fromkeys(PRIMARY_STATES, 0)
                )
                states[record.entry.primary_state] += 1
        return tuple(
            CatalogProject(
                name=name,
                plan_keys=tuple(keys_by_name[name]),
                state_counts=states_by_name[name],
            )
            for name in sorted(keys_by_name)
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "total": self.total,
            "generation": self.generation,
            "complete": self.complete,
            "warnings": list(self.warnings),
            "entries": [entry.to_payload() for entry in self.entries()],
            "projects": [project.to_payload() for project in self.projects()],
            "state_counts": self.state_counts(),
        }


def build_catalog(
    records: Iterable[CatalogRecord],
    *,
    warnings: Iterable[str] = (),
    complete: bool = True,
) -> PlanCatalog:
    """Assemble a catalog, refusing two records for the same plan.

    A duplicate key would make :meth:`PlanCatalog.record` answer arbitrarily
    and would double-count a plan in every project row, so it is a build-time
    refusal rather than a warning nobody reads.
    """
    collected = tuple(records)
    seen: set[str] = set()
    for record in collected:
        if record.plan_key in seen:
            raise CatalogError(f"duplicate plan key in catalog: {record.plan_key}")
        seen.add(record.plan_key)
    return PlanCatalog(
        records=collected,
        warnings=tuple(warnings),
        complete=complete,
    )
