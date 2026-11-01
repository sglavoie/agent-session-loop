"""Opt-in real-CLI acceptance of cross-harness review, repair, and re-review."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from agent_session_loop import harness_compatibility, state
from agent_session_loop.harnesses import HarnessName
from tests.asl_live_lifecycle import launch, supervision
from tests.asl_live_lifecycle_process import Commands, source_identity
from tests.asl_live_redirect_workspace import Workspace, disposable_workspace
from tests.asl_live_review_project import FROZEN_FILES, write_project

MAX_HARNESS_LAUNCHES = 10
CHECKS = (
    "prepare-and-preflight",
    "seeded-defect-demonstrated",
    "independent-review-finding",
    "generated-repair-verified",
    "clean-second-review",
    "immutable-verifier-and-requirements",
    "quiescent-completion",
)


def verify_review_evidence(sequence: dict, harness: str, reviewer: str) -> None:
    """A terminal success alone cannot prove a review/repair sequence happened."""
    all_turns = sequence.get("turns", [])
    if [(t.get("session_kind"), t.get("harness")) for t in all_turns] != [
        ("implementation", harness),
        ("review", reviewer),
        ("implementation", harness),
        ("review", reviewer),
    ]:
        raise RuntimeError(
            "expected independent review, implementation repair, re-review"
        )
    turns = all_turns[1:]
    if any(
        not t.get("verification", {}).get("passed")
        or t.get("returncode") != 0
        or not t.get("committed")
        for t in all_turns
    ):
        raise RuntimeError(
            "every review and repair must pass runner-owned verification"
        )
    first = turns[0].get("review_verdict", {})
    last = turns[2].get("review_verdict", {})
    findings = first.get("findings", [])
    if (
        first.get("outcome") != "findings"
        or first.get("total_findings") != 1
        or len(findings) != 1
        or findings[0].get("repo") != "APP"
        or not any(
            e.get("path") == "APP:totals.py" for e in findings[0].get("evidence", [])
        )
    ):
        raise RuntimeError("first review must identify the seeded APP:totals.py defect")
    if last.get("outcome") != "all-clear" or last.get("total_findings") != 0:
        raise RuntimeError("the second independent review must be all-clear")
    checks = findings[0].get("verification_checks", [])
    product_checks = [
        check
        for check in checks
        if check.get("type") == "command"
        and check.get("repo") == "APP"
        and check.get("argv")
        in (
            ["python3", "verify_review.py", "product"],
            [sys.executable, "verify_review.py", "product"],
        )
        and check.get("review_replay") is True
    ]
    verified_names = {
        check.get("name")
        for check in turns[1]["verification"].get("checks", [])
        if check.get("status") == "pass" and check.get("repo") == "APP"
    }
    if not any(check.get("name") in verified_names for check in product_checks):
        raise RuntimeError(
            "the repair must pass the independent product checker in ASL"
        )
    cycles = sequence.get("review_cycles", [])
    if (
        len(cycles) != 2
        or cycles[0].get("outcome") != "findings"
        or cycles[0].get("review_session_id") != turns[0].get("session_id")
        or cycles[0].get("repair_session_ids") != [turns[1].get("session_id")]
        or cycles[1].get("outcome") != "all-clear"
        or cycles[1].get("review_session_id") != turns[2].get("session_id")
        or not cycles[0].get("fingerprints")
        or cycles[1].get("closed_findings") != cycles[0].get("fingerprints")
        or cycles[1].get("repeated_findings")
    ):
        raise RuntimeError(
            "durable review cycles must link the finding to its closed repair"
        )
    durable = sequence["final_run"]
    if (
        not durable.get("run_finished_at")
        or durable.get("active_turn")
        or durable.get("active_verification")
    ):
        raise RuntimeError("the reviewed run must have a quiescent durable finish")


def run_sequence(
    workspace: Workspace,
    commands: Commands,
    harness: HarnessName,
    reviewer: HarnessName,
) -> dict:
    nonce = uuid.uuid4().hex
    source = write_project(workspace, nonce)
    frozen = {name: (workspace.repo / name).read_bytes() for name in FROZEN_FILES}
    prepared = commands.asl(
        "prepare",
        "prepare",
        str(source),
        "--cwd",
        str(workspace.root),
        "--harness",
        harness,
        "--review-harness",
        reviewer,
        "--validator-harness",
        harness,
        "--validator-jobs",
        "1",
        "--validator-timeout",
        "240",
        "--base-dir",
        str(workspace.root / "plans"),
        "--state-dir",
        str(workspace.state_dir),
        timeout=commands.remaining(),
    )
    if not prepared.get("ok") or not prepared.get("marked_loop_ready"):
        raise RuntimeError(f"prepare did not mark the plan loop-ready: {prepared}")
    plan = Path(prepared["plan_dir"])
    common = [str(plan), "--state-dir", str(workspace.state_dir)]
    preflight = commands.asl(
        "preflight", "preflight", *common, "--cwd", str(workspace.root)
    )
    if not preflight.get("ok"):
        raise RuntimeError(f"preflight failed: {preflight}")
    run = shlex.split(prepared["run_command"]) + [
        "--state-dir",
        str(workspace.state_dir),
        "--timeout",
        "240",
        "--no-supervisor-audit",
        "--commit-per-session",
        "--max-repair-group-attempts",
        "1",
        "--max-review-fix-cycles",
        "1",
    ]
    first = launch(commands, [*run, "--max-sessions", "1"], "implementation")
    stopped = supervision(commands, plan, first)
    if not stopped or stopped.get("reason") != "max-sessions":
        raise RuntimeError(
            "initial implementation did not stop at its budget: "
            + str(
                stopped.get("error") or stopped.get("reason")
                if stopped
                else "no result"
            )
        )
    state_path = state.plan_state_dir(plan, workspace.state_dir) / "state.json"
    implementation = json.loads(state_path.read_text())
    turns = implementation.get("turns", [])
    if (
        len(turns) != 1
        or [t.get("session_id") for t in turns] != ["1"]
        or any(
            t.get("harness") != harness
            or t.get("session_kind") != "implementation"
            or not t.get("verification", {}).get("passed")
            for t in turns
        )
    ):
        raise RuntimeError(
            "the initial implementation lacks verified harness provenance"
        )
    if (workspace.repo / "totals.py").read_bytes() != frozen["legacy.txt"]:
        raise RuntimeError("initial implementation did not reproduce the seeded defect")
    # Demonstrate that the product checker really fails before repair, outside
    # a worker's claim or a review artifact. Keep stdout and exit status as evidence.
    defect = commands.call(
        "demonstrate-defect",
        [sys.executable, "verify_review.py", "product"],
        expected_returncode=1,
    )
    if defect.get("ok") is not False or "expected 0" not in defect.get("error", ""):
        raise RuntimeError(f"the seeded defect was not demonstrated: {defect}")
    second = launch(commands, [*run, "--resume"], "review-and-repair")
    finished = supervision(commands, plan, second)
    if not finished or not finished.get("ok") or finished.get("reason") != "complete":
        raise RuntimeError(
            "review/repair supervision did not complete: "
            + str(
                finished.get("error") or finished.get("reason")
                if finished
                else "no result"
            )
        )
    durable = json.loads(state_path.read_text())
    records = [
        json.loads(line)
        for line in (state_path.parent / "events.jsonl").read_text().splitlines()
    ]
    # Autopilot can resume in a new run after materializing repairs. The event
    # log retains every chunk; state.json intentionally holds only the last run.
    sequence = {
        "turns": [
            row["payload"]
            for row in records
            if row.get("name") == "turn_finished"
            and row["payload"].get("run_id") != implementation["run_id"]
        ],
        "review_cycles": [
            row["payload"] for row in records if row.get("name") == "review_cycle"
        ],
        "final_run": durable,
    }
    verify_review_evidence(sequence, harness, reviewer)
    repair_id = sequence["turns"][2]["session_id"]
    if (
        "asl:generated-review-repair-session"
        not in (plan / f"session-{repair_id}.md").read_text()
    ):
        raise RuntimeError("the repair was not an ASL-generated repair child")
    final = commands.asl("final-status", "status", *common)
    if (
        final["lock"]["active"]
        or not final["sessions"]
        or any(s["status"] != "complete" for s in final["sessions"])
        or durable.get("run_id") == implementation.get("run_id")
    ):
        raise RuntimeError("all reviewed sessions must finish with no active lock")
    for name, original in frozen.items():
        if (workspace.repo / name).read_bytes() != original:
            raise RuntimeError(f"the worker changed frozen acceptance input {name}")
    verified = commands.call(
        "verify-repaired-product", [sys.executable, "verify_review.py", "product"]
    )
    if verified.get("ok") is not True:
        raise RuntimeError("independent product verification did not pass")
    return {
        "plan_dir": str(plan),
        "seeded_failure": defect,
        "product_verification": verified,
        "implementation_run": implementation,
        "review_sequence": sequence,
        "final": final,
        "result": finished,
        "frozen_files_unchanged": list(frozen),
        "final_review": json.loads((plan / "final-review.json").read_text()),
        "final_review_markdown": (plan / "final-review.md").read_text(),
    }


def run_review(
    harness: HarnessName,
    reviewer: HarnessName,
    *,
    opted_in: bool,
    deadline: int,
    keep: bool,
) -> dict:
    started = time.monotonic()
    names = (harness, reviewer)
    probes = {name: harness_compatibility.check_harness(name) for name in names}
    result: dict[str, Any] = {
        "harness": harness,
        "review_harness": reviewer,
        "harnesses": {
            name: {
                "version": p.version,
                "executable": p.executable,
                "compatibility": p.status,
            }
            for name, p in probes.items()
        },
        "status": "skipped",
        "checks": [],
        "verified_versions": {},
        "artifacts": None,
        "detail": "live acceptance was not enabled",
    }
    if opted_in and all(
        p.executable and p.version and not p.blocks for p in probes.values()
    ):
        with disposable_workspace(
            f"{harness}-{reviewer}-review", keep=True, announce=False
        ) as workspace:
            commands = Commands(
                workspace.root,
                deadline=deadline,
                max_harness_launches=MAX_HARNESS_LAUNCHES,
            )
            try:
                for name, probe in probes.items():
                    assert probe.executable is not None
                    commands.bound_harness(name, probe.executable)
                result["evidence"] = run_sequence(
                    workspace, commands, harness, reviewer
                )
                after = {
                    name: harness_compatibility.check_harness(
                        name, use_cache=False
                    ).version
                    for name in names
                }
                result["versions_after"] = after
                if any(after[name] != probes[name].version for name in names):
                    raise RuntimeError(
                        "a harness version changed or became unavailable; "
                        "no pair is verified"
                    )
                result.update(
                    status="passed",
                    checks=list(CHECKS),
                    verified_versions={name: probes[name].version for name in names},
                    detail="finding, verified repair, and clean re-review passed",
                )
            except (Exception, KeyboardInterrupt) as error:
                result.update(
                    status="failed", detail=f"{type(error).__name__}: {error}"
                )
                result["interrupted"] = isinstance(error, KeyboardInterrupt)
            finally:
                result["cleanup"] = commands.cleanup()
                result["commands"] = commands.calls
                result["max_harness_launches"] = MAX_HARNESS_LAUNCHES
            result["artifacts"] = str(workspace.root)
            result["seconds"] = round(time.monotonic() - started, 3)
            (workspace.root / "acceptance.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
        if result["status"] == "passed" and not keep:
            shutil.rmtree(workspace.root)
            result["artifacts"] = None
    elif opted_in:
        result["detail"] = "a harness is missing, unversioned, or incompatible"
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def review_main(
    args: argparse.Namespace, *, harness: HarnessName, opted_in: bool
) -> int:
    source = source_identity()
    with redirect_stdout(sys.stderr):
        result = run_review(
            harness,
            args.review_harness,
            opted_in=opted_in,
            deadline=args.deadline,
            keep=args.keep_workspaces,
        )
    source_after = source_identity()
    unchanged = source == source_after
    report = {
        "schema_version": 1,
        "suite": "review",
        "opted_in": opted_in,
        "model_use": opted_in,
        "deadline_seconds_per_pair": args.deadline,
        "ok": unchanged and result["status"] != "failed",
        "complete": unchanged and result["status"] == "passed",
        "source": source,
        "source_after": source_after,
        "source_unchanged": unchanged,
        "pair": result,
        "verification_note": (
            "Only a passed complete review/repair sequence verifies this ordered pair "
            "and its exact installed versions. Skipped probes provide no "
            "acceptance evidence."
        ),
    }
    if not unchanged:
        report["error"] = (
            "ASL sources changed during acceptance; rerun the final checkout"
        )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        Path(args.report).write_text(rendered)
    if args.json:
        print(rendered, end="")
    else:
        print(
            f"{harness} → {args.review_harness}: "
            f"{result['status']} — {result['detail']}"
        )
        if result["artifacts"]:
            print(f"  artifacts: {result['artifacts']}")
        if not unchanged:
            print(report["error"], file=sys.stderr)
    return 0 if report["ok"] else 1
