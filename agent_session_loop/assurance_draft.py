"""Draft an improvement proposal for one inbox issue, and stop there.

An issue in the assurance inbox says something about ASL is wrong. Turning that
into a change is a human decision, so this module produces exactly one artifact
— a Markdown proposal at the path the operator named — and nothing else. It
never splits a plan, prepares one, marks readiness, starts a loop, commits, or
runs any implementation command, and it imports none of the modules that could:
the chain from "an audit found something" to "ASL edited itself" is broken here
by what this module cannot reach, not by a rule someone has to remember.

The drafter runs under the same restricted read-only access profile the
supervisor audit uses, from a throwaway working directory, with read access to
the run directories holding the issue's evidence and to the ASL package and
skill trees it is reasoning about. Those roots are fingerprinted on both sides
of the turn, so a harness that wrote anything is refused rather than believed.

Validation happens before durability, and durability before the ledger. A reply
that is not a non-empty Markdown document carrying the five required sections is
refused outright; a valid one is created exclusively at the output path and
fullsynced; only then is ``improvement_plan_drafted`` appended. An existing
output path is refused before a harness is ever launched, so drafting twice
never silently overwrites the proposal a human is reading.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_models_artifacts, harnesses, state, state_plan_refs
from . import assurance_projection as projection
from .assurance_models_inbox import ImprovementPlanDraft
from .assurance_models_primitives import (
    AssuranceError,
    canonical_json_bytes,
    parse_issue_id,
)
from .assurance_models_vocab import MAX_LOCATOR_CHARS
from .harnesses import HARNESS_NAMES

#: Seconds granted to the single drafting turn. Drafting gets no retry: a
#: rejected reply is a refusal an operator sees, not a budget to spend twice.
DEFAULT_DRAFT_TIMEOUT_SECONDS = 600

#: The sections a proposal must carry, exactly once each. They are the shape a
#: human needs in order to decide, which is the only reason drafting exists.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Summary",
    "Problem and Evidence",
    "Proposed Changes",
    "Test Plan",
    "Assumptions",
)

#: Largest reply parsed into a proposal. A drafter answering with more than this
#: is not answering the contract, and parsing an unbounded reply is how
#: untrusted output starts costing memory instead of being rejected.
MAX_DRAFT_BYTES = 256 * 1024

#: Files hashed per read root when fingerprinting for mutation.
MAX_FINGERPRINT_FILES = 5000

#: Directory names never hashed: they are build products of this very process,
#: and a ``.pyc`` written while the drafter runs is not a mutation the drafter
#: caused.
_IGNORED_TREE_DIRS: frozenset[str] = frozenset({"__pycache__", ".git"})

REASON_INPUT_ERROR = "input-error"
REASON_UNKNOWN_ISSUE = projection.UNKNOWN_ISSUE
#: The named output already exists. Drafting never overwrites a proposal.
REASON_OUTPUT_EXISTS = "output-exists"
#: The output path cannot be created at all — no parent directory, no
#: permission, or a name the ledger record could not hold.
REASON_OUTPUT_UNWRITABLE = "output-unwritable"
#: The selected harness cannot run under the restricted read-only profile.
REASON_HARNESS_UNAVAILABLE = "harness-unavailable"
REASON_HARNESS_FAILED = "harness-failed"
REASON_TIMEOUT = "timeout"
#: The reply is not a non-empty Markdown proposal carrying the five sections.
REASON_MALFORMED_OUTPUT = "malformed-output"
#: Something under the read-only roots changed while the drafter ran.
REASON_MUTATION_DETECTED = "mutation-detected"
#: The proposal is on disk but the ledger would not accept the record of it.
REASON_RECORD_FAILED = ledger.ERROR_LEDGER_WRITE

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(\S*)[ \t]*$")


class DraftError(Exception):
    """Raised when a drafting request is refused, carrying its reason token."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DraftResult:
    """One drafted proposal: where it landed and what the ledger recorded."""

    requested_issue_id: str
    issue_id: str
    harness: str
    output_path: Path
    output_sha256: str
    drafted_at: str
    byte_count: int
    seq: int
    harness_model: str | None = None
    read_roots: tuple[Path, ...] = ()
    evidence_run_ids: tuple[str, ...] = ()

    @property
    def merged(self) -> bool:
        return self.requested_issue_id != self.issue_id


# ---------------------------------------------------------------------------
# What the drafter is allowed to read
# ---------------------------------------------------------------------------


def package_root() -> Path:
    return Path(__file__).resolve().parent


def skills_root() -> Path:
    return package_root().parent / "skills"


def evidence_run_dirs(
    run_ids: Iterable[str], *, state_dir: str | Path | None = None
) -> tuple[Path, ...]:
    """Return the run directories holding the evidence an issue references.

    A run ID that names no directory contributes nothing rather than failing the
    draft: run directories are pruned long before the ledger history that points
    at them, and a proposal can still be written from the issue's own record.
    """
    found: list[Path] = []
    for run_id in run_ids:
        for location in state_plan_refs.find_run_locations(run_id, state_dir=state_dir):
            run_dir = location.state_dir / state.RUNS_DIR_NAME / run_id
            if run_dir.is_dir():
                found.append(run_dir.resolve())
    return tuple(dict.fromkeys(found))


def draft_read_roots(run_dirs: Sequence[Path]) -> tuple[Path, ...]:
    """The directories the drafter may read, evidence first.

    Nothing writable is ever among them: the output path is written by ASL after
    the turn is over, and the workspace the audited run touched is not here at
    all.
    """
    roots = [Path(item).resolve() for item in run_dirs]
    roots.append(package_root())
    skills = skills_root()
    if skills.is_dir():
        roots.append(skills)
    return tuple(dict.fromkeys(roots))


def _tree_digest(root: Path) -> str | None:
    """Hash a tree by relative path and content, never by mtime."""
    if not root.is_dir():
        return None
    paths: list[Path] = []
    for candidate in root.rglob("*"):
        if _IGNORED_TREE_DIRS.intersection(candidate.parts):
            continue
        if candidate.is_file():
            paths.append(candidate)
    ordered = sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    digest = hashlib.sha256()
    for path in ordered[:MAX_FINGERPRINT_FILES]:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"\x00unreadable")
    return digest.hexdigest()


def fingerprint_roots(roots: Sequence[Path]) -> dict[str, str | None]:
    """Fingerprint every read root so a mutation can be proven, not assumed."""
    return {str(root): _tree_digest(root) for root in roots}


def _mutated_roots(
    before: Mapping[str, str | None], after: Mapping[str, str | None]
) -> tuple[str, ...]:
    return tuple(root for root, digest in before.items() if after.get(root) != digest)


# ---------------------------------------------------------------------------
# What the drafter is given to reason about
# ---------------------------------------------------------------------------


def build_issue_brief(
    inbox: projection.AssuranceProjection,
    issue: projection.Issue,
    *,
    requested_issue_id: str,
    run_dirs: Sequence[Path],
) -> dict[str, Any]:
    """Describe one canonical issue, its whole history, and its evidence.

    The brief is built from the ledger alone and names evidence by run directory
    and locator, so every claim the proposal makes can be traced back to a file
    a human can open.
    """
    finding = issue.finding
    directories = {path.name: str(path) for path in run_dirs}
    return {
        "requested_issue_id": requested_issue_id,
        "issue_id": issue.issue_id,
        "merged_from": list(issue.merged_from),
        "state": issue.state,
        "reopened_count": issue.reopened_count,
        "fingerprints": list(issue.fingerprints),
        "finding": {
            "schema_version": finding.schema_version,
            "category": finding.category,
            "component": finding.component,
            "invariant_key": finding.invariant_key,
            "lane": finding.lane,
            "severity": finding.severity,
            "summary": finding.summary,
            "recommendation": finding.recommendation,
            "evidence": list(finding.evidence),
            "localization": list(finding.localization),
        },
        "occurrences": [
            {
                "seq": occurrence.seq,
                "run_id": occurrence.run_id,
                "observed_at": occurrence.observed_at,
                "severity": occurrence.severity,
                "fingerprint": occurrence.fingerprint,
                "audit_artifact": occurrence.audit_artifact,
            }
            for occurrence in issue.occurrences
        ],
        "dispositions": [
            {
                "kind": item.kind,
                "result_state": item.result_state,
                "recorded_at": item.recorded_at,
                "note": item.note,
            }
            for item in issue.dispositions
        ],
        "existing_plans": [item.to_dict() for item in issue.plans],
        "runs": [_run_brief(inbox, run_id, directories) for run_id in issue.run_ids],
    }


def _run_brief(
    inbox: projection.AssuranceProjection,
    run_id: str,
    directories: Mapping[str, str],
) -> dict[str, Any]:
    audit = inbox.run(run_id)
    return {
        "run_id": run_id,
        "run_dir": directories.get(run_id),
        "state": None if audit is None else audit.state,
        "artifacts": (
            []
            if audit is None
            else [artifact.to_dict() for artifact in audit.summary.artifacts]
        ),
    }


DRAFT_PROMPT_TEMPLATE = """\
You are drafting an improvement proposal for one issue in the agent-session-loop
(ASL) assurance inbox. You are writing a document for a human to read and
decide on. Nothing you write is executed, and you are not implementing anything.

Order of work, which is not optional:

1. Read the issue record below: its finding, its whole observation history, its
   dispositions, and any proposal already drafted for it.
2. Open the evidence it references. Each affected run names its run directory
   and the artifacts inside it, such as `supervisor-audit.md` and
   `supervisor-audit-input.json`.
3. Only then read ASL source to work out what would have to change.

You have read access to exactly these directories and nothing else:

{read_roots}

Prohibited, without exception:

- Editing, creating, moving, or deleting any file, anywhere. You have read
  access only, and the directories above are fingerprinted before and after this
  turn; any change refuses the draft outright.
- Running any command: no `asl split`, `asl prepare`, `asl preflight`,
  `asl expand-ingest`, `asl run`, no git commit, no tests, no implementation.
- Applying the change you are proposing. A proposal is the deliverable.

Answer with the Markdown document itself and nothing else — no preamble, no
closing remarks, and do not wrap the document in a code fence. It must contain
these five ATX headings, once each, spelled exactly like this:

{section_list}

What each section is for:

- Summary: what you propose, in a few sentences a maintainer can act on.
- Problem and Evidence: what is actually wrong, with every claim tied to a
  specific artifact, locator, or source location you read.
- Proposed Changes: the concrete change, by file and by behavior. Say what the
  new invariant is, not merely that something should be improved.
- Test Plan: how a maintainer would prove the change works and that the
  original defect can no longer happen.
- Assumptions: what you could not verify, and what would invalidate the
  proposal if it turned out to be false.

Issue record:

```json
{brief}
```
"""


def build_draft_prompt(brief: Mapping[str, Any], *, read_roots: Sequence[Path]) -> str:
    """Render the prompt for the single drafting turn."""
    return DRAFT_PROMPT_TEMPLATE.format(
        read_roots="\n".join(f"- {root}" for root in read_roots),
        section_list="\n".join(f"- ## {name}" for name in REQUIRED_SECTIONS),
        brief=canonical_json_bytes(dict(brief)).decode("utf-8").strip(),
    )


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


def _unwrap_single_fence(lines: list[str]) -> list[str]:
    """Unwrap a reply that is one fenced block holding the whole document.

    A harness that fences its answer is answering the contract in a wrapper, not
    failing it. Unwrapping exactly one outer fence is the whole tolerance:
    anything else is judged as written.
    """
    if len(lines) < 2:
        return lines
    opening = _FENCE_RE.match(lines[0])
    closing = _FENCE_RE.match(lines[-1])
    if opening is None or closing is None:
        return lines
    if opening.group(1)[0] != closing.group(1)[0] or closing.group(2):
        return lines
    if opening.group(2).lower() not in {"", "markdown", "md"}:
        return lines
    return lines[1:-1]


def _normalize_heading(text: str) -> str:
    return " ".join(text.split()).rstrip(":").casefold()


def _headings(lines: Sequence[str]) -> list[str]:
    """Return the document's headings, ignoring anything inside a code fence."""
    found: list[str] = []
    fence: str | None = None
    for line in lines:
        match = _FENCE_RE.match(line)
        if match is not None:
            delimiter = match.group(1)[0]
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            found.append(_normalize_heading(heading.group(2)))
    return found


def validate_draft_markdown(text: str) -> str:
    """Return the proposal, or refuse the reply that was supposed to be one."""
    if not isinstance(text, str) or not text.strip():
        raise DraftError(REASON_MALFORMED_OUTPUT, "the drafter returned no output")
    if len(text.encode("utf-8")) > MAX_DRAFT_BYTES:
        raise DraftError(
            REASON_MALFORMED_OUTPUT,
            f"the drafter answered with more than the {MAX_DRAFT_BYTES}-byte cap "
            "on a proposal",
        )
    document = "\n".join(_unwrap_single_fence(text.strip().splitlines())).strip()
    if not document:
        raise DraftError(REASON_MALFORMED_OUTPUT, "the drafter returned no output")
    headings = _headings(document.splitlines())
    missing = [
        name
        for name in REQUIRED_SECTIONS
        if headings.count(_normalize_heading(name)) == 0
    ]
    if missing:
        raise DraftError(
            REASON_MALFORMED_OUTPUT,
            f"the proposal is missing the section(s) {', '.join(missing)}",
        )
    duplicated = [
        name
        for name in REQUIRED_SECTIONS
        if headings.count(_normalize_heading(name)) > 1
    ]
    if duplicated:
        raise DraftError(
            REASON_MALFORMED_OUTPUT,
            f"the proposal repeats the section(s) {', '.join(duplicated)}",
        )
    return f"{document}\n"


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def _resolve_output(output_path: str | Path) -> Path:
    target = Path(output_path).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve(strict=False)
    if len(str(target)) > MAX_LOCATOR_CHARS:
        raise DraftError(
            REASON_OUTPUT_UNWRITABLE,
            f"--output is {len(str(target))} characters, over the "
            f"{MAX_LOCATOR_CHARS}-character cap the ledger record can hold",
        )
    if target.exists() or target.is_symlink():
        raise DraftError(
            REASON_OUTPUT_EXISTS,
            f"{target} already exists; drafting never overwrites a proposal",
        )
    if not target.parent.is_dir():
        raise DraftError(
            REASON_OUTPUT_UNWRITABLE,
            f"the directory {target.parent} does not exist",
        )
    return target


def draft_improvement_plan(
    issue_id: str,
    *,
    harness: str,
    output_path: str | Path,
    harness_model: str | None = None,
    effort: str | None = None,
    timeout_seconds: int = DEFAULT_DRAFT_TIMEOUT_SECONDS,
    state_dir: str | Path | None = None,
    stream_callback: harnesses.StreamCallback | None = None,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
) -> DraftResult:
    """Draft one proposal for ``issue_id`` and record that it exists.

    Every refusal happens as early as it can be proven: an unusable request and
    an occupied output path cost no harness turn at all, and a mutation, a
    timeout, or a malformed reply is refused before anything is written. The
    ledger is appended only once the proposal is on disk and fullsynced, so a
    record always names a file a reader can open.
    """
    requested = parse_issue_id(issue_id)
    if harness not in HARNESS_NAMES:
        raise DraftError(
            REASON_INPUT_ERROR,
            f"--harness must be one of {', '.join(HARNESS_NAMES)}; got {harness!r}",
        )
    if timeout_seconds < 1:
        raise DraftError(
            REASON_INPUT_ERROR,
            f"--timeout must be a positive integer; got {timeout_seconds}",
        )
    target = _resolve_output(output_path)

    inbox = projection.read_projection(state_dir=state_dir)
    issue = inbox.issue(requested)
    if issue is None:
        raise DraftError(
            REASON_UNKNOWN_ISSUE, f"{requested} names no issue in the inbox"
        )

    unavailable = harnesses.audit_read_only_unavailable_reason(harness)
    if unavailable is not None:
        raise DraftError(
            REASON_HARNESS_UNAVAILABLE,
            f"{harness} cannot draft under the read-only profile: {unavailable}",
        )

    run_dirs = evidence_run_dirs(issue.run_ids, state_dir=state_dir)
    read_roots = draft_read_roots(run_dirs)
    brief = build_issue_brief(
        inbox, issue, requested_issue_id=requested, run_dirs=run_dirs
    )
    prompt = build_draft_prompt(brief, read_roots=read_roots)

    before = fingerprint_roots(read_roots)
    run_harness = harnesses.run_harness if _run_harness_fn is None else _run_harness_fn
    with tempfile.TemporaryDirectory(prefix="asl-draft-") as workdir:
        workdir_path = Path(workdir)
        try:
            result = run_harness(
                harness,
                cwd=workdir_path,
                plan_dir=None,
                prompt=prompt,
                unsafe_bypass=False,
                last_message_path=workdir_path / "last-message.txt",
                timeout=timeout_seconds,
                stream_callback=stream_callback,
                model=harness_model,
                effort=effort,
                access=harnesses.audit_read_only_access(read_roots),
            )
        except harnesses.HarnessError as error:
            raise DraftError(REASON_HARNESS_FAILED, str(error)) from error

    mutated = _mutated_roots(before, fingerprint_roots(read_roots))
    if mutated or target.exists():
        # Reported before the timeout and exit code, because a read-only turn
        # that changed something has already disproved the boundary the whole
        # draft rests on, whatever else it did.
        changed = list(mutated) + ([str(target)] if target.exists() else [])
        raise DraftError(
            REASON_MUTATION_DETECTED,
            f"{harness} ran under the read-only profile but "
            f"{', '.join(changed)} changed while it ran",
        )
    if result.timed_out:
        raise DraftError(
            REASON_TIMEOUT,
            f"{harness} did not answer within {timeout_seconds} seconds",
        )
    if result.returncode != 0:
        raise DraftError(
            REASON_HARNESS_FAILED,
            f"{harness} exited with code {result.returncode}",
        )

    document = validate_draft_markdown(result.last_message).encode("utf-8")
    try:
        assurance_models_artifacts.write_new_bytes(target, document)
    except AssuranceError as error:
        raise DraftError(
            REASON_OUTPUT_UNWRITABLE, f"could not write {target}: {error}"
        ) from error

    drafted_at = (datetime.now(UTC) if now is None else now()).isoformat()
    try:
        record = projection.record_improvement_plan_draft(
            ImprovementPlanDraft(
                issue_id=issue.issue_id,
                harness=harness,
                output_path=str(target),
                output_sha256=hashlib.sha256(document).hexdigest(),
                drafted_at=drafted_at,
                harness_model=harness_model,
            ),
            state_dir=state_dir,
        )
    except (
        projection.ProjectionError,
        ledger.LedgerError,
        AssuranceError,
        OSError,
    ) as error:
        # The proposal is already durable, so it is not deleted to make the
        # ledger look tidy: a validated document a human can read outranks a
        # history that never mentions it. Saying where it landed is what turns
        # the later ``output-exists`` refusal into an explanation.
        raise DraftError(
            getattr(error, "reason", REASON_RECORD_FAILED),
            f"the proposal was written to {target}, but recording it in the "
            f"ledger failed: {error}",
        ) from error
    draft = record.payload
    return DraftResult(
        requested_issue_id=requested,
        issue_id=draft.issue_id,
        harness=draft.harness,
        output_path=target,
        output_sha256=draft.output_sha256,
        drafted_at=draft.drafted_at,
        byte_count=len(document),
        seq=record.seq,
        harness_model=draft.harness_model,
        read_roots=read_roots,
        evidence_run_ids=tuple(path.name for path in run_dirs),
    )
