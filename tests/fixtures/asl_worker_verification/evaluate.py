# mypy: ignore-errors
# ruff: noqa
#!/usr/bin/env python3
"""Evaluate live-worker verification ownership from focused submodules."""

from __future__ import annotations

import sys
from pathlib import Path

_FIXTURE_DIR = Path(__file__).resolve().parent
if str(_FIXTURE_DIR) not in sys.path:
    sys.path.insert(0, str(_FIXTURE_DIR))

import evaluate_evidence as _evidence
import evaluate_policy as _policy
import evaluate_shared as _shared
import evaluate_trials as _trials

for _module in (_shared, _policy, _trials, _evidence):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


def _policy_packet(*, is_review: bool) -> str:
    """Keep policy-surface rendering behind the facade's testable seam."""
    return _rendered_packet(is_review=is_review)


_policy._rendered_packet = _policy_packet


def _render_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for entry in report["scenarios"]:
        lines.append(f"{entry['scenario']}: {len(entry['trials'])} trial(s)")
        for trial in entry["trials"]:
            mark = "pass" if trial["passed"] else "FAIL"
            lines.append(
                f"  trial {trial['trial']} [{mark}] harness={trial['harness']} "
                f"model={trial['model_requested']} "
                f"observed={','.join(trial['observed_models']) or '-'}"
            )
            for criterion in trial["criteria"]:
                flag = " ok " if criterion["passed"] else "FAIL"
                lines.append(f"    [{flag}] {criterion['name']}: {criterion['detail']}")
    if report["problems"]:
        lines.append("")
        lines.append("Problems:")
        lines.extend(f"  - {problem}" for problem in report["problems"])
    lines.append("")
    verdict = "accepted" if report["accepted"] else "not accepted"
    lines.append(f"live worker evidence: {verdict}")
    return "\n".join(lines)


def _selected_scenarios(names: Iterable[str] | None) -> list[Scenario]:
    if not names:
        return list(SCENARIOS)
    selected: list[Scenario] = []
    for name in names:
        scenario = SCENARIOS_BY_KEY.get(name)
        if scenario is None:
            raise SystemExit(
                f"unknown scenario {name!r}; known: {', '.join(SCENARIOS_BY_KEY)}"
            )
        selected.append(scenario)
    return selected


def command_run(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).resolve()
    scenarios = _selected_scenarios(args.scenario)
    trials = args.trials if args.trials is not None else REQUIRED_TRIALS
    only = set(args.trial or ())
    executable = (
        Path(args.harness_executable).resolve() if args.harness_executable else None
    )

    receipts: list[dict[str, Any]] = []
    for scenario in scenarios:
        for number in range(1, trials + 1):
            if only and number not in only:
                continue
            trial_dir = cache_dir / "trials" / scenario.key / str(number)
            trial_dir.mkdir(parents=True, exist_ok=True)
            print(f"[{scenario.key}/{number}] running", flush=True)
            receipt = run_trial(
                TrialRequest(
                    scenario=scenario,
                    trial=number,
                    trial_dir=trial_dir,
                    harness=args.harness,
                    model=args.model,
                    harness_executable=executable,
                    timeout_seconds=args.timeout,
                )
            )
            receipts.append(receipt)
            print(
                f"[{scenario.key}/{number}] returncode="
                f"{receipt['run']['returncode']} "
                f"duration={receipt['run']['duration_seconds']}s "
                f"focused_exit={receipt['final_state']['focused_exit']} "
                f"contract_exit={receipt['final_state']['contract_exit']}",
                flush=True,
            )
    if args.json:
        print(json.dumps(receipts, indent=2, sort_keys=True))
    return 0


def command_check(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).resolve()
    report = check_cache(cache_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_report(report))
    return 0 if report["accepted"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evaluate.py",
        description=(
            "Record and check whether live ASL workers leave the broad final "
            "gate to the runner."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Where trial artifacts and receipts live (default: the fixture's cache/)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Execute trials against a live harness (spawns a model)"
    )
    run_parser.add_argument("--scenario", action="append", help="Scenario key to run")
    run_parser.add_argument(
        "--trial", action="append", type=int, help="Only this trial number"
    )
    run_parser.add_argument("--trials", type=int, default=None)
    run_parser.add_argument("--harness", default="claude")
    run_parser.add_argument("--model", default="claude-opus-5")
    run_parser.add_argument(
        "--harness-executable",
        default=None,
        help=(
            "Drive the run with this executable instead of the configured "
            "harness. Marks the trial as not live, which `check` never accepts."
        ),
    )
    run_parser.add_argument("--timeout", type=float, default=2400.0)
    run_parser.add_argument("--json", action="store_true")
    run_parser.set_defaults(handler=command_run)

    check_parser = subparsers.add_parser(
        "check", help="Validate recorded evidence (read-only)"
    )
    check_parser.add_argument("--json", action="store_true")
    check_parser.set_defaults(handler=command_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
