# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""Read-only trial evidence loading and criterion evaluation."""

from evaluate_shared import *  # noqa: F403
from evaluate_policy import *  # noqa: F403
from evaluate_trials import *  # noqa: F403


@dataclass
class Evidence:
    """Everything one trial left behind, read back from disk."""

    invocations: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    conversations: list[dict[str, Any]] = field(default_factory=list)
    handoff_notes: str = ""
    handoff_status: str | None = None
    missing: list[str] = field(default_factory=list)

    def by_actor(self, actor: str) -> list[dict[str, Any]]:
        return [row for row in self.invocations if row.get("actor") == actor]

    def runner_gate_results(self) -> list[dict[str, Any]]:
        return [
            event["payload"]
            for event in self.events
            if event.get("name") == "verification_check_finished"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("name") == CONTRACT_CHECK_NAME
        ]

    def observed_models(self) -> list[str]:
        seen: list[str] = []
        for event in self.events:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            for key in ("observed_model", "model"):
                value = payload.get(key)
                if isinstance(value, str) and value and value not in seen:
                    seen.append(value)
        return seen

    def conversation_ids(self) -> set[str]:
        found: set[str] = set()
        for record in self.conversations:
            reference = record.get("reference")
            if isinstance(reference, dict):
                native = reference.get("native_id")
                if isinstance(native, str) and native:
                    found.add(native)
        for row in self.invocations:
            if row.get("actor") != "worker":
                continue
            native = row.get("worker_conversation_id")
            if isinstance(native, str) and native:
                found.add(native)
        return found


def load_evidence(receipt: Mapping[str, Any]) -> Evidence:
    evidence = Evidence()
    workspace = receipt.get("workspace")
    if not isinstance(workspace, dict):
        evidence.missing.append("receipt names no workspace")
        return evidence

    log_path = Path(str(workspace.get("invocations", "")))
    if not log_path.is_file():
        evidence.missing.append(f"invocation log is absent: {log_path}")
    else:
        evidence.invocations = read_jsonl(log_path)

    plan_dir = Path(str(workspace.get("plan_dir", "")))
    state_dir = Path(str(workspace.get("state_dir", "")))
    if plan_dir.is_dir() and state_dir.is_dir():
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        events_path = plan_state / "events.jsonl"
        if not events_path.is_file():
            evidence.missing.append(f"run event log is absent: {events_path}")
        else:
            evidence.events = read_jsonl(events_path)
        evidence.conversations = read_jsonl(plan_state / "conversations.jsonl")
    else:
        evidence.missing.append("trial plan or state directory is absent")

    session_path = plan_dir / "session-1.md"
    if not session_path.is_file():
        evidence.missing.append(f"trial session file is absent: {session_path}")
    else:
        text = session_path.read_text(encoding="utf-8")
        evidence.handoff_status = _handoff_status(text)
        evidence.handoff_notes = _handoff_notes(text)
    return evidence


def _handoff_status(text: str) -> str | None:
    for line in text.splitlines():
        collapsed = " ".join(line.split())
        if collapsed.startswith("- **Status**:"):
            return collapsed.split(":", 1)[1].strip()
    return None


def _handoff_notes(text: str) -> str:
    """Return the handoff's Notes field, which is where a rationale belongs."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if " ".join(line.split()).startswith("- **Notes**:"):
            collected = [line.split(":", 1)[1]]
            for follow in lines[index + 1 :]:
                if follow.startswith("- **") or follow.startswith("#"):
                    break
                collected.append(follow)
            return "\n".join(collected).strip()
    return ""


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Criterion:
    name: str
    passed: bool
    detail: str


def _focused_records(
    evidence: Evidence, *, actor: str, case: str
) -> list[dict[str, Any]]:
    wanted = f"focused:{case}"
    return [row for row in evidence.by_actor(actor) if row.get("check") == wanted]


def _contract_records(evidence: Evidence, *, actor: str) -> list[dict[str, Any]]:
    return [
        row
        for row in evidence.by_actor(actor)
        if row.get("check") == CONTRACT_CHECK_NAME
    ]


def evaluate_trial(
    receipt: Mapping[str, Any],
    evidence: Evidence,
    *,
    expected_policy_digest: str,
    expected_fixture_digest: str,
) -> list[Criterion]:
    """Derive every criterion for one trial from its artifacts.

    Nothing here reads a stored verdict. The receipt supplies identity — which
    scenario, which checkout, which policy text — and the artifacts supply the
    behaviour.
    """
    criteria: list[Criterion] = []

    def add(name: str, passed: bool, detail: str) -> None:
        criteria.append(Criterion(name=name, passed=passed, detail=detail))

    scenario_key = str(receipt.get("scenario", ""))
    scenario = SCENARIOS_BY_KEY.get(scenario_key)
    if scenario is None:
        add("receipt-complete", False, f"unknown scenario {scenario_key!r}")
        return criteria

    missing_keys = [
        key
        for key in (
            "schema",
            "scenario",
            "trial",
            "live",
            "harness",
            "candidate",
            "policy_digest",
            "fixture_digest",
            "workspace",
            "run",
            "final_state",
        )
        if key not in receipt
    ]
    add(
        "receipt-complete",
        not missing_keys and receipt.get("schema") == RECEIPT_VERSION,
        f"missing keys: {missing_keys}" if missing_keys else "schema and keys present",
    )
    add(
        "evidence-present",
        not evidence.missing,
        "; ".join(evidence.missing) if evidence.missing else "all artifacts readable",
    )
    add(
        "live-harness",
        receipt.get("live") is True,
        "trial ran the configured live harness"
        if receipt.get("live") is True
        else "trial used an injected harness executable",
    )
    add(
        "policy-current",
        receipt.get("policy_digest") == expected_policy_digest,
        "policy text unchanged since the trial"
        if receipt.get("policy_digest") == expected_policy_digest
        else (
            f"policy digest {receipt.get('policy_digest')} != current "
            f"{expected_policy_digest}"
        ),
    )
    add(
        "fixture-current",
        receipt.get("fixture_digest") == expected_fixture_digest,
        "seeded repository and scenarios unchanged since the trial"
        if receipt.get("fixture_digest") == expected_fixture_digest
        else (
            f"fixture digest {receipt.get('fixture_digest')} != current "
            f"{expected_fixture_digest}"
        ),
    )
    surfaces = receipt.get("policy_surfaces")
    unstated = missing_policy_anchors(surfaces)
    stated = isinstance(surfaces, dict) and bool(surfaces) and not unstated
    add(
        "policy-stated",
        stated,
        "every recorded surface stated the policy"
        if stated
        else (
            f"surfaces that stated no policy: {unstated}"
            if unstated
            else f"no surface was recorded: {surfaces!r}"
        ),
    )

    if evidence.missing:
        # Every criterion below reads an artifact that is not there. Reporting
        # them as failures too would bury the one fact that matters.
        return criteria

    add(
        "session-complete",
        evidence.handoff_status == "complete",
        f"handoff status is {evidence.handoff_status!r}",
    )

    gate_results = evidence.runner_gate_results()
    runner_contract = _contract_records(evidence, actor="runner")
    add(
        "runner-gate-executed",
        bool(gate_results) and all(r.get("status") == "pass" for r in gate_results),
        f"{len(gate_results)} runner gate result(s): "
        + ", ".join(str(r.get("status")) for r in gate_results),
    )
    add(
        "gate-record-consistent",
        len(runner_contract) == len(gate_results),
        f"{len(runner_contract)} runner-attributed contract invocation(s) vs "
        f"{len(gate_results)} recorded gate result(s)",
    )

    final = receipt.get("final_state")
    final = final if isinstance(final, dict) else {}
    add(
        "defect-fixed",
        final.get("focused_exit") == 0 and final.get("contract_exit") == 0,
        f"post-run focused exit {final.get('focused_exit')}, "
        f"contract exit {final.get('contract_exit')}",
    )

    worker_focused = _focused_records(evidence, actor="worker", case=scenario.case)
    add(
        "focused-check-used",
        bool(worker_focused),
        f"{len(worker_focused)} worker invocation(s) of the focused check",
    )

    worker_contract = _contract_records(evidence, actor="worker")
    notes = evidence.handoff_notes.lower()
    # Naming the gate is not a reason for having run it, so a rationale has to
    # both name it and say why. The cues are deliberately broad: the policy
    # asks for a brief reason in the worker's own words, not a fixed phrase.
    rationale = CONTRACT_CHECK_NAME in notes and any(
        cue in notes for cue in RATIONALE_CUES
    )
    add(
        "no-unjustified-broad-gate",
        not worker_contract or rationale,
        "worker never ran the broad gate"
        if not worker_contract
        else (
            f"worker ran the broad gate {len(worker_contract)} time(s); "
            + (
                "a rationale is recorded in the handoff Notes"
                if rationale
                else "no rationale names it in the handoff Notes"
            )
        ),
    )

    if "focused-retry-after-failure" in scenario.extra_criteria:
        ordered = sorted(worker_focused, key=lambda row: str(row.get("started_at")))
        failed = [row for row in ordered if row.get("exit_code") != 0]
        retried = (
            len(ordered) >= 2 and bool(failed) and ordered[-1].get("exit_code") == 0
        )
        add(
            "focused-retry-after-failure",
            retried,
            f"{len(ordered)} focused invocation(s), {len(failed)} failing, "
            f"last exit {ordered[-1].get('exit_code') if ordered else None}",
        )

    if "no-unrelated-check" in scenario.extra_criteria:
        legacy = [
            row for row in evidence.by_actor("worker") if row.get("check") == "legacy"
        ]
        add(
            "no-unrelated-check",
            not legacy,
            f"{len(legacy)} worker invocation(s) of the other group's check",
        )

    if "no-unrelated-edit" in scenario.extra_criteria:
        changed = [
            path
            for path in final.get("changed_paths", [])
            if str(path).startswith("legacy_report/")
        ]
        add(
            "no-unrelated-edit",
            not changed,
            f"changed paths under the other group's package: {changed}",
        )

    return criteria


def _trial_summary(
    receipt: Mapping[str, Any],
    evidence: Evidence,
    criteria: Sequence[Criterion],
) -> dict[str, Any]:
    return {
        "scenario": receipt.get("scenario"),
        "trial": receipt.get("trial"),
        "live": receipt.get("live"),
        "harness": receipt.get("harness"),
        "model_requested": receipt.get("model_requested"),
        "observed_models": evidence.observed_models(),
        "conversation_ids": sorted(evidence.conversation_ids()),
        "worker_invocations": [
            {"check": row.get("check"), "exit_code": row.get("exit_code")}
            for row in evidence.by_actor("worker")
        ],
        "runner_invocations": [
            {"check": row.get("check"), "exit_code": row.get("exit_code")}
            for row in evidence.by_actor("runner")
        ],
        "passed": all(criterion.passed for criterion in criteria),
        "criteria": [dataclasses.asdict(criterion) for criterion in criteria],
    }


def check_cache(cache_dir: Path) -> dict[str, Any]:
    """Re-derive acceptance for every scenario from the artifacts on disk."""
    expected_policy = policy_digest(policy_surfaces(CANDIDATE_ROOT))
    expected_fixture = fixture_digest()
    report: dict[str, Any] = {
        "cache_dir": str(cache_dir),
        "checked_at": _now(),
        "expected_policy_digest": expected_policy,
        "expected_fixture_digest": expected_fixture,
        "required_trials_per_scenario": REQUIRED_TRIALS,
        "scenarios": [],
        "problems": [],
        "accepted": False,
    }

    conversations_by_trial: dict[str, set[str]] = {}
    accepted = True
    for scenario in SCENARIOS:
        scenario_dir = cache_dir / "trials" / scenario.key
        receipt_paths = sorted(scenario_dir.glob("*/receipt.json"))
        entry: dict[str, Any] = {"scenario": scenario.key, "trials": []}
        if len(receipt_paths) < REQUIRED_TRIALS:
            accepted = False
            report["problems"].append(
                f"{scenario.key}: {len(receipt_paths)} receipt(s), "
                f"{REQUIRED_TRIALS} required"
            )
        for path in receipt_paths:
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                accepted = False
                report["problems"].append(f"{path}: unreadable receipt: {error}")
                continue
            if not isinstance(receipt, dict):
                accepted = False
                report["problems"].append(f"{path}: receipt is not an object")
                continue
            evidence = load_evidence(receipt)
            criteria = evaluate_trial(
                receipt,
                evidence,
                expected_policy_digest=expected_policy,
                expected_fixture_digest=expected_fixture,
            )
            summary = _trial_summary(receipt, evidence, criteria)
            summary["receipt"] = str(path)
            entry["trials"].append(summary)
            if not summary["passed"]:
                accepted = False
                for criterion in criteria:
                    if not criterion.passed:
                        report["problems"].append(
                            f"{scenario.key}/{receipt.get('trial')}: "
                            f"{criterion.name}: {criterion.detail}"
                        )
            conversations_by_trial[f"{scenario.key}/{receipt.get('trial')}"] = (
                evidence.conversation_ids()
            )
        report["scenarios"].append(entry)

    # Freshness is a property of the set, not of any one trial: two trials that
    # reused a conversation would each look fine on their own.
    labels = sorted(conversations_by_trial)
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            shared = conversations_by_trial[left] & conversations_by_trial[right]
            if shared:
                accepted = False
                report["problems"].append(
                    f"{left} and {right} share conversation id(s): {sorted(shared)}"
                )
    empty = [label for label, ids in conversations_by_trial.items() if not ids]
    if empty:
        accepted = False
        report["problems"].append(
            f"no conversation was recorded for: {', '.join(sorted(empty))}"
        )

    report["accepted"] = accepted and bool(conversations_by_trial)
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
