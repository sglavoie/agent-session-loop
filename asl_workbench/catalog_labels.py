"""Cache-safe catalog identity, labels, and summary partitioning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence

from .catalog_vocabulary import (
    _PAUSE_REASON_RE,
    _SESSION_ID_RE,
    _TIMESTAMP_RE,
    _TITLE_RE,
    CACHE_SAFE_FIELDS,
    COUNT_KEYS,
    DISCOVERY_SOURCES,
    KEY_CHARS,
    MAX_LABEL_CHARS,
    MAX_PROJECTS_PER_PLAN,
    PRIVATE_FIELDS,
    UNKNOWN_PROJECT,
    UNNAMED_PLAN,
    CatalogError,
)

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
