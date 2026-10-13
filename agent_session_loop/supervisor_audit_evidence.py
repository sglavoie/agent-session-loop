"""Collect the bounded operational evidence a supervisor audit reads.

That bundle is control-plane only: identities, paths, digests, exit codes,
timings, and guard results, never application source or semantic diffs. It is
a pure function of the run's persisted history, so two equivalent histories
serialize to the same bytes, and anything the size cap forces out is reported
as a count, a digest, and a reason rather than silently dropped.

This is the lowest of the supervisor audit modules: the checks, the agent audit
and the :mod:`.supervisor_audit` entry module all read what it collects, so the
small record helpers they share live here too.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast, get_args

from . import (
    __version__,
    artifacts,
    assurance_models,
    events,
    external_write_evidence,
    harnesses,
    mailbox_constants,
    mailbox_vocabulary,
    plans,
    review,
    state,
    verification,
    workspaces,
)
from .assurance_models import (
    EvidenceKind,
)

#: Ref namespace holding a cache of some other repository's branches. It is
#: written by ``git fetch`` and by nothing a working tree does.
_REMOTE_TRACKING_REF_PREFIX = "refs/remotes/"


#: Version of the ``supervisor-audit-input.json`` payload. Version 2 renamed the
#: events and mailbox summaries' ``rejected_count`` to
#: :data:`UNPARSABLE_LINE_COUNT_KEY`.
AUDIT_INPUT_SCHEMA_VERSION = 2

#: How many lines of a JSONL log would not parse. It sat beside
#: ``counts.turn_rejected`` under the name ``rejected_count``, and the auditing
#: agent read the two as one number: a summary saying zero rejections next to a
#: ``turn_rejected`` record looked like a contradiction, and was filed as one 46
#: times over runs that had all behaved correctly. A field an auditor is asked
#: to reason about has to say what it counts.
UNPARSABLE_LINE_COUNT_KEY = "unparsable_line_count"

#: What the same count was called before schema version 2. Bundles written under
#: version 1 are still the corpus every replay proof is drawn from, so the
#: reader falls back to the old spelling rather than reading a damaged log as an
#: undamaged one.
LEGACY_UNPARSABLE_LINE_COUNT_KEY = "rejected_count"

#: Serialized cap on one audit input bundle, well under the
#: ``assurance_models.MAX_ARTIFACT_BYTES`` ceiling the writer enforces.
AUDIT_INPUT_MAX_BYTES = 512 * 1024

#: How much of a refused bundle the oversized-input retry keeps. A ratio rather
#: than a smaller fixed ceiling because the observed refusals were not at one
#: size — 357 KB was refused where 550 KB went through — so the provider's limit
#: is in tokens, which bytes only approximate. Halving whatever was actually
#: refused moves it decisively under that limit without asserting a byte figure
#: the evidence does not support, and lowering
#: :data:`AUDIT_INPUT_MAX_BYTES` for every run would pay that cost on the runs
#: that never had the problem.
AUDIT_INPUT_OVERFLOW_SHRINK_RATIO = 0.5

#: Per-side preview budget for a captured log or last message.
PREVIEW_HEAD_BYTES = 2 * 1024
PREVIEW_TAIL_BYTES = 2 * 1024

#: Smallest preview the size cap shrinks to before dropping the text entirely.
MIN_PREVIEW_BYTES = 128

#: Longest string kept verbatim inside a copied control-plane payload.
MAX_INLINE_STRING_BYTES = 512

#: Entries kept from any one copied list before the remainder is counted.
MAX_SEQUENCE_ITEMS = 100

#: Files hashed when fingerprinting the ASL package or the skill tree.
MAX_TREE_FILES = 2000

#: Directories a tree digest walks past: the interpreter writes them, nobody
#: authors them.
GENERATED_TREE_DIRECTORIES = frozenset({"__pycache__"})

#: Suffixes of compiled artifacts a tree digest ignores, so equivalent sources
#: hash alike.
GENERATED_TREE_SUFFIXES = frozenset({".pyc", ".pyo"})

#: Turns kept regardless of age, on top of every anomalous one.
RECENT_TURN_COUNT = 3

#: Stand-in for a value withheld because it looked like a credential.
REDACTED = "[redacted]"

#: Why something is absent from the bundle. Token-shaped so they can be read
#: back as reasons without further parsing.
REASON_SIZE_CAP = "size-cap"
REASON_MISSING = "missing"
REASON_UNREADABLE = "unreadable"
REASON_MALFORMED = "malformed"
REASON_OUTSIDE_APPROVED_ROOTS = "outside-approved-roots"
REASON_NOT_RECORDED = "not-recorded"
REASON_APPLICATION_CONTENT = "application-content-excluded"

#: Symbolic names for the roots every reference in the bundle resolves under.
EVIDENCE_ROOT_RUN = "run"
EVIDENCE_ROOT_STATE = "state"
EVIDENCE_ROOT_PLAN = "plan"
EVIDENCE_ROOT_WORKSPACE = "workspace"
EVIDENCE_ROOT_PACKAGE = "package"
EVIDENCE_ROOT_SKILLS = "skills"

#: Keys whose value is application output or a semantic diff rather than
#: control-plane evidence. Their digest and size are kept; their bytes are not.
_CONTENT_KEYS = frozenset(
    {
        "body",
        "content",
        "diff",
        "output",
        "patch",
        "source",
        "stderr",
        "stdout",
        "text",
    }
)

#: Mailbox kinds the bundle gives up first under the size cap. They time-stamp
#: an exchange; the question, the ruling on it, and the notice never go.
_SHEDDABLE_MAILBOX_KINDS = frozenset(mailbox_vocabulary.LIFECYCLE_KINDS) | {
    mailbox_vocabulary.KIND_ACK
}

#: Fields through which a mailbox record joins an exchange it does not name a
#: run for: a reply and a resolution point at their question, a receipt at the
#: closure it acknowledges, and a wait record at the wait it opened.
_MAILBOX_JOIN_KEYS = ("to", "wait_id", "closure_id")

#: How a mailbox record the collector claimed by delivery is marked, as opposed
#: to one that named this run outright or joined an exchange that did.
MAILBOX_OWNERSHIP_DELIVERED = "delivered"

#: Keys naming a Git origin, which is canonicalized rather than copied so a
#: credential embedded in a remote URL cannot reach the bundle.
_ORIGIN_KEYS = frozenset({"origin", "origins", "remote", "remote_url", "url"})

#: The bounded enum a reply origin is drawn from, mirroring
#: ``run_prompts.REPLY_ORIGINS``. No Git remote can collide with these tokens.
_REPLY_ORIGIN_VALUES = frozenset({"argv", "stdin", "mailbox"})

#: The only values ``assurance.role.source`` may carry.  ``source`` is
#: otherwise application content and remains withheld; these bounded enums are
#: control-plane provenance validated by :class:`assurance_models.AuditRole`.
_AUDIT_ROLE_SOURCE_VALUES = frozenset(assurance_models.AUDIT_ROLE_SOURCES)

#: Closed set of tokens a ``source`` may carry to name *where a runner control
#: signal was read*, as opposed to application output. Deliberately a value
#: allowlist rather than a parent-name exemption: a live run writes this
#: provenance under several different parents, while a ``source`` holding real
#: application text must stay withheld under every one of them.
#:
#: Mirrors, rather than imports, the values the run-loop boundaries above this
#: module own — this module is imported *by* them, so naming the values keeps
#: the layering one way round: ``get_args(harnesses.StreamName)``,
#: ``turn_execution.TRUSTED_RUNNER_SIGNAL_SOURCES`` minus the streams,
#: ``turn_execution.RUNNER_STATUS_SOURCE_ABSENT``, and the ``"pause"`` literal
#: ``run_termination`` stamps when the pause itself, not a stream, is the origin.
_CONTROL_PLANE_SOURCE_VALUES = frozenset(get_args(harnesses.StreamName)) | frozenset(
    {"last_message", "absent", "pause"}
)

#: Field parents under which ``origin`` names how an operator reply reached the
#: run rather than a repository. ``None`` covers the reply event, which carries
#: the enum at its payload root.
_REPLY_ORIGIN_PARENTS = frozenset({None, "reply_source", "source"})

#: Keys holding a process environment, whose values are withheld wholesale.
_ENVIRONMENT_KEYS = frozenset({"env", "environ", "environment"})

#: A key is secret-bearing when one of these words is the whole key or a whole
#: word inside it. The trailing boundary is what keeps ``auth`` from swallowing
#: ``authorized_aliases``; it admits a plural and an upper-case camelCase
#: continuation (``authToken``) so narrowing the match sheds no real secret.
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[-_.])(?:api[-_]?key|access[-_]?key|auth|authorization|bearer|"
    r"cookie|credential|credentials|passphrase|passwd|password|private[-_]?key|"
    r"pwd|secret|session[-_]?key|signature|token)s?(?=$|[-_.]|(?-i:[A-Z]))"
)
_SECRET_VALUE_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
#: The closed vocabulary the external-write guard classifies a path into.
#: A value drawn from it is a verdict, never a secret, whatever its key.
_EXTERNAL_WRITE_CLASS_VALUES = frozenset(external_write_evidence.EXTERNAL_WRITE_CLASSES)


def _is_path_key(name: str) -> bool:
    """Whether a map key is an absolute path rather than a field name."""
    return name.startswith("/") and "\n" not in name


_CREDENTIAL_URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")

#: Keys whose value is a command line. Argv is redacted positionally as well as
#: by shape, because a secret usually arrives as the argument after its flag.
_ARGV_KEYS = frozenset({"argv", "args", "command", "cmd"})
_SECRET_FLAG_RE = re.compile(
    r"(?i)^--?(?:api[-_]?key|auth|authorization|bearer|credential|credentials|"
    r"key|passphrase|passwd|password|pwd|secret|token)$"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)^(--?(?:api[-_]?key|auth|authorization|bearer|credential|credentials|"
    r"key|passphrase|passwd|password|pwd|secret|token)=)(.+)$"
)

#: Events emitted per heartbeat or per delivery. They dominate a long run's log
#: and say the least about it, so they are the first thing the cap sheds.
_BULK_EVENT_NAMES = frozenset(
    {
        events.AGENT_MUX_DELIVERY_EVENT,
        events.PROCESS_SWEEP_EVENT,
        events.TURN_HEARTBEAT_EVENT,
        events.VERIFICATION_CHECK_STARTED_EVENT,
        events.VERIFICATION_HEARTBEAT_EVENT,
    }
)

#: Events that are themselves the anomaly, a guard result, or the run's
#: terminal record. They are shed last and only when nothing else is left.
_ANOMALY_EVENT_NAMES = frozenset(
    {
        events.ACTION_REQUIRED_EVENT,
        events.PREFLIGHT_AUTO_REPAIRED_EVENT,
        events.RESOLUTION_CONSUMED_EVENT,
        events.REVIEW_REPAIR_REOPENED_EVENT,
        events.REVIEW_VERDICT_RETRY_EVENT,
        events.RUN_CONTROL_EVENT,
        events.RUN_FINISHED_EVENT,
        events.RUN_PAUSED_EVENT,
        events.TURN_HANG_CLEARED_EVENT,
        events.TURN_HANG_SUSPECTED_EVENT,
        events.TURN_REJECTED_EVENT,
        events.VERIFICATION_CHECK_SKIPPED_EVENT,
        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
    }
)

#: Protected records the cap gives up only after every other protected one:
#: how the run ended and which turns the guards refused are the facts a
#: containment verdict cannot be reached without.
_LAST_SHED_EVENT_NAMES = frozenset(
    {
        events.RUN_FINISHED_EVENT,
        events.RUN_PAUSED_EVENT,
        events.TURN_REJECTED_EVENT,
    }
)

_RUN_STARTED_EVENT = events.RUN_STARTED_EVENT
_TERMINAL_EVENT_NAMES = frozenset({events.RUN_FINISHED_EVENT, events.RUN_PAUSED_EVENT})

#: State keys copied verbatim (after redaction) into each projection.
_REVIEW_STATE_KEYS = (
    "review_artifact_sha256",
    "review_completed",
    "review_cycles",
    "review_harness",
    "review_output_path",
    "review_output_sha256",
    "review_requested",
    "review_session_executed",
    "review_session_id",
    "review_skipped_reason",
    "review_verdict",
)
_REPAIR_STATE_KEYS = (
    "review_fix_disposition",
    "review_reopen",
    "review_resume",
)
#: Mirrors ``commit_settlement.PAUSED_WORK_COMMIT_KEY`` as a literal. Importing
#: that module would drag the commit ledger, the loop's reporting and turn
#: execution into every audit; the bundle reads state as data, so a literal is
#: what the projection needs anyway.
PAUSED_WORK_COMMIT_KEY = "paused_work_commit"

_COMMIT_STATE_KEYS = (
    "commit_mode",
    "repair_commit_mode",
    "commit_groups",
    "commit_failures",
    "unresolved_commit_groups",
    "commits",
    # The one commit a run makes that no accepted turn produced. Without it the
    # bundle shows a `session-paused` group and a commit with no explanation for
    # either; with it, the settlement's own verdict travels beside them.
    PAUSED_WORK_COMMIT_KEY,
)
#: A commit attempt's artifact directory, as ``run_commit`` names it:
#: ``commit-{index:04d}-{suffix}``. Matching the numbered prefix rather than
#: ``commit-`` alone keeps sibling directories such as ``commit-group-recovery``
#: (``run_commit.RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME``), which hold messages
#: rather than attempts, from reading as an attempt with no result (ASI-117).
_COMMIT_ATTEMPT_DIR_RE = re.compile(r"^commit-\d{4,}-")


def _is_commit_attempt_dir_name(name: str) -> bool:
    """Whether a run-directory entry name is a commit attempt's directory."""
    return _COMMIT_ATTEMPT_DIR_RE.match(name) is not None


#: How a projected commit was learned. The runner's own commit records carry no
#: origin, so this marks exactly the entries the audit derived from the write
#: guard rather than from a commit the runner ran itself.
OBSERVED_COMMIT_ORIGIN = "observed-write-guard"
_STATUS_STATE_KEYS = (
    "action_required",
    # A run that superseded a live ask on its way into a runner-declared pause
    # wrote the displaced record here. Projecting it is what stops the bundle
    # from showing an `action_required` of null with nothing saying what stood
    # there.
    events.ACTION_REQUIRED_SUPERSEDED_KEY,
    "assurance",
    "command",
    "completed_session_ids",
    "completed_sessions",
    "control",
    "details",
    "dry_run",
    "duration_seconds",
    "ok",
    "pause_details",
    "pause_message",
    "pause_reason",
    "pause_policy",
    "paused",
    "readiness_gate",
    "readiness_gate_reopened",
    "readiness_state_active",
    "readiness_state_current",
    "readiness_state_final",
    "readiness_state_initial",
    "reason",
    "run_finished_at",
    "run_started_at",
    "version",
)
#: Where a retired ``status.action_required`` is kept. The status section is
#: otherwise a verbatim copy of the terminal state payload, so a retirement has
#: to name what overrode the copy and keep the record it replaced: the history
#: an auditor reads is not allowed to shrink because the current state moved on.
#: Shared with ``run_termination``, which writes the same block onto the payload
#: when a runner-declared pause supersedes a live ask, so the copy the audit
#: makes and the copy it might inherit are spelled one way.
_STATUS_ACTION_REQUIRED_SUPERSEDED = events.ACTION_REQUIRED_SUPERSEDED_KEY

#: What retired it: the run's mailbox holds the ruling that answered the ask,
#: and a receipt proving the ruling reached the run.
SUPERSEDED_BY_CONSUMED_RULING = "mailbox-ruling-consumed"
_SNAPSHOT_IDENTITY_KEYS = (
    "content_sha256",
    "counts",
    "next_pending",
    "next_pending_id",
    "path_hash",
    "plan_hash",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes without splitting."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class EvidenceRoots:
    """The approved roots every reference in the bundle must resolve inside.

    References are rendered as ``<root>/<relative>`` rather than as absolute
    paths, which is what makes containment decidable: a locator that cannot be
    expressed this way names something outside the audit's reach and is
    withheld instead of copied.
    """

    roots: tuple[tuple[str, Path], ...]

    @classmethod
    def build(
        cls,
        *,
        run_dir: Path | str,
        state_dir: Path | str,
        plan_dir: Path | str,
        workspace_root: Path | str | None = None,
    ) -> EvidenceRoots:
        package_root = Path(__file__).resolve().parent
        declared: list[tuple[str, Path | str]] = [
            (EVIDENCE_ROOT_RUN, run_dir),
            (EVIDENCE_ROOT_STATE, state_dir),
            (EVIDENCE_ROOT_PLAN, plan_dir),
            (EVIDENCE_ROOT_PACKAGE, package_root),
            (EVIDENCE_ROOT_SKILLS, package_root.parent / "skills"),
        ]
        if workspace_root is not None:
            declared.append((EVIDENCE_ROOT_WORKSPACE, workspace_root))
        return cls(
            roots=tuple(
                (name, workspaces.canonicalize_filesystem_path(value))
                for name, value in declared
            )
        )

    def to_dict(self) -> dict[str, str]:
        return {name: str(path) for name, path in self.roots}

    def locate(self, path: Path | str) -> str | None:
        """Return the symbolic locator for ``path``, or ``None`` when unsafe.

        The deepest matching root wins, so an artifact inside the run directory
        is named ``run/...`` rather than ``state/runs/<id>/...`` even though
        both containments hold.
        """
        candidate = workspaces.canonicalize_filesystem_path(path)
        best: tuple[int, str] | None = None
        for name, root in self.roots:
            if candidate == root:
                locator = name
            elif workspaces.is_descendant(candidate, root):
                locator = f"{name}/{candidate.relative_to(root).as_posix()}"
            else:
                continue
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, locator)
        return None if best is None else best[1]


@dataclass(frozen=True)
class AuditInput:
    """The bounded, control-plane-only evidence bundle an auditor reads."""

    payload: dict[str, Any]

    def to_bytes(self) -> bytes:
        return assurance_models.canonical_json_bytes(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def evidence(self) -> tuple[assurance_models.EvidenceRef, ...]:
        return tuple(
            assurance_models.parse_evidence_ref(entry)
            for entry in self.payload.get("evidence", ())
        )

    @property
    def omissions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload.get("omissions", ()))

    def write(self, run_dir: Path | str, *, durable: bool = True) -> Path:
        """Write the bundle as the run's canonical audit input, exactly once."""
        return assurance_models.write_audit_artifact(
            run_dir,
            assurance_models.AUDIT_INPUT_FILENAME,
            self.to_bytes(),
            durable=durable,
        )


class _Omissions:
    """Counts, digests, and reasons for everything the bundle left out."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}

    def record(self, section: str, reason: str, payload: Any) -> None:
        entry = self._entries.setdefault(
            (section, reason),
            {
                "section": section,
                "reason": reason,
                "count": 0,
                "digest": hashlib.sha256(),
            },
        )
        entry["count"] += 1
        entry["digest"].update(assurance_models.canonical_json_bytes(payload))

    def seed(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Adopt already-rendered omissions so a later pass extends them.

        A rendered entry carries a digest, not the payloads it was built from,
        so the running hash is re-opened over that digest: the result still
        identifies exactly one set of withheld payloads, and whatever the next
        pass drops extends it. Counts stay exact, which is what a reader of the
        bundle acts on.
        """
        for entry in entries:
            section = str(entry.get("section") or "")
            reason = str(entry.get("reason") or "")
            count = entry.get("count")
            digest = hashlib.sha256()
            digest.update(assurance_models.canonical_json_bytes(entry.get("sha256")))
            self._entries[(section, reason)] = {
                "section": section,
                "reason": reason,
                "count": count if isinstance(count, int) and count > 0 else 0,
                "digest": digest,
            }

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {
                "section": entry["section"],
                "reason": entry["reason"],
                "count": entry["count"],
                "sha256": entry["digest"].hexdigest(),
            }
            for entry in sorted(
                self._entries.values(),
                key=lambda item: (item["section"], item["reason"]),
            )
        ]


class _Collector:
    """Redacts, bounds, and validates every value on its way into the bundle."""

    def __init__(self, roots: EvidenceRoots) -> None:
        self._roots = roots
        self._evidence: dict[str, assurance_models.EvidenceRef] = {}
        self._previews: list[dict[str, Any]] = []
        self.omissions = _Omissions()

    # -- references ------------------------------------------------------

    def locator(self, path: Path | str | None) -> str | None:
        if path is None:
            return None
        locator = self._roots.locate(path)
        if locator is None:
            self.omissions.record(
                "references", REASON_OUTSIDE_APPROVED_ROOTS, str(path)
            )
        return locator

    def reference(
        self,
        path: Path | str | None,
        *,
        kind: EvidenceKind,
        digest: str | None = None,
        detail: str | None = None,
    ) -> str | None:
        """Register ``path`` in the evidence index and return its locator."""
        locator = self.locator(path)
        if locator is None:
            return None
        if (
            locator not in self._evidence
            and len(self._evidence) < assurance_models.MAX_EVIDENCE_ENTRIES
        ):
            self._evidence[locator] = assurance_models.EvidenceRef(
                kind=kind, locator=locator, digest=digest, detail=detail
            )
        return locator

    def evidence(self) -> list[dict[str, Any]]:
        return [self._evidence[locator].to_dict() for locator in sorted(self._evidence)]

    # -- redaction -------------------------------------------------------

    def redact(
        self, value: Any, *, key: str | None = None, parent: str | None = None
    ) -> Any:
        """Return ``value`` with credentials, content, and unsafe paths removed."""
        if parent == verification.FAILURE_DIAGNOSTIC_KEY:
            # A structured rejection travels as control-plane evidence, so
            # every field inside it is held to that standard: a value drawn
            # from ASL's own closed gate vocabulary passes, and anything else —
            # an unknown key, a nested structure, an application token in a
            # field that must name a gate or a reason — is the rejected
            # artifact's own text and is withheld like any output.
            if not verification.is_control_plane_diagnostic_field(key, value):
                return self._content_stub(
                    value
                    if isinstance(value, str)
                    else json.dumps(value, default=str, sort_keys=True)
                )
            return self._redact_string(value, key=key, parent=parent)
        if isinstance(value, Mapping):
            redacted: dict[str, Any] = {}
            for raw_name, item in value.items():
                name = str(raw_name)
                if _is_path_key(name):
                    # A path-shaped key is data, not a field name: the guard
                    # keys its per-path manifests by the path it judged, and a
                    # path that merely ends in ``ntfy.token`` names no secret.
                    # Its value is therefore judged in the enclosing field's
                    # context, and the key itself is held to the same root
                    # policy as a path-shaped value.
                    redacted[self._redact_path_key(name)] = self._redact_path_keyed(
                        name, item, key=key, parent=parent
                    )
                    continue
                redacted[name] = self.redact(item, key=name, parent=key)
            return redacted
        if isinstance(value, (list, tuple)):
            items = list(value)
            if key in _ARGV_KEYS:
                items = _redact_argv(items)
            kept = [
                self.redact(item, key=key, parent=parent)
                for item in items[:MAX_SEQUENCE_ITEMS]
            ]
            for dropped in items[MAX_SEQUENCE_ITEMS:]:
                self.omissions.record(key or "payload", REASON_SIZE_CAP, dropped)
            return kept
        if isinstance(value, str):
            return self._redact_string(value, key=key, parent=parent)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return self._redact_string(str(value), key=key, parent=parent)

    def _redact_string(
        self, text: str, *, key: str | None, parent: str | None = None
    ) -> str:
        if key is not None:
            if _SECRET_KEY_RE.search(key):
                return REDACTED
            if key in _ENVIRONMENT_KEYS:
                return REDACTED
            if key in _CONTENT_KEYS:
                if parent == "role" and text in _AUDIT_ROLE_SOURCE_VALUES:
                    return text
                if key == "source" and text in _CONTROL_PLANE_SOURCE_VALUES:
                    return text
                return self._content_stub(text)
            if key in _ORIGIN_KEYS:
                if _is_reply_origin(text, key=key, parent=parent):
                    return text
                return _redact_origin(text)
        if text.startswith("/") and "\n" not in text:
            located = self._roots.locate(text)
            if located is not None:
                return located
            self.omissions.record("references", REASON_OUTSIDE_APPROVED_ROOTS, text)
            return f"[{REASON_OUTSIDE_APPROVED_ROOTS}:{_sha256_text(text)[:16]}]"
        cleaned = _redact_secrets(text)
        if len(cleaned.encode("utf-8")) <= MAX_INLINE_STRING_BYTES:
            return cleaned
        self.omissions.record(key or "payload", REASON_SIZE_CAP, text)
        return (
            f"{_truncate_utf8(cleaned, MAX_INLINE_STRING_BYTES)}"
            f"…[truncated sha256:{_sha256_text(text)[:16]}]"
        )

    def _redact_path_keyed(
        self, name: str, item: Any, *, key: str | None, parent: str | None
    ) -> Any:
        """Redact the value stored under the path-shaped key ``name``."""
        if isinstance(item, str):
            if item in _EXTERNAL_WRITE_CLASS_VALUES:
                return item
            if _SECRET_KEY_RE.search(Path(name).name):
                # A free-form string stored against a credential-shaped
                # file name may be that file's content; withhold it.
                return REDACTED
        return self.redact(item, key=key, parent=parent)

    def _redact_path_key(self, name: str) -> str:
        """Return a path-shaped map key, hashed when outside approved roots.

        A key inside an approved root is kept verbatim so a lookup by path
        still resolves; one outside every root is withheld exactly as a
        path-shaped value would be.
        """
        if self._roots.locate(name) is not None:
            return name
        self.omissions.record("references", REASON_OUTSIDE_APPROVED_ROOTS, name)
        return f"[{REASON_OUTSIDE_APPROVED_ROOTS}:{_sha256_text(name)[:16]}]"

    def _content_stub(self, text: str) -> str:
        self.omissions.record("payload", REASON_APPLICATION_CONTENT, len(text))
        return (
            f"[{REASON_APPLICATION_CONTENT} bytes:{len(text.encode('utf-8'))} "
            f"sha256:{_sha256_text(text)[:16]}]"
        )

    # -- files -----------------------------------------------------------

    def file_digest(
        self, path: Path, *, kind: EvidenceKind, section: str | None = None
    ) -> dict[str, Any]:
        """Describe a file by identity alone: locator, size, and digest.

        ``section`` names the omission bucket an absent or unreadable file is
        counted under. Evidence kinds are a closed vocabulary, so a caller that
        wants a narrower bucket than its kind says so here.
        """
        record: dict[str, Any] = {"reference": None, "available": False}
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            record["reason"] = REASON_MISSING
        except OSError:
            record["reason"] = REASON_UNREADABLE
        else:
            digest = artifacts.sha256_hex(data)
            record.update(
                available=True,
                byte_count=len(data),
                sha256=digest,
                reference=self.reference(path, kind=kind, digest=digest),
            )
            return record
        record["reference"] = self.locator(path)
        self.omissions.record(section or kind, record["reason"], str(path))
        return record

    def file_preview(self, path: Path, *, kind: EvidenceKind) -> dict[str, Any]:
        """Describe a stream by identity plus a bounded, shrinkable preview."""
        capture = artifacts.StreamCapture(
            head_bytes=PREVIEW_HEAD_BYTES, tail_bytes=PREVIEW_TAIL_BYTES
        )
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    capture.feed(chunk)
        except FileNotFoundError:
            capture.discard()
            return self._unavailable_preview(path, kind, REASON_MISSING)
        except OSError:
            capture.discard()
            return self._unavailable_preview(path, kind, REASON_UNREADABLE)
        metadata = capture.finalize(failed=False)
        record = {
            "available": True,
            "reference": self.reference(path, kind=kind, digest=metadata.sha256),
            "byte_count": metadata.byte_count,
            "sha256": metadata.sha256,
            "truncated": metadata.truncated,
            "preview": _redact_secrets(metadata.preview),
        }
        self._previews.append(record)
        return record

    def _unavailable_preview(
        self, path: Path, kind: EvidenceKind, reason: str
    ) -> dict[str, Any]:
        self.omissions.record(kind, reason, str(path))
        return {
            "available": False,
            "reason": reason,
            "reference": self.locator(path),
        }

    def text_preview(self, text: str, *, label: str) -> dict[str, Any]:
        """Bound a string that is already in memory, keeping its full digest."""
        encoded = text.encode("utf-8")
        preview, truncated = artifacts.bounded_preview(
            encoded, head_bytes=PREVIEW_HEAD_BYTES, tail_bytes=PREVIEW_TAIL_BYTES
        )
        record = {
            "available": True,
            "label": label,
            "byte_count": len(encoded),
            "sha256": artifacts.sha256_hex(encoded),
            "truncated": truncated,
            "preview": _redact_secrets(preview),
        }
        self._previews.append(record)
        return record

    # -- size cap --------------------------------------------------------

    def shrink_previews(
        self, budget: int, *, only: Iterable[dict[str, Any]] | None = None
    ) -> bool:
        """Trim preview text to ``budget`` bytes, reporting whether anything moved."""
        targets = self._previews if only is None else list(only)
        changed = False
        for record in targets:
            preview = record.get("preview")
            if not isinstance(preview, str) or not preview:
                continue
            if len(preview.encode("utf-8")) <= budget:
                continue
            record["preview"] = _truncate_utf8(preview, budget)
            record["truncated"] = True
            self.omissions.record("previews", REASON_SIZE_CAP, record.get("sha256"))
            changed = True
        return changed

    def adopt_previews(self, payload: Any) -> None:
        """Register the previews already inside ``payload`` as shrinkable.

        A collector that did not build the payload has no preview list, and
        :meth:`shrink_previews` would then silently shrink nothing — leaving a
        second shedding pass to drop whole turns where trimming their text would
        have done. Recovering them from the payload keeps the give-up order
        intact across passes.
        """
        if isinstance(payload, Mapping):
            if isinstance(payload.get("preview"), str) and "sha256" in payload:
                self._previews.append(cast(dict[str, Any], payload))
                return
            for value in payload.values():
                self.adopt_previews(value)
        elif isinstance(payload, list):
            for value in payload:
                self.adopt_previews(value)

    @property
    def previews(self) -> list[dict[str, Any]]:
        return self._previews


def _redact_argv(items: Sequence[Any]) -> list[Any]:
    """Blank out argv values that a secret-bearing flag introduces or embeds."""
    redacted: list[Any] = []
    expect_secret = False
    for item in items:
        if not isinstance(item, str):
            redacted.append(item)
            expect_secret = False
            continue
        if expect_secret:
            redacted.append(REDACTED)
            expect_secret = False
            continue
        assignment = _SECRET_ASSIGNMENT_RE.match(item)
        if assignment is not None:
            redacted.append(f"{assignment.group(1)}{REDACTED}")
            continue
        redacted.append(item)
        expect_secret = _SECRET_FLAG_RE.match(item) is not None
    return redacted


def _is_reply_origin(value: str, *, key: str, parent: str | None) -> bool:
    """Whether an ``origin`` field names a reply channel rather than a remote.

    Canonicalizing one erases the only record of whether an operator reply
    arrived by argv, by stdin, or through the mailbox, and leaves an
    ``[unparsable-origin:…]`` hash in its place.
    """
    return (
        key == "origin"
        and parent in _REPLY_ORIGIN_PARENTS
        and value in _REPLY_ORIGIN_VALUES
    )


def _redact_origin(value: str) -> str:
    """Return a credential-free origin identity, or a stable stand-in."""
    try:
        return workspaces.canonicalize_origin(value)
    except workspaces.WorkspaceError:
        return f"[unparsable-origin:{_sha256_text(value)[:16]}]"


def _redact_secrets(text: str) -> str:
    """Strip URL userinfo and recognizable credential literals from ``text``."""
    cleaned = _CREDENTIAL_URL_RE.sub(rf"\1{REDACTED}@", text)
    return _SECRET_VALUE_RE.sub(REDACTED, cleaned)


def _read_json_file(path: Path) -> tuple[Any, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, REASON_MISSING
    except OSError:
        return None, REASON_UNREADABLE
    try:
        return json.loads(text), None
    except json.JSONDecodeError, ValueError:
        return None, REASON_MALFORMED


def _is_generated_artifact(relative: Path) -> bool:
    """True when a path is interpreter output rather than an authored source file."""
    if any(part in GENERATED_TREE_DIRECTORIES for part in relative.parts):
        return True
    return relative.suffix in GENERATED_TREE_SUFFIXES


def _tree_digest(root: Path, *, patterns: Sequence[str]) -> dict[str, Any]:
    """Hash a source tree by relative path and content, never by mtime.

    Generated bytecode is excluded, so two checkouts of the same sources hash
    alike no matter how often an interpreter has run against them.
    """
    if not root.is_dir():
        return {"available": False, "reason": REASON_MISSING}
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(
            candidate
            for candidate in root.rglob(pattern)
            if candidate.is_file()
            and not _is_generated_artifact(candidate.relative_to(root))
        )
    ordered = sorted(set(paths), key=lambda item: item.relative_to(root).as_posix())
    omitted = max(0, len(ordered) - MAX_TREE_FILES)
    digest = hashlib.sha256()
    for path in ordered[:MAX_TREE_FILES]:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"\x00unreadable")
    return {
        "available": True,
        "file_count": min(len(ordered), MAX_TREE_FILES),
        "omitted_file_count": omitted,
        "sha256": digest.hexdigest(),
    }


def _event_priority(name: str, payload: Mapping[str, Any]) -> int:
    """Return 0 for evidence the cap protects, 2 for what it sheds first.

    Bulk chatter never reaches the protected tier, even when one record's
    payload looks like a failure: hundreds of heartbeats and deliveries
    sharing that tier with the pause they precede would shed the pause first,
    because a tier is shed oldest first (ASI-9).
    """
    if name in _ANOMALY_EVENT_NAMES or name == _RUN_STARTED_EVENT:
        return 0
    if name in _BULK_EVENT_NAMES:
        return 1 if _payload_shows_failure(payload) else 2
    if _payload_shows_failure(payload):
        return 0
    return 1


def _payload_shows_failure(payload: Mapping[str, Any]) -> bool:
    """Whether a record is evidence of an anomaly rather than of progress."""
    returncode = payload.get("returncode")
    if isinstance(returncode, int) and returncode != 0:
        return True
    if payload.get("passed") is False:
        return True
    if payload.get("status") in {"fail", "unavailable"}:
        return True
    for key in ("timed_out", "interrupted", "hang_suspected", "paused"):
        if payload.get(key) is True:
            return True
    for key in ("failure_reason", "failure_classification", "pause_reason"):
        if payload.get(key):
            return True
    return payload.get("ok") is False


_REVIEW_BROAD_FAILURES_DEFERRED_REASON = "findings-defer-broad-failures"
_BROAD_REVIEW_CHECK_PREFIX = "broad:"


def _deferred_review_acceptance(
    verification_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return a structurally valid broad-failure acceptance projection."""
    acceptance = verification_payload.get("acceptance")
    if not isinstance(acceptance, Mapping):
        return None
    names = acceptance.get("deferred_failure_names")
    if not isinstance(names, (list, tuple)) or not names:
        return None
    if any(
        not isinstance(name, str) or not name.startswith(_BROAD_REVIEW_CHECK_PREFIX)
        for name in names
    ):
        return None
    if not (
        acceptance.get("accepted") is True
        and acceptance.get("reason") == _REVIEW_BROAD_FAILURES_DEFERRED_REASON
        and acceptance.get("review_outcome") == "findings"
        and acceptance.get("deferred_failure_count") == len(names)
    ):
        return None
    return acceptance


def _turn_shows_failure(result: Mapping[str, Any]) -> bool:
    """Whether a turn result is an anomaly, including at exit code zero.

    A harness can exit cleanly and still leave the turn unusable: the handoff
    may no longer parse, or the plan may have moved in a way the loop refuses.
    Both are recorded on the turn result rather than in its exit code, so an
    audit that only reads the generic failure signals reports the turn as
    healthy and hides exactly the case an operator opened the audit to find.
    """
    if _payload_shows_failure(result):
        return True
    if result.get("plan_error"):
        return True
    if _guard_shows_failure(result.get("workspace_write_guard")):
        return True
    # Verification is the authoritative gate, and it runs after the harness has
    # already exited. A turn whose gate failed therefore looks healthy at every
    # top-level signal, so the nested result is the only place the failure is.
    verification = result.get("verification")
    if isinstance(verification, Mapping) and verification.get("passed") is False:
        return _deferred_review_acceptance(verification) is None
    status_result = result.get("status_result")
    return status_result is not None and status_result != "complete"


def _mark_rejected_turns(
    turns: list[dict[str, Any]], event_records: Sequence[Mapping[str, Any]]
) -> None:
    """Mark turns a post-execution guard refused after they had already run.

    A refusal is recorded in the event stream, not on the turn result the
    harness left behind, so the turn keeps claiming the clean exit it made
    before the guard reverted it. Correlating by turn index is what stops a
    refused turn from being projected as healthy.
    """
    rejected: set[int] = set()
    for record in event_records:
        if record.get("name") != events.TURN_REJECTED_EVENT:
            continue
        index = _mapping(record.get("payload")).get("turn_index")
        if isinstance(index, int):
            rejected.add(index)
    if not rejected:
        return
    for turn in turns:
        index = _mapping(turn.get("result")).get("turn_index")
        if isinstance(index, int) and index in rejected:
            turn["anomalous"] = True
            turn["rejected"] = True


def _unredeemed_deferral_turn_indexes(
    state_payload: Mapping[str, Any],
    event_records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[int, list[str]]:
    """Turn indexes whose accepted deferral the run then declared unredeemed.

    Read from the pause the run actually took rather than re-derived: the
    ``required-checks-incomplete`` blocker already resolved every deferral
    against the review cycles that could have closed it, and it names the ones
    that reached no all-clear. Entries carrying another run's id are skipped,
    because this bundle's turn indexes mean nothing in that run's numbering.
    """
    sources: list[Mapping[str, Any]] = []
    status_details = _mapping(state_payload.get("pause_details"))
    if state_payload.get("pause_reason") == (
        events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE
    ):
        sources.append(status_details)
    for record in event_records:
        if record.get("name") != events.RUN_PAUSED_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        if payload.get("reason") == events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE:
            sources.append(_mapping(payload.get("details")))
    indexes: dict[int, list[str]] = {}
    for details in sources:
        for item in _sequence(details.get("deferred_unresolved")):
            entry = _mapping(item)
            entry_run = entry.get("run_id")
            if isinstance(entry_run, str) and entry_run and entry_run != run_id:
                continue
            index = entry.get("turn_index")
            if not isinstance(index, int):
                continue
            checks = [
                check
                for check in _sequence(entry.get("checks"))
                if isinstance(check, str)
            ]
            indexes.setdefault(index, [])
            for check in checks:
                if check not in indexes[index]:
                    indexes[index].append(check)
    return indexes


def _mark_unredeemed_deferral_turns(
    turns: list[dict[str, Any]],
    state_payload: Mapping[str, Any],
    event_records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> None:
    """Mark turns whose deferred broad-check failures were never redeemed.

    A findings review may finish with the plan's own gates failing, because
    those failures are the repair specification. What the deferral buys is a
    delay, redeemed only by a later all-clear review -- so an accepted deferral
    exempts the turn from :func:`_turn_shows_failure` on the promise that the
    redemption is still coming. When the run then pauses as
    ``required-checks-incomplete`` naming those same checks, the promise is
    broken and the exemption outlived its premise: the turn was projected
    healthy while the run it belongs to was refusing to report success for
    exactly its verification (ASI-15). Marked here rather than inside
    ``_turn_shows_failure`` for the same reason a guard refusal is: the fact
    that settles it is recorded after the turn, not on its result.
    """
    indexes = _unredeemed_deferral_turn_indexes(
        state_payload, event_records, run_id=run_id
    )
    if not indexes:
        return
    for turn in turns:
        result = _mapping(turn.get("result"))
        index = result.get("turn_index")
        if not isinstance(index, int) or index not in indexes:
            continue
        if _mapping(result.get("verification")).get("passed") is not False:
            continue
        turn["anomalous"] = True
        turn["deferral_unredeemed"] = True
        if indexes[index]:
            turn["unredeemed_checks"] = list(indexes[index])


def _guard_shows_failure(guard: Any) -> bool:
    """Whether a workspace write guard refused the turn or could not judge it.

    A turn that wrote outside its declared scope exits zero and reports a
    complete status, so the guard result is the only place that failure is
    recorded. A guard that could not compare the workspace counts too: it
    proves nothing about the writes it was there to bound.
    """
    if not isinstance(guard, Mapping):
        return False
    if guard.get("passed") is False:
        return True
    if guard.get("violations"):
        return True
    if guard.get("unavailable"):
        return True
    return bool(guard.get("reason"))


def _turn_directories(run_dir: Path) -> list[Path]:
    try:
        entries = [
            entry
            for entry in run_dir.iterdir()
            if entry.is_dir() and entry.name.startswith(state.TURN_DIR_PREFIX)
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda entry: entry.name)


#: How ``repository_evidence`` keys a linked worktree: the repository it
#: belongs to, then the worktree's own root.
_WORKTREE_KEY_SEPARATOR = "::worktree::"


def _declared_repository_alias(guard: Mapping[str, Any], key: str) -> str:
    """Name a guard's changed-repository key the way the plan declares it.

    The change map is keyed by workspace-relative repository path, and by
    ``<path>::worktree::<root>`` for a linked worktree, while session scopes --
    and therefore every write-scope question asked of them -- are keyed by
    registry alias. Comparing the two namespaces directly reads a commit to a
    declared repository as a commit to an undeclared one, which is what made a
    routine run report a scope breach it never committed (ASI-117).

    A linked worktree resolves to the repository it belongs to because it
    shares that repository's object store and refs: a commit made there lands
    in the registered repository, so the registered repository is what the
    declared scope has to answer for. A key that resolves to no registered
    repository is returned verbatim, so an unknown repository stays unknown.
    """
    registered = [
        _mapping(entry) for entry in _sequence(guard.get("registered_repositories"))
    ]
    aliases: set[str] = set()
    by_path: dict[str, str] = {}
    for entry in registered:
        alias = entry.get("alias")
        if not isinstance(alias, str) or not alias:
            continue
        aliases.add(alias)
        path = entry.get("path")
        if isinstance(path, str) and path:
            by_path[path] = alias
    root = key.split(_WORKTREE_KEY_SEPARATOR, 1)[0]
    if root in aliases:
        return root
    return by_path.get(root, key)


def _observed_repository_writes(
    turns: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Repository writes the write guard saw, whoever made them.

    ``commit_mode`` describes only the commits the runner itself was asked to
    make, so a worker that commits under a null commit mode leaves the commit
    projection empty while the guard records the move. Harvesting the guard
    here means the projection answers "what landed" rather than "what the
    runner intended", which is the question an auditor is asking.

    A moved ref counts even when HEAD stayed put: a commit written to a branch
    the worktree does not have checked out still landed. A moved *remote-tracking*
    ref does not, because it is a cache of another repository that ``git fetch``
    rewrites and nothing a turn writes touches. Projecting one as a commit put a
    commit the session never made in front of the checks that read this
    projection, and one of them read it as the write-scope guard having failed to
    hold a violation it did hold (ASI-9).
    """
    observed: list[dict[str, Any]] = []
    for turn in turns:
        result = _mapping(turn.get("result"))
        guard = _mapping(result.get("workspace_write_guard"))
        for key, raw_changes in _mapping(guard.get("changed")).items():
            changes = _mapping(raw_changes)
            refs = [
                str(_mapping(entry).get("ref"))
                for entry in _sequence(changes.get("refs_changed"))
                if _mapping(entry).get("ref")
                and not str(_mapping(entry)["ref"]).startswith(
                    _REMOTE_TRACKING_REF_PREFIX
                )
            ]
            head_changed = changes.get("head_changed") is True
            if not head_changed and not refs:
                continue
            observed.append(
                {
                    "turn": turn.get("name"),
                    "directory": turn.get("directory"),
                    "repository": _declared_repository_alias(guard, str(key)),
                    "repository_key": str(key),
                    "session_id": result.get("session_id"),
                    "head_changed": head_changed,
                    "transition": _mapping(changes.get("head_transition")) or None,
                    "head_before": changes.get("head_before"),
                    "head_after": changes.get("head_after"),
                    "refs_changed": refs,
                }
            )
    return observed


def _observed_head_transitions(
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The observed writes that moved HEAD, in the shape bundles record."""
    return [
        {
            "turn": entry.get("turn"),
            "repository": entry.get("repository"),
            "transition": entry.get("transition"),
        }
        for entry in observed
        if entry.get("head_changed") is True
    ]


def _recorded_commit_heads(section: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The repository and resulting head of every commit the run recorded."""
    recorded = [_mapping(item) for item in _sequence(section.get("commits"))]
    recorded.extend(
        _mapping(_mapping(item).get("result"))
        for item in _sequence(section.get("attempts"))
    )
    pairs: set[tuple[str, str]] = set()
    for entry in recorded:
        alias = (
            entry.get("repository_alias")
            or entry.get("repo")
            or entry.get("repository_key")
        )
        for key in ("head_after", "commit_sha"):
            head = entry.get(key)
            if isinstance(alias, str) and isinstance(head, str):
                pairs.add((alias, head))
    return pairs


def _reconcile_observed_commits(
    section: dict[str, Any], observed: Sequence[Mapping[str, Any]]
) -> None:
    """Project what the guard saw onto the commit records the run kept.

    A worker that commits on its own leaves ``attempts``, ``commit_groups`` and
    ``commits`` empty while the commits themselves sit in the repository, so
    the projections are derived here rather than left contradicting the guard.
    Every derived entry names its ``origin``, and one whose resulting head a
    recorded commit already claims is dropped instead of counted twice.
    """
    accounted = _recorded_commit_heads(section)
    commits: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    groups: dict[Any, dict[str, Any]] = {}
    for entry in observed:
        alias = entry.get("repository")
        transition = _mapping(entry.get("transition"))
        # A transition is one measured move, so its endpoints and its count
        # travel together. The entry's own heads are the session's baseline,
        # which a continuation merge can carry back past this turn's move;
        # pairing that baseline with the transition's count described a range
        # the count was never measured over (ASI-158).
        if transition:
            head_before = transition.get("before")
            head_after = transition.get("after")
        else:
            head_before = entry.get("head_before")
            head_after = entry.get("head_after")
        if (
            isinstance(alias, str)
            and isinstance(head_after, str)
            and (alias, head_after) in accounted
        ):
            continue
        commits_added = transition.get("commits_added")
        commit = {
            "origin": OBSERVED_COMMIT_ORIGIN,
            "turn": entry.get("turn"),
            "session_id": entry.get("session_id"),
            # The key stays verbatim so the exact repository the guard named is
            # never lost, while the alias is what a write-scope question reads.
            "repository_key": entry.get("repository_key") or alias,
            "repository_alias": alias,
            "repo": alias,
            "head_before": head_before,
            "head_after": head_after,
            "commit_sha": head_after if commits_added else None,
            "relation": transition.get("relation"),
            "commits_added": commits_added,
            "history_rewrite": transition.get("history_rewrite"),
            "refs_changed": list(entry.get("refs_changed") or ()),
        }
        commits.append(commit)
        attempts.append(
            {
                "name": entry.get("turn"),
                "reference": entry.get("directory"),
                "origin": OBSERVED_COMMIT_ORIGIN,
                "result": commit,
            }
        )
        session_id = entry.get("session_id")
        group = groups.setdefault(
            session_id if session_id is not None else entry.get("turn"),
            {
                "origin": OBSERVED_COMMIT_ORIGIN,
                "scope": "session" if session_id is not None else "end",
                "session_id": session_id,
                "ordered_aliases": [],
                "repositories": [],
            },
        )
        if alias not in group["ordered_aliases"]:
            group["ordered_aliases"].append(alias)
        group["repositories"].append(commit)
    if not commits:
        return
    section["attempts"] = list(_sequence(section.get("attempts"))) + attempts
    section["commits"] = list(_sequence(section.get("commits"))) + commits
    section["commit_groups"] = list(_sequence(section.get("commit_groups"))) + list(
        groups.values()
    )


def _reference_commit_streams(collector: _Collector, result: Mapping[str, Any]) -> None:
    """Index the retained streams a failed commit attempt points at.

    A failed attempt records the full ``git`` diagnostic in its own stdout and
    stderr files and carries only a bounded preview inline, so the paths are
    where the reason a commit was refused actually survives. Naming them in the
    result while indexing only the parent directory left the auditor unable to
    tell evidence it could read from a path it merely saw, and a path that is
    not on disk is recorded as an omission rather than passed over, so a missing
    stream is stated instead of being indistinguishable from one never written
    (ASI-137).
    """
    if not _commit_shows_failure(result):
        return
    for key in ("stderr_path", "stdout_path"):
        path = result.get(key)
        if not isinstance(path, str) or not path:
            continue
        stream = key.removesuffix("_path")
        if not Path(path).is_file():
            collector.omissions.record("commits", REASON_MISSING, path)
            continue
        collector.reference(
            path, kind="commit", detail=f"{_commit_record_label(result)} {stream}"
        )


def _commit_section(
    collector: _Collector,
    *,
    run_dir: Path,
    state_payload: Mapping[str, Any],
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What the run committed, from state projection plus per-commit results."""
    section = _projection(collector, state_payload, _COMMIT_STATE_KEYS)
    observed = _observed_repository_writes(turns)
    section["observed_head_transitions"] = _observed_head_transitions(observed)
    attempts: list[dict[str, Any]] = []
    try:
        directories = sorted(
            entry
            for entry in run_dir.iterdir()
            if entry.is_dir() and _is_commit_attempt_dir_name(entry.name)
        )
    except OSError:
        directories = []
    for directory in directories:
        result, reason = _read_json_file(directory / state.RESULT_FILE_NAME)
        entry: dict[str, Any] = {
            "name": directory.name,
            "reference": collector.reference(directory, kind="commit"),
        }
        if isinstance(result, Mapping):
            entry["result"] = collector.redact(result)
            _reference_commit_streams(collector, result)
        else:
            collector.omissions.record(
                "commits", reason or REASON_MALFORMED, str(directory)
            )
            entry["result"] = {"available": False, "reason": reason or REASON_MALFORMED}
        attempts.append(entry)
    section["attempts"] = attempts
    _reconcile_observed_commits(section, observed)
    return section


def _read_events(
    path: Path, run_id: str, collector: _Collector
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the run's slice of the shared event log, failing loudly on damage.

    The log is per-plan and append-only, so a run is the span from its own
    ``run_started`` to its terminal record, plus any record that names the run
    ID outright. Lines that will not parse are counted and digested rather than
    dropped: a damaged log is itself evidence about the run that wrote it.
    """
    summary: dict[str, Any] = {"available": False, UNPARSABLE_LINE_COUNT_KEY: 0}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        summary["reason"] = REASON_MISSING
        collector.omissions.record("events", REASON_MISSING, str(path))
        return [], summary
    except OSError:
        summary["reason"] = REASON_UNREADABLE
        collector.omissions.record("events", REASON_UNREADABLE, str(path))
        return [], summary

    summary["available"] = True
    records: list[dict[str, Any]] = []
    inside = False
    terminated = False
    rejected = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError, ValueError:
            rejected += 1
            collector.omissions.record("events", REASON_MALFORMED, line[:2000])
            continue
        if not isinstance(parsed, Mapping):
            rejected += 1
            collector.omissions.record("events", REASON_MALFORMED, line[:2000])
            continue
        payload = parsed.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        name = str(parsed.get("name") or "")
        named_run = payload.get("run_id")
        names_this_run = named_run == run_id
        if names_this_run and name == _RUN_STARTED_EVENT:
            inside = True
            terminated = False
        if not names_this_run and (not inside or terminated or named_run is not None):
            continue
        records.append(
            {
                "sequence": index,
                "name": name,
                "created_at": parsed.get("created_at"),
                "version": parsed.get("version"),
                "priority": _event_priority(name, payload),
                "payload": collector.redact(payload),
            }
        )
        if names_this_run and name in _TERMINAL_EVENT_NAMES:
            terminated = True
    summary[UNPARSABLE_LINE_COUNT_KEY] = rejected
    summary["reference"] = collector.reference(path, kind="event")
    return records, summary


def _delivered_mailbox_ids(
    event_records: Sequence[Mapping[str, Any]], run_id: str
) -> frozenset[str]:
    """Mailbox messages this run's own event stream says it took delivery of.

    Guidance is addressed to the plan, not to a run: a supervisor appends it
    without knowing which run — or which turn — will read it, so
    ``mailbox_appenders.append_guidance`` writes no ``run_id`` and the record cannot be
    claimed the way ``append_notice``'s is. Delivery is the tie, and delivery is
    already recorded: the runner appends a ``guidance-observed`` mailbox event
    naming the message it read into a turn. Joining on that accounts for the
    guidance a run acted on while leaving guidance meant for some other run
    where it belongs.
    """
    delivered: set[str] = set()
    for record in event_records:
        if record.get("name") != events.MAILBOX_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        if payload.get("action") != events.MAILBOX_ACTION_GUIDANCE_OBSERVED:
            continue
        if payload.get("run_id") not in (None, run_id):
            continue
        message_id = payload.get("message_id")
        if message_id is not None:
            delivered.add(str(message_id))
    return frozenset(delivered)


def _read_mailbox(
    path: Path,
    run_id: str,
    collector: _Collector,
    *,
    delivered_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the run's slice of the shared operator mailbox.

    Without it an audit sees the contract a session was materialized with and
    the recipe it actually ran, but nothing saying a human ruled between the
    two, and reads an authorized narrowing as an evaded gate.

    Only a record that names a run outright can be claimed by run ID; a reply,
    a receipt and the wait records join their exchange through ``to``,
    ``wait_id`` or ``closure_id``, so ownership is seeded from the named
    records and walked forward. ``delivered_ids`` adds the third seed a
    plan-addressed record needs, since guidance names no run and answers no
    question: the run's own event stream saying it read that message. A record
    that never joins belongs to another run or to the plan, and is counted
    rather than reported as withheld: an omission means evidence was kept back,
    not that it was someone else's.

    Guidance-delivery receipts seed their targets before that forward walk:
    explicit inbox reads have receipts but need not have runner delivery events.
    Missing targets are recorded as omissions instead of silently unscoped.

    The file is read directly rather than through :mod:`mailbox`, which is
    addressed by plan directory and takes a shared lock that a live supervisor
    could stall collection behind.
    """
    summary: dict[str, Any] = {
        "available": False,
        "delivered_count": 0,
        UNPARSABLE_LINE_COUNT_KEY: 0,
        "unscoped_count": 0,
        "counts": {},
    }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        summary["reason"] = REASON_MISSING
        collector.omissions.record("mailbox", REASON_MISSING, str(path))
        return [], summary
    except OSError:
        summary["reason"] = REASON_UNREADABLE
        collector.omissions.record("mailbox", REASON_UNREADABLE, str(path))
        return [], summary

    summary["available"] = True
    parsed_lines: list[tuple[int, Mapping[str, Any]]] = []
    rejected = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError, ValueError:
            rejected += 1
            collector.omissions.record("mailbox", REASON_MALFORMED, line[:2000])
            continue
        if not isinstance(parsed, Mapping):
            rejected += 1
            collector.omissions.record("mailbox", REASON_MALFORMED, line[:2000])
            continue
        parsed_lines.append((index, parsed))

    owned = {
        str(parsed["id"])
        for _, parsed in parsed_lines
        if parsed.get("run_id") == run_id and parsed.get("id") is not None
    }
    # Delivery receipts point backwards to plan-addressed guidance. Seed those
    # targets before the forward exchange walk, including explicit inbox reads
    # that do not emit a guidance-observed event (ASI-131).
    referenced_ids = frozenset(
        str(parsed["to"])
        for _, parsed in parsed_lines
        if parsed.get("run_id") == run_id
        and parsed.get("kind") == mailbox_vocabulary.KIND_GUIDANCE_DELIVERY
        and parsed.get("to") is not None
    )
    delivered_ids = delivered_ids | frozenset(
        str(parsed["to"])
        for _, parsed in parsed_lines
        if parsed.get("run_id") == run_id
        and parsed.get("kind") == mailbox_vocabulary.KIND_GUIDANCE_DELIVERY
        and parsed.get("delivery_state") in mailbox_vocabulary.GUIDANCE_DELIVERED_STATES
        and parsed.get("to") is not None
    )
    present_ids = {str(parsed["id"]) for _, parsed in parsed_lines if parsed.get("id")}
    missing_ids = sorted((referenced_ids | delivered_ids) - present_ids)
    summary["missing_reference_count"] = len(missing_ids)
    for identifier in missing_ids:
        collector.omissions.record("mailbox", REASON_MISSING, identifier)
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    unscoped = 0
    delivered = 0
    for index, parsed in parsed_lines:
        identifier = parsed.get("id")
        was_delivered = identifier is not None and str(identifier) in delivered_ids
        was_referenced = identifier is not None and str(identifier) in referenced_ids
        joined = any(parsed.get(key) in owned for key in _MAILBOX_JOIN_KEYS)
        if (
            not joined
            and not was_delivered
            and not was_referenced
            and parsed.get("run_id") != run_id
        ):
            unscoped += 1
            continue
        if identifier is not None:
            owned.add(str(identifier))
        kind = str(parsed.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        # The prose lives under ``text``, which redaction already treats as
        # application content, so the ruling's digest and length survive and
        # its bytes do not.
        record = dict(collector.redact(parsed))
        record["sequence"] = index
        if was_delivered and parsed.get("run_id") != run_id:
            # Say how a record naming no run reached this run's slice, so a
            # reader is not left inferring it from the absence of a ``run_id``.
            delivered += 1
            record["ownership"] = MAILBOX_OWNERSHIP_DELIVERED
        elif was_referenced and parsed.get("run_id") != run_id:
            record["ownership"] = "delivery-referenced"
        records.append(record)
    summary["delivered_count"] = delivered
    summary[UNPARSABLE_LINE_COUNT_KEY] = rejected
    summary["unscoped_count"] = unscoped
    summary["counts"] = counts
    summary["reference"] = collector.reference(path, kind="event")
    return records, summary


def _plan_section(
    collector: _Collector,
    *,
    plan_dir: Path,
    state_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Identity of the source plan directory and of the split it produced."""
    section: dict[str, Any] = {
        "reference": collector.reference(plan_dir, kind="plan"),
        "context": collector.file_digest(plan_dir / "context.md", kind="plan"),
        "manifest": collector.file_digest(plan_dir / "manifest.md", kind="plan"),
        # Every turn claims ``progress_appended``; without this the claim cites
        # nothing, and a journal that was never written goes unreported.
        "progress": collector.file_digest(plans.progress_path(plan_dir), kind="plan"),
        "metadata": _plan_metadata(collector, plan_dir),
    }
    for label, key in (("initial", "initial_snapshot"), ("final", "final_snapshot")):
        snapshot = state_payload.get(key)
        if isinstance(snapshot, Mapping):
            section[label] = {
                name: collector.redact(snapshot.get(name), key=name)
                for name in _SNAPSHOT_IDENTITY_KEYS
                if name in snapshot
            }
        else:
            section[label] = {"available": False, "reason": REASON_NOT_RECORDED}
    sessions = []
    for path in sorted(plan_dir.glob("session-*.md")):
        entry = collector.file_digest(path, kind="plan")
        entry["name"] = path.name
        sessions.append(entry)
    section["sessions"] = sessions
    return section


def _plan_metadata(collector: _Collector, plan_dir: Path) -> dict[str, Any]:
    """Read the durable workspace binding without recovering a transaction.

    ``plans.load_plan_metadata`` repairs a pending plan transaction as a side
    effect. An evidence collector must observe the run rather than change it,
    so the file is parsed directly.
    """
    path = plan_dir / plans.PLAN_METADATA_FILE_NAME
    payload, reason = _read_json_file(path)
    if payload is None:
        # A file holding the literal `null` parses to `None` with no reason, so
        # it is reported the way every other reader in this module reports an
        # unusable payload rather than as an omission with a null reason.
        reason = reason or REASON_MALFORMED
        collector.omissions.record("plan.metadata", reason, str(path))
        return {"available": False, "reason": reason}
    return {
        "available": True,
        "reference": collector.reference(
            path, kind="plan", digest=_sha256_text(json.dumps(payload, sort_keys=True))
        ),
        "content": collector.redact(payload),
    }


def _registry_section(
    collector: _Collector, state_payload: Mapping[str, Any]
) -> dict[str, Any]:
    """The workspace binding and repository registry the run executed under."""
    repositories = state_payload.get("repositories")
    if not isinstance(repositories, (list, tuple)):
        run_config = state_payload.get("run_config")
        if isinstance(run_config, Mapping):
            repositories = run_config.get("repositories")
    if not isinstance(repositories, (list, tuple)):
        return {"registered": False, "reason": REASON_NOT_RECORDED}
    return {
        "registered": True,
        "workspace_root": collector.locator(
            state_payload.get("workspace_root") or state_payload.get("cwd") or "."
        ),
        "digest": state_payload.get("registry_digest"),
        "repositories": collector.redact(list(repositories)),
    }


def _scopes_section(
    collector: _Collector, *, plan_dir: Path, aliases: Sequence[str]
) -> dict[str, Any]:
    """Per-session read and write scopes as the split plan declares them."""
    if not aliases:
        return {"available": False, "reason": REASON_NOT_RECORDED}
    sessions: list[dict[str, Any]] = []
    try:
        session_files = plans.discover_session_files(plan_dir)
    except plans.PlanError:
        collector.omissions.record("scopes", REASON_UNREADABLE, str(plan_dir))
        return {"available": False, "reason": REASON_UNREADABLE}
    paths = {session_file.path for session_file in session_files}
    # Historical audit evidence still uses zero-padded session IDs. Preserve
    # those exact numeric filenames without treating session-N.notes.md (or
    # other supporting documents) as sessions.
    paths.update(
        path
        for path in plan_dir.glob("session-*.md")
        if plans.LOOSE_SESSION_FILE_RE.fullmatch(path.name)
    )
    for path in sorted(paths):
        entry: dict[str, Any] = {"session": path.stem}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            entry["available"] = False
            entry["reason"] = REASON_UNREADABLE
            sessions.append(entry)
            continue
        try:
            scope = plans.parse_session_scope(text, tuple(aliases))
        except plans.PlanError, ValueError:
            entry["available"] = False
            entry["reason"] = REASON_MALFORMED
        else:
            entry.update(
                available=True, reads=list(scope.reads), writes=list(scope.writes)
            )
        sessions.append(entry)
    return {"available": True, "sessions": sessions}


def _reference_verification_stderr(
    collector: _Collector, result: Mapping[str, Any]
) -> None:
    """Index the stderr artifact each failed verification check points at.

    That artifact is where a rejected gate's full diagnostic survives: the check
    record's own copy is a preview the content rules withhold, and its inline
    detail is bounded. Naming the path without indexing it left the auditor
    unable to tell evidence it could read from a path it merely saw.
    """
    for check in _sequence(_mapping(result.get("verification")).get("checks")):
        entry = _mapping(check)
        if entry.get("status") == "pass":
            continue
        path = entry.get("stderr_artifact_path")
        if isinstance(path, str) and path:
            collector.reference(
                path, kind="artifact", detail=f"{entry.get('name')} stderr"
            )


def _turn_section(collector: _Collector, directory: Path) -> dict[str, Any]:
    """One turn's packet, prompt, harness metadata, and bounded streams."""
    result, reason = _read_json_file(directory / state.RESULT_FILE_NAME)
    turn: dict[str, Any] = {
        "directory": collector.reference(directory, kind="artifact"),
        "name": directory.name,
    }
    if isinstance(result, Mapping):
        _reference_verification_stderr(collector, result)
        turn["result"] = collector.redact(result)
        turn["anomalous"] = _turn_shows_failure(result)
        # Beside ``anomalous``, never folded into it: a harness that lost an
        # auxiliary transport and still produced a usable turn is degraded, and
        # counting that as an anomaly would contradict every successful run.
        turn["degraded"] = bool(result.get("harness_degradation"))
    else:
        collector.omissions.record("turns", reason or REASON_MALFORMED, str(directory))
        turn["result"] = {"available": False, "reason": reason or REASON_MALFORMED}
        turn["anomalous"] = True
        turn["degraded"] = False
    turn["logs"] = {
        "stdout": collector.file_preview(directory / "stdout.log", kind="harness"),
        "stderr": collector.file_preview(directory / "stderr.log", kind="harness"),
    }
    turn["last_message"] = collector.file_preview(
        directory / "last-message.txt", kind="harness"
    )
    return turn


def _verification_section(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Check-level outcomes across the run, without any command output."""
    sessions = []
    for turn in turns:
        result = turn.get("result")
        if not isinstance(result, Mapping):
            continue
        session_verification = result.get("verification")
        if not isinstance(session_verification, Mapping):
            continue
        sessions.append(
            {
                "turn": turn.get("name"),
                "session_id": result.get("session_id"),
                "verification": session_verification,
            }
        )
    return {"sessions": sessions, "session_count": len(sessions)}


def _moment(value: object) -> datetime | None:
    """Parse a recorded ISO timestamp, or ``None`` when it cannot be trusted."""
    if not isinstance(value, str) or not value:
        return None
    candidate = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _mailbox_asks(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every question in the run's mailbox slice, with how far it got.

    Mirrors :meth:`mailbox_snapshot.MailboxView.delivery_state_for`: a question is
    answered once a reply or a resolution targets it, and answered *and
    consumed* once a receipt names that closure. Only the consumed state
    proves the answer reached the run rather than sitting unread.
    """
    closures: dict[str, Mapping[str, Any]] = {}
    receipted: set[str] = set()
    for record in records:
        kind = record.get("kind")
        target = record.get("to")
        if kind in mailbox_vocabulary.CLOSURE_KINDS and target is not None:
            closures.setdefault(str(target), record)
        elif (
            kind == mailbox_vocabulary.KIND_RECEIPT
            and record.get("closure_id") is not None
        ):
            receipted.add(str(record["closure_id"]))
    asks: list[dict[str, Any]] = []
    for record in records:
        if (
            record.get("kind") != mailbox_vocabulary.KIND_QUESTION
            or record.get("id") is None
        ):
            continue
        closure = closures.get(str(record["id"]))
        closure_id = None if closure is None else closure.get("id")
        asks.append(
            {
                "id": str(record["id"]),
                "session_id": record.get("session_id"),
                "turn_index": record.get("turn_index"),
                "closure_id": closure_id,
                "closure_action": None if closure is None else closure.get("action"),
                "closed_at": None if closure is None else closure.get("created_at"),
                "consumed": closure_id is not None and str(closure_id) in receipted,
            }
        )
    return asks


def _raised_by_same_turn(ask: Mapping[str, Any], signal: Mapping[str, Any]) -> bool:
    """Whether ``ask`` was raised by the turn that emitted ``signal``.

    Both identifiers are required on both sides. A signal that names no turn
    cannot be shown to correspond to any particular question, and the whole
    point of the comparison is to avoid retiring one turn's ask on another
    turn's answer.
    """
    for key in ("session_id", "turn_index"):
        expected = signal.get(key)
        actual = ask.get(key)
        if expected is None or actual is None or str(expected) != str(actual):
            return False
    return True


def _status_section(
    collector: _Collector,
    state_payload: Mapping[str, Any],
    mailbox_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The terminal state projection, with an answered ask retired.

    ``action_required`` is the current unresolved intervention, not the run's
    history — the reading ``turn_execution.clear_live_action_required``
    already enforces on live state, where starting another turn or finishing
    unpaused proves the signal was answered. Neither proof is available to a
    run that stopped *on* the signal, so a paused run reaches the bundle still
    advertising an ask a human has already ruled on, and an auditor cannot tell
    it from a run genuinely waiting on one.

    A ruling the run consumed is the third proof. When every question the
    signal's own turn raised has been answered and receipted, and at least one
    of those rulings is no older than the signal itself, the projection is
    retired and the record it replaced is kept beside it. ``action_required``
    of ``null`` next to a standing ``paused`` then reads as what it is: a run
    merely paused, not one still asking. The ordering requirement is what keeps
    an earlier, unrelated exchange in the same turn from retiring a signal
    raised after it; without a ruling that postdates the signal, nothing shows
    a human ever saw it.
    """
    status = _projection(collector, state_payload, _STATUS_STATE_KEYS)
    signal = status.get("action_required")
    if not isinstance(signal, Mapping):
        return status
    if signal.get("reason") not in events.LIVE_ACTION_REQUIRED_REASONS:
        return status
    raised_at = _moment(signal.get("created_at"))
    if raised_at is None:
        return status
    asks = [
        ask
        for ask in _mailbox_asks(mailbox_records)
        if _raised_by_same_turn(ask, signal)
    ]
    if not asks or not all(ask["consumed"] for ask in asks):
        return status
    answered_after = [
        ask
        for ask in asks
        if (closed_at := _moment(ask["closed_at"])) is not None
        and closed_at >= raised_at
    ]
    if not answered_after:
        return status
    status["action_required"] = None
    status[_STATUS_ACTION_REQUIRED_SUPERSEDED] = {
        "superseded_by": SUPERSEDED_BY_CONSUMED_RULING,
        "questions": asks,
        "record": dict(signal),
    }
    return status


def _projection(
    collector: _Collector, state_payload: Mapping[str, Any], keys: Sequence[str]
) -> dict[str, Any]:
    return {
        key: collector.redact(state_payload.get(key), key=key)
        for key in keys
        if key in state_payload
    }


def _repository_section(
    collector: _Collector,
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Per-repository mutation fingerprints: paths and hashes, never content."""
    if not isinstance(snapshot, Mapping):
        return {"available": False, "reason": REASON_NOT_RECORDED}
    return {
        "available": True,
        "repositories": {
            str(name): collector.redact(fingerprint)
            for name, fingerprint in snapshot.items()
        },
    }


def _versions_section() -> dict[str, Any]:
    """What ASL and its skills were, by version and by content digest."""
    package_root = Path(__file__).resolve().parent
    return {
        "asl": __version__,
        "package": _tree_digest(package_root, patterns=("*.py",)),
        "skills": _tree_digest(package_root.parent / "skills", patterns=("*",)),
        "assurance_schema_version": assurance_models.ASSURANCE_SCHEMA_VERSION,
        "event_schema_version": events.EVENT_SCHEMA_VERSION,
    }


def _shed(
    payload: dict[str, Any],
    collector: _Collector,
    *,
    max_bytes: int,
) -> None:
    """Shrink the bundle to ``max_bytes``, giving up the least useful evidence first.

    Ordering is the whole point: heartbeat chatter goes before ordinary
    records, ordinary records before old healthy turns, and the terminal,
    failing, and guard evidence goes last, so a bundle that had to shed
    something still explains how the run ended.
    """
    records: list[dict[str, Any]] = payload["events"]["records"]
    messages: list[dict[str, Any]] = payload["mailbox"]["records"]
    turns: list[dict[str, Any]] = payload["turns"]
    protected = {
        id(turn)
        for turn in turns[-RECENT_TURN_COUNT:]
        + [t for t in turns if t.get("anomalous")]
    }

    def size() -> int:
        payload["omissions"] = collector.omissions.to_list()
        return len(assurance_models.canonical_json_bytes(payload))

    def drop_events(priority: int, batch: int) -> bool:
        candidates = [
            index
            for index, record in enumerate(records)
            if record.get("priority") == priority
        ]
        # Within a tier, the terminal and rejection records go last.
        candidates.sort(key=lambda i: records[i].get("name") in _LAST_SHED_EVENT_NAMES)
        doomed = sorted(candidates[:batch])
        for index in reversed(doomed):
            collector.omissions.record("events", REASON_SIZE_CAP, records[index])
            del records[index]
        return bool(doomed)

    def drop_mailbox(batch: int) -> bool:
        doomed = [
            index
            for index, message in enumerate(messages)
            if message.get("kind") in _SHEDDABLE_MAILBOX_KINDS
        ][:batch]
        for index in reversed(doomed):
            collector.omissions.record("mailbox", REASON_SIZE_CAP, messages[index])
            del messages[index]
        return bool(doomed)

    def drop_turn(only_unprotected: bool, batch: int) -> bool:
        doomed = [
            index
            for index, turn in enumerate(turns)
            if not (only_unprotected and id(turn) in protected)
        ][:batch]
        for index in reversed(doomed):
            collector.omissions.record(
                "turns", REASON_SIZE_CAP, turns[index].get("name")
            )
            del turns[index]
        return bool(doomed)

    steps: list[Callable[[int], bool]] = [
        lambda batch: collector.shrink_previews(
            MIN_PREVIEW_BYTES,
            only=[
                preview
                for turn in turns
                if id(turn) not in protected
                for preview in _turn_previews(turn)
            ],
        ),
        lambda batch: drop_events(2, batch),
        drop_mailbox,
        lambda batch: drop_events(1, batch),
        lambda batch: drop_turn(True, batch),
        lambda batch: collector.shrink_previews(MIN_PREVIEW_BYTES),
        lambda batch: collector.shrink_previews(0),
        lambda batch: drop_events(0, batch),
        lambda batch: drop_turn(False, batch),
    ]
    # Batches grow geometrically so a log with thousands of records is bounded
    # in a logarithmic number of serializations rather than one per record.
    for step in steps:
        batch = 1
        while size() > max_bytes:
            if not step(batch):
                break
            batch = min(batch * 2, 4096)
        if size() <= max_bytes:
            return


def _turn_previews(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    logs = turn.get("logs")
    if isinstance(logs, Mapping):
        found.extend(item for item in logs.values() if isinstance(item, dict))
    last_message = turn.get("last_message")
    if isinstance(last_message, dict):
        found.append(last_message)
    return found


def collect_audit_input(
    *,
    run_id: str,
    run_dir: Path | str,
    state_dir: Path | str,
    plan_dir: Path | str,
    workspace_root: Path | str | None = None,
    state_payload: Mapping[str, Any] | None = None,
    workspace_snapshot: Mapping[str, Any] | None = None,
    terminal_pending: bool = False,
    max_bytes: int = AUDIT_INPUT_MAX_BYTES,
) -> AuditInput:
    """Collect the bounded operational evidence bundle for one finished run.

    The bundle is a pure function of the run's persisted history and of the
    trees ASL itself ships, so two equivalent histories serialize to identical
    bytes — which is what lets the artifact's digest key the idempotent
    publication in :mod:`agent_session_loop.assurance_projection`. Nothing is
    collected by wall clock, and nothing is collected from the application
    under test: paths, digests, exit codes, timings, and guard results carry
    the control-plane story, while file contents and semantic diffs stay out.

    Set ``terminal_pending`` when the caller audits a run whose terminal record
    has not been written yet, so the checks that read for a missing terminal
    record do not report the caller's own ordering as a defect.
    """
    run_dir = Path(run_dir)
    state_dir = Path(state_dir)
    plan_dir = Path(plan_dir)
    roots = EvidenceRoots.build(
        run_dir=run_dir,
        state_dir=state_dir,
        plan_dir=plan_dir,
        workspace_root=workspace_root,
    )
    collector = _Collector(roots)
    # Registered first so it survives the evidence-index cap. A deterministic
    # check may not cite nothing, so this is the locator every check falls back
    # to when the artifact it read was shed or was never written.
    collector.reference(run_dir, kind="artifact", detail="run directory")

    state_path = state_dir / events.STATE_FILE_NAME
    if state_payload is None:
        loaded, reason = _read_json_file(state_path)
        if isinstance(loaded, Mapping):
            state_payload = loaded
        else:
            collector.omissions.record(
                "state", reason or REASON_MALFORMED, str(state_path)
            )
            state_payload = {}
    state_reference = collector.reference(state_path, kind="state")

    registry = _registry_section(collector, state_payload)
    aliases = [
        str(entry.get("alias"))
        for entry in (registry.get("repositories") or ())
        if isinstance(entry, Mapping) and entry.get("alias")
    ]
    event_records, event_summary = _read_events(
        state_dir / events.EVENTS_FILE_NAME, run_id, collector
    )
    mailbox_records, mailbox_summary = _read_mailbox(
        state_dir / mailbox_constants.MAILBOX_FILE_NAME,
        run_id,
        collector,
        delivered_ids=_delivered_mailbox_ids(event_records, run_id),
    )
    turns = [
        _turn_section(collector, directory) for directory in _turn_directories(run_dir)
    ]
    _mark_rejected_turns(turns, event_records)
    _mark_unredeemed_deferral_turns(
        turns, state_payload, event_records, run_id=str(run_id)
    )

    payload: dict[str, Any] = {
        "schema_version": AUDIT_INPUT_SCHEMA_VERSION,
        "run_id": run_id,
        "roots": roots.to_dict(),
        "plan": _plan_section(
            collector, plan_dir=plan_dir, state_payload=state_payload
        ),
        "registry": registry,
        "scopes": _scopes_section(collector, plan_dir=plan_dir, aliases=aliases),
        "configuration": collector.redact(state_payload.get("run_config") or {}),
        "state": {
            "reference": state_reference,
            "harness": state_payload.get("harness"),
            # Requested/effective semantics are explicit for the audit.  The
            # historical ``model`` aliases remain for consumers of older
            # bundles, while ``model_projection`` is authoritative per role.
            "requested_model": collector.redact(
                state_payload.get("model"), key="requested_model"
            ),
            "requested_model_source": state_payload.get("model_source"),
            "model": collector.redact(state_payload.get("model"), key="model"),
            "model_source": state_payload.get("model_source"),
            "observed_model": collector.redact(
                state_payload.get("observed_model"), key="observed_model"
            ),
            "observed_model_source": state_payload.get("observed_model_source"),
            # Where the two are reconciled per role: an unresolved request that
            # every turn of a role answered the same way resolves here, and a
            # review harness keeps its own record.
            "model_projection": collector.redact(
                state_payload.get("model_projection") or [], key="model_projection"
            ),
            "effort": state_payload.get("effort"),
            "turn_count": len(turns),
        },
        "turns": turns,
        "events": {
            "records": event_records,
            "summary": event_summary,
            "counts": _event_counts(event_records),
            "terminal_pending": bool(terminal_pending),
        },
        "mailbox": {"records": mailbox_records, "summary": mailbox_summary},
        "verification": _verification_section(turns),
        "review": _projection(collector, state_payload, _REVIEW_STATE_KEYS),
        "repair": _projection(collector, state_payload, _REPAIR_STATE_KEYS),
        "commits": _commit_section(
            collector, run_dir=run_dir, state_payload=state_payload, turns=turns
        ),
        "status": _status_section(collector, state_payload, mailbox_records),
        "repository": _repository_section(collector, workspace_snapshot),
        "versions": _versions_section(),
    }
    # Digest rather than merely name each artifact: a locator alone cannot tell
    # the auditor whether the file it points at is the one the run produced.
    for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS:
        candidate = run_dir / name
        if candidate.exists():
            collector.file_digest(candidate, kind="artifact")
    # The review cycle records these two paths and their digests, so an index
    # that never mentions them leaves the auditor unable to tell a review that
    # produced no artifact from one whose artifact was simply never indexed.
    # The absence is therefore recorded rather than skipped.
    for candidate in (
        review.final_review_output_path(plan_dir),
        review.final_review_artifact_path(plan_dir),
    ):
        collector.file_digest(candidate, kind="artifact", section="review.artifacts")

    payload["evidence"] = collector.evidence()
    payload["omissions"] = collector.omissions.to_list()
    _stamp_supersessions(turns)
    _shed(payload, collector, max_bytes=max_bytes)
    payload["evidence"] = collector.evidence()
    payload["omissions"] = collector.omissions.to_list()
    return AuditInput(payload=payload)


def tighten_audit_input(bundle: AuditInput, *, max_bytes: int) -> AuditInput:
    """Shed an already-collected bundle down to a smaller ceiling.

    Shed again rather than collected again: collecting reads the run directory,
    which by the time this is needed holds the audit's own input artifact and
    attempt records, so a second collection would describe a different run than
    the first one did — including a reference to its own predecessor. Shedding
    the payload already in hand keeps the result a strictly smaller view of the
    same evidence, given up in the same least-useful-first order, and leaves the
    canonical bundle on disk the one the deterministic verdicts were read from.

    The evidence index is not shed, so the locators an auditor may cite are the
    same either way.
    """
    payload = copy.deepcopy(bundle.payload)
    collector = _Collector(EvidenceRoots(roots=()))
    collector.omissions.seed(payload.get("omissions") or ())
    collector.adopt_previews(payload)
    _shed(payload, collector, max_bytes=max_bytes)
    payload["omissions"] = collector.omissions.to_list()
    return AuditInput(payload=payload)


def _event_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        name = str(record.get("name") or "")
        counts[name] = counts.get(name, 0) + 1
    return counts


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


#: Turn key naming the later clean completion that replaced a turn, stamped
#: at collection time so supersession survives the retry being shed.
TURN_SUPERSEDED_BY_KEY = "superseded_by"


def _stamp_supersessions(turns: Sequence[dict[str, Any]]) -> None:
    """Record on each superseded turn the name of the retry that replaced it.

    Supersession is judged on the full turn list at collection time because
    the size cap protects anomalous turns but not the clean retries that
    supersede them: once a retry is shed, the bundle alone would read the
    recovered attempt as unanswered counter-evidence (ASI-23).
    """
    for name, replacement in _supersessions(turns).items():
        replacement_name = replacement.get("name")
        if not isinstance(replacement_name, str):
            continue
        for turn in turns:
            if turn.get("name") == name:
                turn[TURN_SUPERSEDED_BY_KEY] = replacement_name


def _supersessions(
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Map each superseded turn's name to the clean completion that replaced it."""

    completed: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for turn in turns:
        if turn.get("anomalous") is True:
            continue
        result = _mapping(turn.get("result"))
        session_id = result.get("session_id")
        index = result.get("turn_index")
        if not isinstance(session_id, str) or not isinstance(index, int):
            continue
        if result.get("status_result") != "complete":
            continue
        latest = completed.get(session_id)
        if latest is None or index >= latest[0]:
            completed[session_id] = (index, turn)
    superseded: dict[str, Mapping[str, Any]] = {}
    for turn in turns:
        result = _mapping(turn.get("result"))
        session_id = result.get("session_id")
        index = result.get("turn_index")
        name = turn.get("name")
        if not isinstance(session_id, str) or not isinstance(index, int):
            continue
        if not isinstance(name, str):
            continue
        latest = completed.get(session_id)
        if latest is not None and latest[0] > index:
            superseded[name] = latest[1]
    return superseded


def _commit_record_label(payload: Mapping[str, Any]) -> str:
    """Name a commit record by the artifact it wrote, else by its index."""
    for key in ("artifact_dir", "result_path"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    index = payload.get("commit_index")
    return f"commit {index}" if index is not None else "an unnamed commit"


def _commit_shows_failure(payload: Mapping[str, Any]) -> bool:
    """Whether a commit record is a failure in the runner's own vocabulary.

    Most commit failures exit zero: ``unchanged-head``, ``missing-head``,
    ``dirty-after-commit`` and ``status-unavailable`` are all reached on a clean
    exit, and the runner reports them as ``success=False`` with the outcome
    token. The generic anomaly predicate reads neither field, so selecting on it
    alone skips every commit failure but the one that exited nonzero. Any
    ``status_result`` other than ``success`` counts, so an outcome ASL never
    writes is graded rather than silently read as a clean commit.
    """
    if payload.get("success") is False:
        return True
    status_result = payload.get("status_result")
    if isinstance(status_result, str) and status_result and status_result != "success":
        return True
    return _payload_shows_failure(payload)
