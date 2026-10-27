"""Exact factual corrections applied to session instructions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .authoring_transaction import PlanTransaction
from .contracts import COMPLETION_CONTRACT_HEADING
from .plans_errors import PlanError
from .plans_files import (
    canonicalize_path,
    read_text_and_line_ending,
    with_line_ending,
)
from .plans_handoff import HANDOFF_HEADING
from .prepare_models import (
    _HEADING_LINE_RE,
    BLOCKER_AMBIGUOUS,
    BLOCKER_CONTRACT_BODY,
    BLOCKER_HANDOFF_BODY,
    BLOCKER_NOT_FOUND,
    BLOCKER_STRUCTURAL,
    CORRECTION_ALREADY_APPLIED,
    CORRECTION_APPLIED,
    CORRECTION_BLOCKED,
    PrepareValidation,
    ProposedCorrection,
)


@dataclass(frozen=True)
class CorrectionOutcome:
    """The result of attempting one proposed factual correction."""

    session_id: str
    expected_text: str
    replacement_text: str
    status: str
    blocker: str | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status == CORRECTION_APPLIED

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "expected_text": self.expected_text,
            "replacement_text": self.replacement_text,
            "status": self.status,
            "blocker": self.blocker,
            "detail": self.detail,
        }


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level == 0 or level == len(stripped) or stripped[level] != " ":
        return 0
    return level


def _protected_line_ranges(lines: list[str]) -> tuple[tuple[int, int, str], ...]:
    """Line ranges (start, end, blocker) covering the contract and handoff sections."""
    ranges: list[tuple[int, int, str]] = []

    handoff_start: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            handoff_start = index
            break
    if handoff_start is not None:
        ranges.append((handoff_start, len(lines), BLOCKER_HANDOFF_BODY))

    body_end = handoff_start if handoff_start is not None else len(lines)
    for index in range(body_end):
        if lines[index].strip() == COMPLETION_CONTRACT_HEADING:
            end = body_end
            for later in range(index + 1, body_end):
                level = _heading_level(lines[later])
                if 0 < level <= 2:
                    end = later
                    break
            ranges.append((index, end, BLOCKER_CONTRACT_BODY))
            break

    return tuple(ranges)


def _match_line_span(text: str, offset: int, length: int) -> tuple[int, int]:
    start_line = text.count("\n", 0, offset)
    end_line = text.count("\n", 0, offset + max(length - 1, 0))
    return start_line, end_line


def _blocked(
    session_id: str,
    correction: ProposedCorrection,
    blocker: str,
    detail: str,
) -> CorrectionOutcome:
    return CorrectionOutcome(
        session_id=session_id,
        expected_text=correction.expected_text,
        replacement_text=correction.replacement_text,
        status=CORRECTION_BLOCKED,
        blocker=blocker,
        detail=detail,
    )


def _apply_one_correction(
    text: str, session_id: str, correction: ProposedCorrection
) -> tuple[str, CorrectionOutcome]:
    if _HEADING_LINE_RE.search(correction.expected_text) or _HEADING_LINE_RE.search(
        correction.replacement_text
    ):
        return text, _blocked(
            session_id,
            correction,
            BLOCKER_STRUCTURAL,
            "correction touches markdown headings; structural or "
            "design-changing edits are not applied automatically",
        )

    occurrences = text.count(correction.expected_text)
    # A replacement that extends its expected text still contains it once
    # applied, so "already applied" also means every remaining occurrence
    # of the expected text sits inside an occurrence of the replacement.
    already_applied = correction.replacement_text in text and (
        occurrences == 0
        or (
            correction.expected_text in correction.replacement_text
            and correction.expected_text
            not in text.replace(correction.replacement_text, "")
        )
    )
    if already_applied:
        return text, CorrectionOutcome(
            session_id=session_id,
            expected_text=correction.expected_text,
            replacement_text=correction.replacement_text,
            status=CORRECTION_ALREADY_APPLIED,
            detail="replacement text already present",
        )
    if occurrences == 0:
        return text, _blocked(
            session_id,
            correction,
            BLOCKER_NOT_FOUND,
            "expected original text not found in session file",
        )
    if occurrences > 1:
        return text, _blocked(
            session_id,
            correction,
            BLOCKER_AMBIGUOUS,
            f"expected original text matches {occurrences} times; "
            "corrections require exactly one match",
        )

    offset = text.find(correction.expected_text)
    lines = text.splitlines()
    start_line, end_line = _match_line_span(text, offset, len(correction.expected_text))
    for range_start, range_end, blocker in _protected_line_ranges(lines):
        if start_line < range_end and end_line >= range_start:
            section = (
                COMPLETION_CONTRACT_HEADING
                if blocker == BLOCKER_CONTRACT_BODY
                else HANDOFF_HEADING
            )
            return text, _blocked(
                session_id,
                correction,
                blocker,
                f"match lies inside the protected {section!r} section",
            )

    updated = text.replace(correction.expected_text, correction.replacement_text, 1)
    return updated, CorrectionOutcome(
        session_id=session_id,
        expected_text=correction.expected_text,
        replacement_text=correction.replacement_text,
        status=CORRECTION_APPLIED,
        detail=f"replaced at line {start_line + 1}",
    )


def apply_corrections(
    plan_dir: str | Path, validation: PrepareValidation
) -> tuple[CorrectionOutcome, ...]:
    """Apply every proposed factual correction that matches exactly once.

    Corrections are applied in order against the in-memory session text so
    multiple corrections to one file compose. A correction is applied only
    when its ``expected_text`` occurs exactly once and that occurrence lies
    outside the ``## Completion Contract`` and ``## Handoff`` sections;
    everything else is reported as a blocker outcome and left unapplied.
    Re-running with the same corrections yields ``already-applied`` outcomes
    and no file changes. All file writes commit through one atomic
    :class:`PlanTransaction`.
    """
    canonical_dir = canonicalize_path(plan_dir)
    outcomes: list[CorrectionOutcome] = []
    transaction = PlanTransaction(canonical_dir)

    for session in validation.sessions:
        if not session.corrections:
            continue
        session_path = canonical_dir / session.session_id.filename
        try:
            text, line_ending = read_text_and_line_ending(session_path)
        except FileNotFoundError as error:
            raise PlanError(f"missing session file: {session_path}") from error
        except (OSError, UnicodeDecodeError) as error:
            raise PlanError(f"cannot read {session_path}: {error}") from error

        original = text
        for correction in session.corrections:
            text, outcome = _apply_one_correction(
                text, session.session_id.value, correction
            )
            outcomes.append(outcome)
        if text != original:
            transaction.stage(
                session.session_id.filename, with_line_ending(text, line_ending)
            )

    transaction.commit()
    return tuple(outcomes)
