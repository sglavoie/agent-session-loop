from .asl_live_redirect_runner import run_harness_acceptance
from .asl_live_redirect_support import *


def build_report(
    acceptances: Sequence[HarnessAcceptance],
    *,
    opted_in: bool,
    requested: Sequence[str],
    seconds: float,
) -> dict[str, Any]:
    """The stable JSON document this runner exists to produce."""
    counts = {STATUS_PASSED: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
    limitations: list[str] = []
    verified: dict[str, str | None] = {}
    for acceptance in acceptances:
        counts[STATUS_PASSED] += acceptance.passed
        counts[STATUS_FAILED] += acceptance.failed
        counts[STATUS_SKIPPED] += acceptance.skipped
        limitations.extend(acceptance.limitations)
        # A version is only "verified" when a live scenario actually passed
        # against it. An installed version nothing exercised is inventory.
        verified[acceptance.harness] = acceptance.version if acceptance.passed else None
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": counts[STATUS_FAILED] == 0,
        "opted_in": opted_in,
        "model_use": opted_in,
        "opt_in": {"env_var": OPT_IN_ENV_VAR, "flag": "--live"},
        "requested_harnesses": list(requested),
        "scenarios": [
            {"scenario": scenario.key, "proves": scenario.proves}
            for scenario in SCENARIOS
        ],
        "harnesses": [acceptance.payload() for acceptance in acceptances],
        "counts": counts,
        "verified_versions": verified,
        "verification_note": VERIFICATION_NOTE,
        "limitations": limitations,
        "seconds": round(seconds, 3),
    }


def format_report(report: Mapping[str, Any]) -> str:
    """A terminal rendering that names the consequence of every row."""
    marks = {STATUS_PASSED: "ok", STATUS_FAILED: "FAIL", STATUS_SKIPPED: "skip"}
    lines: list[str] = []
    if not report["opted_in"]:
        lines.append(
            "live acceptance is opt-in and was not enabled: no harness turn ran "
            f"and no model was used (pass --live or set {OPT_IN_ENV_VAR}=1)"
        )
        lines.append("")
    for entry in report["harnesses"]:
        version = entry["version"] or "unknown version"
        lines.append(
            f"{entry['harness']} {version} "
            f"[{entry['compatibility']}: {entry['compatibility_reason']}]"
        )
        for scenario in entry["scenarios"]:
            lines.append(
                f"  {marks[scenario['status']]:>4}  "
                f"{scenario['scenario']:<34} {scenario['seconds']:>7.1f}s  "
                f"{scenario['detail']}"
            )
        lines.append("")
    counts = report["counts"]
    lines.append(
        f"{counts[STATUS_PASSED]} passed, {counts[STATUS_FAILED]} failed, "
        f"{counts[STATUS_SKIPPED]} skipped in {report['seconds']:.1f}s"
    )
    verified = {k: v for k, v in report["verified_versions"].items() if v}
    lines.append(
        "verified versions: "
        + (
            ", ".join(f"{name} {version}" for name, version in sorted(verified.items()))
            if verified
            else "none"
        )
    )
    lines.append(report["verification_note"])
    for limitation in report["limitations"]:
        lines.append(f"  limitation: {limitation}")
    return "\n".join(lines)


def _harness_name(value: str) -> HarnessName:
    for name in HARNESS_NAMES:
        if name == value:
            return name
    raise argparse.ArgumentTypeError(
        f"unknown harness {value!r}; choose from {', '.join(HARNESS_NAMES)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tests.asl_live_redirect_acceptance",
        description=(
            "Opt-in live acceptance for ASL interruption, conversation "
            "continuity, and urgent-correction delivery against the real "
            "harness CLIs."
        ),
    )
    parser.add_argument(
        "--harness",
        action="append",
        type=_harness_name,
        metavar="NAME",
        help="exercise one harness; repeatable",
    )
    parser.add_argument(
        "--all-harnesses",
        action="store_true",
        help="exercise every supported harness",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "actually launch harness turns. This spends provider quota. "
            f"{OPT_IN_ENV_VAR}=1 is the equivalent environment switch."
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument(
        "--report", metavar="PATH", help="write the JSON report to PATH"
    )
    parser.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="leave the disposable repositories on disk for inspection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    requested: tuple[HarnessName, ...]
    if args.all_harnesses:
        requested = tuple(HARNESS_NAMES)
    elif args.harness:
        requested = tuple(dict.fromkeys(args.harness))
    else:
        parser.error("choose --all-harnesses or at least one --harness")
        return 2
    opted_in = live_opt_in(flag=bool(args.live))
    started = time.monotonic()
    acceptances = [
        run_harness_acceptance(
            harness, opted_in=opted_in, keep=bool(args.keep_workspaces)
        )
        for harness in requested
    ]
    report = build_report(
        acceptances,
        opted_in=opted_in,
        requested=requested,
        seconds=time.monotonic() - started,
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
