"""What a turn wrote outside the checkouts, and whose authority it answered to.

The repository snapshot answers one question -- did a registered checkout
change -- and reports silence about everywhere else as cleanliness. That
silence is what let a session edit an external audit store and still pass as
workspace-clean (ASI-19): the state root is exempted from repository intent as
a plan artifact, so the write produced no ``changed`` entry, no topology row,
and nothing ``unavailable``.

This module judges *paths* rather than repositories. Every location a turn's
own artifacts and sign-off message name is resolved and classified against the
authorities this run can name -- the control plane, the registered
repositories, the guarded workspace tree, the exemptions the pre-turn intent
gate already honors, the platform temporary directories -- and whatever
survives all of them answers to nothing this run can name, which is what
:data:`EXTERNAL_WRITE_UNCLASSIFIED` means.

Durable records this boundary writes: none. Every function here reads --
``lstat``, ``scandir``, and bounded content hashes -- and returns a payload for
its caller to persist. The write-guard evidence blocks that carry these
verdicts into a turn's result file are written by their own owners; nothing
here opens a path for writing.

Nothing here imports the runner. The plan directory, the state-dir argument,
the run directory, the workspace root, the registered repository roots, the
pre-turn digests and the turn's start time all enter as parameters, and the
two collaborators this module needs -- ``run_inputs._runner_owned_roots`` and
``preflight_footprint.plan_provenance_exempt_roots`` -- are read at their owning
modules, so no seam is required.

Invariants that survive the move:

* **Fail closed, into the unavailable lane.** A path that fits no bucket
  becomes an ``unclassified`` row, which :func:`_external_write_unavailable`
  projects into the guard's ``unavailable`` lane rather than into
  ``violations``: an unclassified write is one the guard could not judge, not
  one it judged and rejected.
* **The control plane outranks a repository containing it.** A plan directory
  or state root may legitimately sit inside a checkout, and the runner's own
  bookkeeping is never the work under judgement, so
  :func:`_classify_external_path` matches it before anything else.
* **Ephemeral is matched last.** Under test the plan directory, the workspace,
  and every repository live inside ``TMPDIR``; an early ephemeral match would
  collapse the whole taxonomy into one bucket, which is equally true of an
  operator who keeps a checkout there.
* **Named is not written.** ``turn_started_ns`` bounds the unclassified lane to
  writes that actually landed during the turn, so a path merely mentioned, or
  one last written before the turn began, is classified and counted but never
  charged to the turn as a mutation. It is still named: those rows go to
  ``unclassified_unwritten`` rather than nowhere, because a nonzero
  ``classified.unclassified`` count beside an empty ``unclassified`` lane reads
  as a guard that observed something it then declined to say anything about
  (ASI-120).
* **A root the turn never named is still compared.** The whole point is that a
  control-plane write leaves no trace in the paths a turn declares, so every
  pre-turn digest is re-read and diffed even when no artifact mentions it.
* **Bounded cost on every turn.** Directories are digested by shape -- a
  sorted, capped listing of direct entries with their sizes and mtimes -- and
  files by content only up to :data:`EXTERNAL_WRITE_DIGEST_MAX_BYTES`, because
  the live state root measures in gigabytes and this pass runs on every turn.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    preflight_footprint,
    repository_intent,
    run_inputs,
    state,
    workspaces,
)

#: A file larger than this is digested by size and mtime rather than by
#: content. The cap keeps the guard's cost bounded on a turn that names a
#: build artifact or a transcript; the classification, not the digest, is what
#: fails the guard closed.
EXTERNAL_WRITE_DIGEST_MAX_BYTES = 8 * 1024 * 1024

#: How many direct entries a directory digest covers, and how many rows any
#: one classification bucket reports before it is truncated to a count.
EXTERNAL_WRITE_MANIFEST_MAX_ENTRIES = 512

#: The classification a path receives when it fits none of the known buckets.
#: Routed into the guard's existing ``unavailable`` lane rather than into
#: ``violations``: an unclassified write is one the guard could not judge, not
#: one it judged and rejected.
EXTERNAL_WRITE_UNCLASSIFIED = "unclassified"

#: Reported as the ``unavailable`` reason for each unclassified mutation.
EXTERNAL_WRITE_UNCLASSIFIED_REASON = "external-write-unclassified"

#: Reported for an unclassified path the turn named but did not write. It is
#: not an ``unavailable`` reason: these rows never reach the guard's
#: fail-closed lane, and exist so a nonzero ``classified.unclassified`` count
#: beside an empty ``unclassified`` lane is explained rather than contradictory
#: (ASI-120).
EXTERNAL_WRITE_UNCLASSIFIED_UNWRITTEN_REASON = "external-write-unclassified-unwritten"

#: Every bucket :func:`_classify_external_path` can return. Ordered from the
#: most specific authority to the least so the enumeration reads as the
#: precedence the classifier applies.
EXTERNAL_WRITE_CLASSES: tuple[str, ...] = (
    "control-plane",
    "workspace-repository",
    "workspace-unregistered-path",
    "unregistered-repository",
    "plan-provenance",
    "system-path",
    "authorized-external-data",
    "ephemeral-path",
    "unresolved",
    EXTERNAL_WRITE_UNCLASSIFIED,
)


def _ephemeral_roots() -> tuple[Path, ...]:
    """Canonical temporary directories, which are not workspace writes.

    A turn that names a scratch file has not mutated anything durable, and the
    platform temporary directory is where the plan directory itself lives under
    test. Resolving through symlinks matters on macOS, where ``/tmp`` and the
    per-user ``TMPDIR`` both live under ``/private``.

    ``/tmp`` and ``/var/tmp`` are listed explicitly because neither is implied
    by the other two:
    :func:`tempfile.gettempdir` honours ``TMPDIR``, which on macOS is the
    per-user ``/var/folders/.../T`` directory, so ``/tmp`` -- canonically
    ``/private/tmp`` -- would otherwise reach the classifier unclassified and
    fail the write guard closed the moment a turn named a scratch file there.
    The same applies to ``/var/tmp`` (``/private/var/tmp`` on macOS).
    """
    roots: dict[Path, None] = {}
    for value in (tempfile.gettempdir(), os.environ.get("TMPDIR"), "/tmp", "/var/tmp"):
        if not value:
            continue
        roots[workspaces.canonicalize_filesystem_path(value)] = None
    return tuple(roots)


def _external_path_digest(path: Path) -> dict[str, Any]:
    """Digest one external path cheaply enough to run on every turn.

    A control-plane root is a directory ASL appends to constantly and may hold
    gigabytes of run transcripts, so a recursive content hash is not on the
    table -- the live state root measures in gigabytes. A directory is
    therefore digested by its *shape*: the bounded, sorted listing of its
    direct entries with each entry's size and mtime, which is exactly what
    moves when the runner appends a record to a store inside it. A file is
    digested by content up to :data:`EXTERNAL_WRITE_DIGEST_MAX_BYTES`, and by
    size and mtime beyond it.

    The directory form is deliberately shallow. It is evidence that a control
    plane root was or was not disturbed, not a commitment about every byte
    beneath it; a mutation nested deeper is caught by digesting the individual
    path the turn named, which is what the post-turn pass adds.
    """
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return {"present": False}
    except OSError as error:
        return {"present": True, "unavailable": str(error)}
    record: dict[str, Any] = {
        "present": True,
        "mtime_ns": stat_result.st_mtime_ns,
        "size": stat_result.st_size,
    }
    if stat.S_ISDIR(stat_result.st_mode):
        record["kind"] = "directory"
        digest = hashlib.sha256()
        count = 0
        try:
            with os.scandir(path) as entries:
                rows = sorted(
                    (entry.name, entry)
                    for entry in entries  # noqa: C414
                )
            for name, entry in rows:
                count += 1
                if count > EXTERNAL_WRITE_MANIFEST_MAX_ENTRIES:
                    continue
                child = entry.stat(follow_symlinks=False)
                digest.update(
                    f"{name}\0{stat.S_ISDIR(child.st_mode)}\0"
                    f"{child.st_size}\0{child.st_mtime_ns}\0".encode()
                )
        except OSError as error:
            record["unavailable"] = str(error)
            return record
        record["entry_count"] = count
        record["truncated"] = count > EXTERNAL_WRITE_MANIFEST_MAX_ENTRIES
        record["sha256"] = digest.hexdigest()
        return record
    if not stat.S_ISREG(stat_result.st_mode):
        record["kind"] = "other"
        return record
    record["kind"] = "file"
    if stat_result.st_size > EXTERNAL_WRITE_DIGEST_MAX_BYTES:
        record["truncated"] = True
        return record
    try:
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        record["unavailable"] = str(error)
    return record


def _external_control_plane_paths(
    roots: Sequence[Path],
    *,
    shallow_roots: Sequence[Path] = (),
) -> tuple[Path, ...]:
    """The bounded path set a pre-turn control-plane digest covers.

    Each root plus its direct entries: the roots catch a store appearing or
    disappearing, and the direct entries catch an append to a store the runner
    keeps at the top level, which a directory digest of the root alone would
    miss because appending to a file moves neither the parent's mtime nor its
    entry names.

    A ``shallow_root`` contributes only itself. That is the shape a root whose
    direct entries are other plans' state directories needs: its own directory
    digest already carries every child's name, size, and mtime -- enough to see
    a top-level store appended to or a sibling plan appearing -- while
    expanding it would digest every unrelated plan's directory on every turn.
    """
    paths: dict[Path, None] = {}
    for root in shallow_roots:
        paths[workspaces.canonicalize_filesystem_path(root)] = None
    for root in roots:
        canonical = workspaces.canonicalize_filesystem_path(root)
        paths[canonical] = None
        try:
            with os.scandir(canonical) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            # An absent or unreadable root is still digested as a root; the
            # digest itself records why it could not be read.
            continue
        for name in names[:EXTERNAL_WRITE_MANIFEST_MAX_ENTRIES]:
            paths[canonical / name] = None
    return tuple(paths)


def _capture_external_write_digests(
    roots: Sequence[Path],
    *,
    shallow_roots: Sequence[Path] = (),
) -> dict[str, dict[str, Any]]:
    """Digest the control-plane path set, keyed by canonical path."""
    return {
        repository_intent.redact_credentials(str(path)): _external_path_digest(path)
        for path in _external_control_plane_paths(roots, shallow_roots=shallow_roots)
    }


def _classify_external_path(
    resolved: repository_intent.ResolvedCandidate,
    *,
    control_plane_roots: Sequence[Path],
    repository_roots: Sequence[Path],
    workspace_root: Path,
    ephemeral_roots: Sequence[Path],
    authorized_roots: Sequence[Path] = (),
) -> str:
    """Name the authority a path answers to, or report it unclassified.

    Precedence runs from the most specific authority outward. The control
    plane wins over everything because a plan directory or state root may
    legitimately sit inside a checkout, and the runner's own bookkeeping is
    never the work under judgement. A repository comes next, then the guarded
    workspace tree, then the exemptions the pre-turn intent gate already
    honors. Whatever survives all of that answers to nothing this run can
    name, which is what :data:`EXTERNAL_WRITE_UNCLASSIFIED` means.

    Ephemeral paths are matched late, not early: a temporary directory is only
    a temporary directory when nothing more specific claims the path. Under
    test the plan directory, the workspace, and every repository live inside
    ``TMPDIR``, and an early match would collapse the whole taxonomy into one
    bucket -- which is also true of an operator who keeps a checkout there.
    """
    path = resolved.resolved
    if path is None or resolved.exemption == "unresolved":
        return "unresolved"
    if any(
        workspaces.is_descendant(path, root, allow_equal=True)
        for root in control_plane_roots
    ):
        return "control-plane"
    if resolved.exemption == "system-path":
        return "system-path"
    if resolved.git_root is not None:
        if any(
            workspaces.is_descendant(resolved.git_root, root, allow_equal=True)
            for root in repository_roots
        ):
            return "workspace-repository"
        return "unregistered-repository"
    if resolved.exemption == "plan-provenance":
        return "plan-provenance"
    if workspaces.is_descendant(path, workspace_root, allow_equal=True):
        # Inside the guarded tree but in no repository: a scratch file beside
        # the checkouts. Recorded rather than fatal -- the registry decides
        # what may be written, and no registered repository was touched.
        return "workspace-unregistered-path"
    if any(
        workspaces.is_descendant(root, path, allow_equal=True)
        for root in (workspace_root, *repository_roots)
    ):
        # An ancestor of the workspace: `$HOME` itself when a repository is
        # registered beneath it. Naming a container is not writing into it.
        return "workspace-unregistered-path"
    if any(path.is_relative_to(root) for root in authorized_roots):
        return "authorized-external-data"
    if any(
        workspaces.is_descendant(path, root, allow_equal=True)
        for root in ephemeral_roots
    ):
        return "ephemeral-path"
    return EXTERNAL_WRITE_UNCLASSIFIED


def _external_write_evidence(
    *,
    artifact_paths: Sequence[Path],
    root: Path,
    workspace_root: Path,
    repository_roots: Sequence[Path],
    control_plane_roots: Sequence[Path],
    provenance_roots: Sequence[Path],
    digests_before: Mapping[str, Mapping[str, Any]],
    turn_started_ns: int | None,
    ephemeral_roots: Sequence[Path] | None = None,
    authorized_roots: Sequence[Path] = (),
    limit: int = EXTERNAL_WRITE_MANIFEST_MAX_ENTRIES,
) -> dict[str, Any]:
    """Classify and digest every external path this turn named.

    The repository snapshot answers one question -- did a registered checkout
    change -- and reports silence about everywhere else as cleanliness. That
    silence is what let audit-store writes count as workspace-clean evidence:
    the state root is exempted from repository intent as a plan artifact, so it
    produced no ``changed`` entry, no topology row, and nothing ``unavailable``.

    This pass judges paths rather than repositories. Every location the turn's
    own artifacts and sign-off message name is classified, control-plane paths
    are recorded with before and after digests so a permitted mutation is
    evidenced rather than assumed, and anything that fits no bucket becomes an
    ``unclassified`` row. ``turn_started_ns`` bounds that last lane to writes
    that actually landed during the turn: a path merely mentioned, or one last
    written before the turn began, is classified but not charged to it.
    """
    candidates: list[repository_intent.IntentCandidate] = []
    for path in artifact_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # Unreadable artifacts name nothing. The turn is still judged on
            # every artifact that can be read.
            continue
        candidates.extend(repository_intent.candidates_from_plan_document(text))
    intent = repository_intent.resolve_intent(
        candidates,
        root=root,
        exempt_roots=control_plane_roots,
        provenance_roots=provenance_roots,
    )
    if ephemeral_roots is None:
        ephemeral_roots = _ephemeral_roots()
    # A legacy run is bound to the repository containing its execution root,
    # which the caller cannot name because the root may sit below it.
    known_repositories = tuple(repository_roots) + (
        (intent.bound_git_root,) if intent.bound_git_root is not None else ()
    )

    counts = dict.fromkeys(EXTERNAL_WRITE_CLASSES, 0)
    control_plane: dict[str, dict[str, Any]] = {}
    authorized: dict[str, dict[str, Any]] = {}
    unclassified: dict[str, dict[str, Any]] = {}
    unclassified_unwritten: dict[str, dict[str, Any]] = {}
    mentioned: dict[str, str] = {}
    seen: set[str] = set()
    for resolved in intent.candidates:
        classification = _classify_external_path(
            resolved,
            control_plane_roots=control_plane_roots,
            repository_roots=known_repositories,
            workspace_root=workspace_root,
            ephemeral_roots=ephemeral_roots,
            authorized_roots=authorized_roots,
        )
        assert resolved.resolved is not None or classification == "unresolved"
        key = repository_intent.redact_credentials(
            str(resolved.resolved)
            if resolved.resolved is not None
            else resolved.candidate.value
        )
        if key in seen:
            continue
        seen.add(key)
        counts[classification] += 1
        mentioned[key] = classification
        if classification in ("control-plane", "authorized-external-data"):
            records = control_plane if classification == "control-plane" else authorized
            records[key] = {
                "before": dict(digests_before.get(key, {"observed": False})),
                "after": _external_path_digest(Path(str(resolved.resolved))),
                "surface": resolved.candidate.surface,
            }
            continue
        if classification != EXTERNAL_WRITE_UNCLASSIFIED:
            continue
        after = _external_path_digest(Path(str(resolved.resolved)))
        mutated = bool(after.get("present")) and (
            turn_started_ns is None
            or int(after.get("mtime_ns") or 0) >= turn_started_ns
        )
        if not mutated:
            # Named but not written during this turn: classified, recorded in
            # the counts, and not charged to the turn as a mutation. Kept by
            # name rather than as a bare tally, because a nonzero
            # ``classified.unclassified`` beside an empty ``unclassified`` lane
            # reads as a guard that saw something and then said nothing about
            # it (ASI-120).
            unclassified_unwritten[key] = {
                "reason": EXTERNAL_WRITE_UNCLASSIFIED_UNWRITTEN_REASON,
                "surface": resolved.candidate.surface,
                "after": after,
            }
            continue
        unclassified[key] = {
            "reason": EXTERNAL_WRITE_UNCLASSIFIED_REASON,
            "surface": resolved.candidate.surface,
            "after": after,
        }

    # Roots the turn never named are still compared, because the whole point is
    # that a control-plane write leaves no trace in the paths a turn declares.
    for key, before in digests_before.items():
        if key in control_plane or key in authorized:
            continue
        after = _external_path_digest(Path(key))
        if after == dict(before):
            continue
        is_authorized = any(Path(key).is_relative_to(root) for root in authorized_roots)
        is_control = any(Path(key).is_relative_to(root) for root in control_plane_roots)
        records = authorized if is_authorized and not is_control else control_plane
        records[key] = {
            "before": dict(before),
            "after": after,
            "surface": "authorized-root"
            if records is authorized
            else "control-plane-root",
        }

    # A worker must not redirect an approved root or turn an unreadable digest
    # into proof of an authorized write. Approval is pinned to the canonical
    # root selected before the turn, not to a mutable symlink spelling.
    for root in authorized_roots:
        key = repository_intent.redact_credentials(str(root))
        after = _external_path_digest(root)
        if workspaces.canonicalize_filesystem_path(root) != root or after.get(
            "unavailable"
        ):
            unclassified[key] = {
                "reason": "external-write-root-unavailable",
                "after": after,
            }
        authorized.setdefault(
            key,
            {
                "before": dict(digests_before.get(key, {"observed": False})),
                "after": after,
                "surface": "authorized-root",
            },
        )

    mutated_control_plane = sorted(
        key
        for key, record in control_plane.items()
        if record["before"].get("observed") is not False
        and record["before"] != record["after"]
    )
    reported, omitted = (
        sorted(control_plane)[:limit],
        max(0, len(control_plane) - limit),
    )
    evidence: dict[str, Any] = {
        "classified": {name: counts[name] for name in EXTERNAL_WRITE_CLASSES},
        "control_plane": {key: control_plane[key] for key in reported},
        "control_plane_mutated": mutated_control_plane[:limit],
        "authorized_roots": [str(root) for root in authorized_roots],
        "authorized": {key: authorized[key] for key in sorted(authorized)[:limit]},
        "authorized_omitted": max(0, len(authorized) - limit),
        "unclassified": unclassified,
        "unclassified_unwritten": {
            key: unclassified_unwritten[key]
            for key in sorted(unclassified_unwritten)[:limit]
        },
        "unclassified_unwritten_omitted": max(0, len(unclassified_unwritten) - limit),
        "paths": {key: mentioned[key] for key in sorted(mentioned)[:limit]},
    }
    if omitted:
        evidence["control_plane_omitted"] = omitted
    return evidence


def _external_write_unavailable(
    evidence: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Project unclassified external mutations into the fail-closed lane."""
    unclassified = evidence.get("unclassified")
    if not isinstance(unclassified, Mapping):
        return {}
    return {str(key): dict(value) for key, value in unclassified.items()}


def _control_plane_digest_roots(
    plan_dir: Path,
    state_dir_arg: str | None,
    *,
    run_dir: Path | None = None,
) -> tuple[Path, ...]:
    """The control-plane trees digested entry by entry before and after a turn.

    Narrower than :func:`run_inputs._runner_owned_roots`, which also names the
    whole state root: this run's own plan directory, plan state directory, and run
    directory are where a permitted control-plane mutation usually lands, and
    each is small enough to expand. The state root itself is digested too, but
    shallowly -- see :func:`_control_plane_shallow_digest_roots`.
    """
    roots = [plan_dir, state.plan_state_dir(plan_dir, state_dir_arg)]
    if run_dir is not None:
        roots.append(run_dir)
    return tuple(roots)


def _control_plane_shallow_digest_roots(state_dir_arg: str | None) -> tuple[Path, ...]:
    """Control-plane roots digested by shape alone, never entry by entry.

    The state root is where ASL keeps the stores that belong to no single plan
    -- the assurance inbox above all, which is the *external audit store* a
    session was observed editing while its bound workspace stayed clean
    (ASI-19). Its own directory digest carries every direct child's name, size,
    and mtime, so an append to a top-level store or a new plan directory is
    visible without expanding it; expanding it would digest every unrelated
    plan's state directory on every turn, which is the cost that kept it out of
    :func:`_control_plane_digest_roots`.
    """
    return (state.state_root(state_dir_arg),)


def _turn_external_write_evidence(
    *,
    plan_dir: Path,
    artifact_paths: Sequence[Path],
    root: Path,
    workspace_root: Path,
    repository_roots: Sequence[Path],
    state_dir_arg: str | None,
    run_dir: Path | None,
    digests_before: Mapping[str, Mapping[str, Any]] | None,
    turn_started_ns: int | None,
    authorized_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """Build one turn's external-write verdict for either guard mode."""
    return _external_write_evidence(
        artifact_paths=artifact_paths,
        root=root,
        workspace_root=workspace_root,
        repository_roots=repository_roots,
        control_plane_roots=run_inputs._runner_owned_roots(
            plan_dir, state_dir_arg, run_dir=run_dir
        ),
        provenance_roots=preflight_footprint.plan_provenance_exempt_roots(plan_dir),
        digests_before=digests_before if digests_before is not None else {},
        turn_started_ns=turn_started_ns,
        authorized_roots=authorized_roots,
    )
