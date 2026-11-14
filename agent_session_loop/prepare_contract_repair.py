"""Bounded, guarded repair of validator-reported completion-contract problems.

When validation reports ``contract_problems``, ``asl prepare`` can hand them to
one repair turn instead of stopping. The turn may edit only ``context.md`` and
existing ``session-<id>.md`` files in the plan directory. Everything else is
enforced here rather than trusted:

- every plan-directory file is snapshotted first, and any change outside the
  allowed files, any added or removed file, a changed session id set, an
  unparseable completion contract, or a changed ``## Handoff`` section restores
  the whole snapshot and refuses the round;
- the codebase's Git ``HEAD`` and ``status --porcelain`` must be identical
  after the turn, because a change there cannot be undone safely;
- each round is appended to ``prepare-contract-repairs.json`` with the
  problems it answered and each changed file's before and after digest.

The repairer may also answer that a problem needs a human decision; prepare
then stops with that question instead of guessing. The source plan file is
never edited: repairs live in the split plan directory.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts, durability, harnesses, plans_registry
from .plans_discovery import discover_session_files
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_handoff import HANDOFF_HEADING
from .prepare_models import ContractProblem
from .prepare_validator import _extract_json_object

DEFAULT_CONTRACT_REPAIR_ROUNDS = 2
PREPARE_CONTRACT_REPAIRS_FILENAME = "prepare-contract-repairs.json"
PREPARE_CONTRACT_REPAIRS_VERSION = 1


@dataclass(frozen=True)
class ContractRepairPacket:
    """Everything one repair turn is told."""

    plan_dir: Path
    codebase_root: Path
    round: int
    problems: tuple[tuple[str, ContractProblem], ...]
    allowed_files: tuple[str, ...]

    def render(self) -> str:
        lines = [
            "# ASL preparation contract repair",
            "",
            (
                "Validators reviewed this split plan against the codebase and "
                "reported completion-contract problems. Edit the plan so each "
                "problem is resolved. The plan directory is "
                f"{self.plan_dir}; the codebase is {self.codebase_root} "
                "(read it as needed, never modify it)."
            ),
            "",
            "## Rules",
            "",
            (
                "- Edit only these files in the plan directory: "
                + ", ".join(self.allowed_files)
                + ". A problem in one session may need a matching edit in "
                "another session or in context.md; make it."
            ),
            (
                "- Keep every session heading, session id and `## Handoff` "
                "section exactly as it is. Keep each `## Completion Contract` "
                "a `### Done when` list plus a `### Verify` fenced json block "
                "that still parses."
            ),
            (
                "- Resolve problems by making the plan precise: specify the "
                "missing interface, rule or test. Do not narrow scope, drop "
                "required behaviour, or delete a check just to silence a "
                "problem."
            ),
            (
                "- When resolving a problem needs a product decision only the "
                "plan's owner can make, do not guess: leave it and report it "
                "under needs_decision with one concrete question."
            ),
            "",
            f"## Problems (repair round {self.round})",
            "",
        ]
        for session_id, problem in self.problems:
            lines.append(f"- Session {session_id}: {problem.message}")
            lines.append(f"  Suggested fix: {problem.suggested_fix}")
        lines.extend(
            [
                "",
                "## Answer",
                "",
                (
                    "After editing, reply with one JSON object and nothing "
                    'else: {"repaired": ["<session id>", ...], '
                    '"needs_decision": [{"session_id": "<id>", '
                    '"question": "<one question>"}]}.'
                ),
            ]
        )
        return "\n".join(lines)


ContractRepairer = Callable[[ContractRepairPacket], object]


@dataclass(frozen=True)
class NeedsDecision:
    session_id: str
    question: str


@dataclass(frozen=True)
class ContractRepairOutcome:
    round: int
    changed_files: tuple[str, ...]
    needs_decision: tuple[NeedsDecision, ...] = field(default_factory=tuple)


def problem_signature(
    problems: Iterable[tuple[str, ContractProblem]],
) -> frozenset[tuple[str, str]]:
    """The identity two rounds compare to detect a repair that changed nothing."""
    return frozenset((session_id, problem.message) for session_id, problem in problems)


def launch_contract_repair(
    harness_name: str,
    packet: ContractRepairPacket,
    *,
    model: str | None = None,
    effort: str | None = None,
    timeout: int | None = None,
) -> object:
    """Run one non-bypass worker turn whose writable root is the plan directory."""
    with tempfile.TemporaryDirectory(prefix="asl-prepare-repair-") as tmp:
        last_message_path = Path(tmp) / "repair-last-message.txt"
        try:
            result = harnesses.run_harness(
                harness_name,
                cwd=packet.plan_dir,
                plan_dir=packet.plan_dir,
                prompt=packet.render(),
                # Repairs edit plan text only. They never inherit the
                # implementation worker's unsafe-bypass setting.
                unsafe_bypass=False,
                last_message_path=last_message_path,
                timeout=timeout,
                model=model,
                effort=effort,
            )
        except harnesses.HarnessError as error:
            raise PlanError(
                f"contract repair harness {harness_name!r} failed to launch: {error}"
            ) from error
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        detail = f": {lines[-1].strip()}" if lines else ""
        raise PlanError(
            f"contract repair harness {harness_name!r} exited "
            f"{result.returncode}{detail}"
        )
    if not result.last_message.strip():
        raise PlanError(f"contract repair harness {harness_name!r} returned no output")
    return _extract_json_object(result.last_message)


def _allowed_files(plan_dir: Path) -> tuple[str, ...]:
    return (
        "context.md",
        *(session.filename for session in discover_session_files(plan_dir)),
    )


def _snapshot(plan_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(plan_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(plan_dir).as_posix()] = path.read_bytes()
    return files


def _restore(plan_dir: Path, snapshot: dict[str, bytes]) -> None:
    current = _snapshot(plan_dir)
    for name in current.keys() - snapshot.keys():
        (plan_dir / name).unlink()
    for name, data in snapshot.items():
        if current.get(name) != data:
            durability.replace_bytes(plan_dir / name, data)


def _codebase_state(root: Path) -> tuple[str, str] | None:
    """``HEAD`` and porcelain status, or ``None`` outside a Git worktree."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if head.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PlanError(f"cannot read codebase state for repair: {error}") from error
    return head.stdout.strip(), status.stdout


def _handoff_section(text: str) -> str:
    index = text.find(f"\n{HANDOFF_HEADING}\n")
    return "" if index < 0 else text[index:]


def _parse_answer(answer: object) -> tuple[NeedsDecision, ...]:
    if not isinstance(answer, dict):
        raise PlanError("contract repair answer is not a JSON object")
    raw = answer.get("needs_decision", [])
    if not isinstance(raw, list):
        raise PlanError("contract repair needs_decision must be a list")
    decisions: list[NeedsDecision] = []
    for entry in raw:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("session_id"), str)
            or not isinstance(entry.get("question"), str)
            or not entry["question"].strip()
        ):
            raise PlanError(
                "contract repair needs_decision entries need session_id and question"
            )
        decisions.append(NeedsDecision(entry["session_id"], entry["question"].strip()))
    return tuple(decisions)


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _record_round(
    plan_dir: Path,
    packet: ContractRepairPacket,
    before: dict[str, bytes],
    after: dict[str, bytes],
    changed: tuple[str, ...],
    decisions: tuple[NeedsDecision, ...],
) -> None:
    path = plan_dir / PREPARE_CONTRACT_REPAIRS_FILENAME
    rounds: list[object] = []
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        existing = None
    if (
        isinstance(existing, dict)
        and existing.get("version") == PREPARE_CONTRACT_REPAIRS_VERSION
        and isinstance(existing.get("rounds"), list)
    ):
        rounds = list(existing["rounds"])
    rounds.append(
        {
            "round": packet.round,
            "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(
                timespec="seconds"
            ),
            "problems": [
                {"session_id": session_id, **problem.to_dict()}
                for session_id, problem in packet.problems
            ],
            "changed_files": [
                {
                    "path": name,
                    "before_sha256": _digest(before.get(name)),
                    "after_sha256": _digest(after.get(name)),
                }
                for name in changed
            ],
            "needs_decision": [
                {"session_id": decision.session_id, "question": decision.question}
                for decision in decisions
            ],
        }
    )
    document = {"version": PREPARE_CONTRACT_REPAIRS_VERSION, "rounds": rounds}
    durability.replace_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def run_contract_repair(
    plan_dir: str | Path,
    problems: tuple[tuple[str, ContractProblem], ...],
    *,
    codebase_root: str | Path,
    round_number: int,
    repairer: ContractRepairer,
) -> ContractRepairOutcome:
    """Run one guarded repair round; restore the plan and raise on any violation."""
    canonical_dir = canonicalize_path(plan_dir)
    root = canonicalize_path(codebase_root)
    allowed = _allowed_files(canonical_dir)
    session_ids = tuple(session.id for session in discover_session_files(canonical_dir))
    packet = ContractRepairPacket(
        plan_dir=canonical_dir,
        codebase_root=root,
        round=round_number,
        problems=problems,
        allowed_files=allowed,
    )
    before = _snapshot(canonical_dir)
    codebase_before = _codebase_state(root)
    try:
        answer = repairer(packet)
        decisions = _parse_answer(answer)
    except Exception:
        _restore(canonical_dir, before)
        raise
    if _codebase_state(root) != codebase_before:
        _restore(canonical_dir, before)
        raise PlanError(
            f"contract repair changed the codebase at {root}; inspect it before "
            "resuming (the plan directory was restored)"
        )
    after = _snapshot(canonical_dir)
    changed = tuple(
        sorted(
            name
            for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
        )
    )
    violation = _violation(canonical_dir, before, after, changed, allowed, session_ids)
    if violation is not None:
        _restore(canonical_dir, before)
        raise PlanError(
            f"contract repair refused: {violation}; the plan directory was restored"
        )
    _record_round(canonical_dir, packet, before, after, changed, decisions)
    return ContractRepairOutcome(round_number, changed, decisions)


def _violation(
    plan_dir: Path,
    before: dict[str, bytes],
    after: dict[str, bytes],
    changed: tuple[str, ...],
    allowed: tuple[str, ...],
    session_ids: tuple[str, ...],
) -> str | None:
    registry = plans_registry.read_repository_registry(plan_dir)
    outside = [name for name in changed if name not in allowed]
    if outside:
        return "it changed files it may not edit: " + ", ".join(outside)
    if tuple(session.id for session in discover_session_files(plan_dir)) != session_ids:
        return "it changed the plan's session ids"
    for session_id in session_ids:
        name = f"session-{session_id}.md"
        if name not in changed:
            continue
        old_text = before[name].decode("utf-8")
        new_text = after[name].decode("utf-8", errors="replace")
        if _handoff_section(new_text) != _handoff_section(old_text):
            return f"it changed the {HANDOFF_HEADING} section of {name}"
        try:
            contract = contracts.parse_completion_contract(
                new_text, session_id=session_id, repository_registry=registry
            )
        except PlanError as error:
            return f"{name} no longer has a parseable completion contract ({error})"
        if contract is None:
            return f"{name} no longer has a completion contract"
    return None
