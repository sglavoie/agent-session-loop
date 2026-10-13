"""Assurance inbox records.

Observations, dispositions, merges, identity bindings, closeouts, and plan drafts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .assurance_models_findings import Finding, parse_finding
from .assurance_models_primitives import (
    AssuranceError,
    _duplicates,
    _optional_text,
    _require_digest,
    _require_identifier,
    _require_issue_ids,
    _require_locator,
    _require_number,
    _require_object,
    _require_sequence,
    _require_text,
    _require_timestamp,
    identity_key_for_issue,
    issue_id_for_identity_key,
    parse_identity_key,
    parse_issue_id,
)
from .assurance_models_vocab import (
    CLOSEOUT_OUTCOMES,
    DISPOSITION_KINDS,
    DISPOSITION_RESULT_STATE,
    IDENTITY_BINDING_SOURCES,
    MAX_ISSUE_REFERENCES,
    MAX_LOCATOR_CHARS,
    MAX_TOKEN_CHARS,
    CloseoutOutcome,
    DispositionKind,
    IdentityBindingSource,
    IssueState,
)
from .harnesses import HARNESS_NAMES, HarnessName
from .literals import as_literal

# ---------------------------------------------------------------------------
# Inbox records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueObservation:
    """One sighting of a finding in one run.

    The observation carries the run and the moment; the finding carries the
    identity. Keeping them apart is what lets the ledger record a hundred
    sightings of one defect without a hundred issues.
    """

    run_id: str
    finding: Finding
    observed_at: str
    audit_artifact: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, label="observation 'run_id'")
        if not isinstance(self.finding, Finding):
            raise AssuranceError("observation 'finding' must be a Finding")
        _require_timestamp(self.observed_at, label="observation 'observed_at'")
        if self.audit_artifact is not None:
            _require_locator(self.audit_artifact, label="observation 'audit_artifact'")

    @property
    def fingerprint(self) -> str:
        return self.finding.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "observed_at": self.observed_at,
            "audit_artifact": self.audit_artifact,
            "finding": self.finding.to_dict(),
        }


def parse_issue_observation(
    payload: object, *, label: str = "observation"
) -> IssueObservation:
    """Parse one issue observation, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "run_id",
            "fingerprint",
            "observed_at",
            "audit_artifact",
            "finding",
        ),
    )
    finding = parse_finding(data.get("finding"), label=f"{label} 'finding'")
    declared = data.get("fingerprint")
    if declared is not None and declared != finding.fingerprint:
        raise AssuranceError(
            f"{label} 'fingerprint' {declared!r} does not match its finding's "
            f"fingerprint {finding.fingerprint}"
        )
    return IssueObservation(
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        finding=finding,
        observed_at=_require_timestamp(
            data.get("observed_at"), label=f"{label} 'observed_at'"
        ),
        audit_artifact=(
            None
            if data.get("audit_artifact") is None
            else _require_locator(
                data.get("audit_artifact"), label=f"{label} 'audit_artifact'"
            )
        ),
    )


@dataclass(frozen=True)
class Disposition:
    """A human's decision about an issue, appended rather than applied in place."""

    issue_id: str
    kind: DispositionKind
    recorded_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        as_literal(
            self.kind, DISPOSITION_KINDS, "disposition 'kind'", error=AssuranceError
        )
        _require_timestamp(self.recorded_at, label="disposition 'recorded_at'")
        if self.note is not None:
            _require_text(self.note, label="disposition 'note'")

    @property
    def result_state(self) -> IssueState:
        """The issue state this disposition produces."""
        return DISPOSITION_RESULT_STATE[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "kind": self.kind,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def parse_disposition(payload: object, *, label: str = "disposition") -> Disposition:
    """Parse one disposition record, failing closed."""
    data = _require_object(
        payload, label=label, allowed_keys=("issue_id", "kind", "recorded_at", "note")
    )
    return Disposition(
        issue_id=parse_issue_id(data.get("issue_id")),
        kind=as_literal(
            data.get("kind"), DISPOSITION_KINDS, f"{label} 'kind'", error=AssuranceError
        ),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class IssueMerge:
    """An irreversible redirection of one issue's history into another."""

    source_issue_id: str
    target_issue_id: str
    recorded_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_issue_id", parse_issue_id(self.source_issue_id)
        )
        object.__setattr__(
            self, "target_issue_id", parse_issue_id(self.target_issue_id)
        )
        if self.source_issue_id == self.target_issue_id:
            raise AssuranceError(
                f"merge source and target are both {self.source_issue_id}; an "
                "issue cannot merge into itself"
            )
        _require_timestamp(self.recorded_at, label="merge 'recorded_at'")
        if self.note is not None:
            _require_text(self.note, label="merge 'note'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_issue_id": self.source_issue_id,
            "target_issue_id": self.target_issue_id,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def parse_issue_merge(payload: object, *, label: str = "merge") -> IssueMerge:
    """Parse one merge record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("source_issue_id", "target_issue_id", "recorded_at", "note"),
    )
    return IssueMerge(
        source_issue_id=parse_issue_id(data.get("source_issue_id")),
        target_issue_id=parse_issue_id(data.get("target_issue_id")),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class IssueKeptDistinct:
    """A durable statement that two reviewed issues describe separate defects.

    A merge is an event, and its negative is not simply the absence of one: a
    reviewer read a suggested pair and ruled that it is two defects, and without
    that ruling on disk every later reconciliation asks the same question again.

    What the ruling cannot say is "never ask again". It was reached about issues
    as they read at the time, so the identity digest each named issue had then is
    stored beside it, and the ruling holds only while those digests still
    describe them. An issue that absorbs a materially different sighting is a
    different question, and gets asked again rather than answered by a verdict
    nobody gave about it.
    """

    issue_ids: tuple[str, ...]
    identity_digests: tuple[str, ...]
    recorded_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issue_ids",
            _require_issue_ids(list(self.issue_ids), label="keep-distinct 'issue_ids'"),
        )
        if len(self.issue_ids) < 2:
            raise AssuranceError(
                "keep-distinct must name at least two issues; "
                f"got {len(self.issue_ids)}"
            )
        object.__setattr__(
            self,
            "identity_digests",
            tuple(
                _require_digest(digest, label="keep-distinct 'identity_digests' entry")
                for digest in self.identity_digests
            ),
        )
        if len(self.identity_digests) != len(self.issue_ids):
            raise AssuranceError(
                "keep-distinct must carry one identity digest per issue; got "
                f"{len(self.identity_digests)} digest(s) for "
                f"{len(self.issue_ids)} issue(s)"
            )
        _require_timestamp(self.recorded_at, label="keep-distinct 'recorded_at'")
        if self.note is not None:
            _require_text(self.note, label="keep-distinct 'note'")

    @property
    def reviewed_digests(self) -> tuple[tuple[str, str], ...]:
        """Each named issue beside the identity digest it was reviewed at."""
        return tuple(zip(self.issue_ids, self.identity_digests, strict=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_ids": list(self.issue_ids),
            "identity_digests": list(self.identity_digests),
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def parse_issue_kept_distinct(
    payload: object, *, label: str = "keep-distinct"
) -> IssueKeptDistinct:
    """Parse one keep-distinct record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("issue_ids", "identity_digests", "recorded_at", "note"),
    )
    digests = _require_sequence(
        data.get("identity_digests"),
        label=f"{label} 'identity_digests'",
        maximum=MAX_ISSUE_REFERENCES,
    )
    return IssueKeptDistinct(
        issue_ids=_require_issue_ids(
            data.get("issue_ids"), label=f"{label} 'issue_ids'"
        ),
        identity_digests=tuple(
            _require_digest(digest, label=f"{label} 'identity_digests' entry")
            for digest in digests
        ),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class IssueIdentityBinding:
    """A durable statement that one identity key names one canonical issue.

    Keys and issue IDs are both minted by ASL, so a binding never introduces a
    new name: it says which allocated issue owns an allocated key today. That is
    what lets a merged-away key keep resolving without the merge record having to
    be reinterpreted, and what keeps an agent from selecting an identity by
    rewording the defect it describes.
    """

    identity_key: str
    issue_id: str
    source: IdentityBindingSource
    bound_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity_key", parse_identity_key(self.identity_key))
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        as_literal(
            self.source,
            IDENTITY_BINDING_SOURCES,
            "binding 'source'",
            error=AssuranceError,
        )
        _require_timestamp(self.bound_at, label="binding 'bound_at'")
        if self.note is not None:
            _require_text(self.note, label="binding 'note'")
        minted = identity_key_for_issue(self.issue_id)
        if self.source == "allocation" and self.identity_key != minted:
            raise AssuranceError(
                f"binding {self.identity_key!r} claims to be the key allocated "
                f"with {self.issue_id}, whose key is {minted}"
            )

    @property
    def named_issue_id(self) -> str:
        """The issue the key itself names, which a merge may have moved."""
        return issue_id_for_identity_key(self.identity_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_key": self.identity_key,
            "issue_id": self.issue_id,
            "source": self.source,
            "bound_at": self.bound_at,
            "note": self.note,
        }


def parse_issue_identity_binding(
    payload: object, *, label: str = "binding"
) -> IssueIdentityBinding:
    """Parse one identity-binding record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("identity_key", "issue_id", "source", "bound_at", "note"),
    )
    return IssueIdentityBinding(
        identity_key=parse_identity_key(data.get("identity_key")),
        issue_id=parse_issue_id(data.get("issue_id")),
        source=as_literal(
            data.get("source"),
            IDENTITY_BINDING_SOURCES,
            f"{label} 'source'",
            error=AssuranceError,
        ),
        bound_at=_require_timestamp(data.get("bound_at"), label=f"{label} 'bound_at'"),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class Closeout:
    """A human's verdict on what a run's audit was worth."""

    run_id: str
    outcome: CloseoutOutcome
    recorded_at: str
    issue_ids: tuple[str, ...] = ()
    rework_minutes: float | None = None
    note: str | None = None
    plan_id: str | None = None
    replaces: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", tuple(parse_issue_id(item) for item in self.issue_ids)
        )
        duplicates = _duplicates(self.issue_ids)
        if duplicates:
            raise AssuranceError(
                f"closeout repeats issue id(s) {', '.join(duplicates)}"
            )
        if len(self.issue_ids) > MAX_ISSUE_REFERENCES:
            raise AssuranceError(
                f"closeout 'issue_ids' must hold at most {MAX_ISSUE_REFERENCES} "
                f"entries; got {len(self.issue_ids)}"
            )
        _require_identifier(self.run_id, label="closeout 'run_id'")
        as_literal(
            self.outcome, CLOSEOUT_OUTCOMES, "closeout 'outcome'", error=AssuranceError
        )
        _require_timestamp(self.recorded_at, label="closeout 'recorded_at'")
        if self.rework_minutes is not None:
            object.__setattr__(
                self,
                "rework_minutes",
                _require_number(
                    self.rework_minutes,
                    label="closeout 'rework_minutes'",
                    minimum=0.0,
                ),
            )
        if self.note is not None:
            _require_text(self.note, label="closeout 'note'")
        if self.plan_id is not None:
            _require_identifier(self.plan_id, label="closeout 'plan_id'")
        if self.replaces is not None:
            _require_identifier(self.replaces, label="closeout 'replaces'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "issue_ids": list(self.issue_ids),
            "rework_minutes": self.rework_minutes,
            "note": self.note,
            "plan_id": self.plan_id,
            "replaces": self.replaces,
        }


def parse_closeout(payload: object, *, label: str = "closeout") -> Closeout:
    """Parse one closeout record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "run_id",
            "outcome",
            "recorded_at",
            "issue_ids",
            "rework_minutes",
            "note",
            "plan_id",
            "replaces",
        ),
    )
    return Closeout(
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        outcome=as_literal(
            data.get("outcome"),
            CLOSEOUT_OUTCOMES,
            f"{label} 'outcome'",
            error=AssuranceError,
        ),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        issue_ids=_require_issue_ids(
            data.get("issue_ids", []), label=f"{label} 'issue_ids'"
        ),
        rework_minutes=(
            None
            if data.get("rework_minutes") is None
            else _require_number(
                data.get("rework_minutes"),
                label=f"{label} 'rework_minutes'",
                minimum=0.0,
            )
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        plan_id=(
            None
            if data.get("plan_id") is None
            else _require_identifier(data.get("plan_id"), label=f"{label} 'plan_id'")
        ),
        replaces=(
            None
            if data.get("replaces") is None
            else _require_identifier(data.get("replaces"), label=f"{label} 'replaces'")
        ),
    )


@dataclass(frozen=True)
class ImprovementPlanDraft:
    """A record that an improvement proposal was drafted for one issue.

    Drafting is deliberately inert: the record names the file and its digest so
    a reader can find and verify the proposal, and carries none of the prompt or
    response text that produced it.
    """

    issue_id: str
    harness: HarnessName
    output_path: str
    output_sha256: str
    drafted_at: str
    harness_model: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        as_literal(self.harness, HARNESS_NAMES, "draft 'harness'", error=AssuranceError)
        # The operator chooses ``--output`` freely, so an absolute path here is
        # ordinary and the run-relative locator rule does not apply.
        _require_text(
            self.output_path, label="draft 'output_path'", max_chars=MAX_LOCATOR_CHARS
        )
        _require_digest(self.output_sha256, label="draft 'output_sha256'")
        _require_timestamp(self.drafted_at, label="draft 'drafted_at'")
        if self.harness_model is not None:
            _require_text(
                self.harness_model,
                label="draft 'harness_model'",
                max_chars=MAX_TOKEN_CHARS,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "harness": self.harness,
            "output_path": self.output_path,
            "output_sha256": self.output_sha256,
            "drafted_at": self.drafted_at,
            "harness_model": self.harness_model,
        }


def parse_improvement_plan_draft(
    payload: object, *, label: str = "draft"
) -> ImprovementPlanDraft:
    """Parse one improvement-plan record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "issue_id",
            "harness",
            "output_path",
            "output_sha256",
            "drafted_at",
            "harness_model",
        ),
    )
    return ImprovementPlanDraft(
        issue_id=parse_issue_id(data.get("issue_id")),
        harness=as_literal(
            data.get("harness"),
            HARNESS_NAMES,
            f"{label} 'harness'",
            error=AssuranceError,
        ),
        output_path=_require_text(
            data.get("output_path"),
            label=f"{label} 'output_path'",
            max_chars=MAX_LOCATOR_CHARS,
        ),
        output_sha256=_require_digest(
            data.get("output_sha256"), label=f"{label} 'output_sha256'"
        ),
        drafted_at=_require_timestamp(
            data.get("drafted_at"), label=f"{label} 'drafted_at'"
        ),
        harness_model=_optional_text(
            data.get("harness_model"),
            label=f"{label} 'harness_model'",
            max_chars=MAX_TOKEN_CHARS,
        ),
    )
