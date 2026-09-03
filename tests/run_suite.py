"""Parallel shard driver for the agent_session_loop test suite.

``just test-fast`` runs the unit/schema/parser modules; ``just test-all`` runs
every test module plus the standalone helper-script suites, one isolated
subprocess per shard. Shards are ordered longest-first so the slowest module
starts immediately and bounds wall-clock time.

``just test-portable`` runs the subset that needs nothing but the Python
interpreter, which is what a non-macOS CI lane can honestly gate on.

Usage:
    python3 -m tests.run_suite --fast
    python3 -m tests.run_suite --portable
    python3 -m tests.run_suite --all [--jobs N]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# Unit/schema/parser modules: no fake-harness runs, no HTTP servers, no
# multi-second subprocess orchestration. Ordered longest-first.
FAST_MODULES = (
    "tests.test_daily_remind",
    "tests.test_claude_lint_hook",
    "tests.test_asl_cli",
    "tests.test_asl_cli_trace",
    "tests.test_asl_preflight",
    "tests.test_asl_verification",
    "tests.test_asl_baseline",
    "tests.test_asl_prepare",
    "tests.test_asl_tail",
    "tests.test_asl_docs",
    "tests.test_asl_workspaces",
    "tests.test_asl_repository_evidence",
    "tests.test_asl_repository_scope",
    "tests.test_asl_repository_intent",
    "tests.test_asl_repository_registration",
    "tests.test_asl_assurance_baseline",
    "tests.test_asl_assurance_models",
    "tests.test_asl_assurance_ledger",
    "tests.test_asl_assurance_projection",
    "tests.test_asl_assurance_reconcile",
    "tests.test_asl_assurance_cli",
    "tests.test_asl_supervisor_audit",
    "tests.test_asl_audit_replay",
    "tests.test_asl_assurance_lifecycle",
    "tests.test_asl_assurance_metrics",
    "tests.test_asl_acceptance",
    "tests.test_asl_turn_execution",
    "tests.test_asl_verification_transitions",
    "tests.test_asl_review_transitions",
    "tests.test_asl_review_session_contract",
    "tests.test_asl_review_session_body",
    "tests.test_asl_review_guard",
    "tests.test_asl_transitions",
    "tests.test_asl_run_coordination",
    "tests.test_asl_run_selection",
    "tests.test_asl_run_inputs",
    "tests.test_asl_run_persistence",
    "tests.test_asl_run_reporting",
    "tests.test_asl_live_verification",
    "tests.test_asl_run_termination",
    "tests.test_asl_readiness_gate",
    "tests.test_asl_review_cycle",
    "tests.test_asl_review_delta",
    "tests.test_asl_guidance_delivery",
    "tests.test_asl_handoff_guards",
    "tests.test_asl_external_write_evidence",
    "tests.test_asl_workspace_evidence",
    "tests.test_asl_claude_stream",
    "tests.test_asl_typing_ratchet",
    "tests.test_asl_runner_shrink",
    "tests.test_asl_turn_finalization",
    "tests.test_asl_turn_commit",
    "tests.test_asl_commit_groups",
    "tests.test_asl_core",
    "tests.test_asl_qna",
    "tests.test_asl_prompts",
    "tests.test_asl_control",
    "tests.test_asl_bridge_projection",
    "tests.test_asl_bridge_fixtures",
    "tests.test_asl_agent_mux_contract",
    "tests.test_asl_agent_mux_projection",
    "tests.test_asl_agent_mux_pairing",
    "tests.test_asl_diagnostics_export",
    "tests.test_asl_workbench_archive",
    "tests.test_asl_workbench_summary",
    "tests.test_asl_workbench_recovery",
    "tests.test_asl_progress",
    "tests.test_asl_metrics",
    "tests.test_asl_compaction",
    "tests.test_asl_authoring",
    "tests.test_session_overview",
    "tests.test_asl_run_contention",
    "tests.test_asl_state_migration",
    "tests.test_asl_process_tree",
    "tests.test_asl_supervision_liveness",
    "tests.test_asl_retention",
    "tests.test_asl_maintenance",
    "tests.test_asl_harness_compatibility",
    "tests.test_asl_startup_context",
    "tests.test_asl_commit_message",
    "tests.test_skill_sync",
    "tests.test_asl_literals",
)

# Modules that need nothing outside the Python interpreter: no `git`, no shell
# script, no launchd domain, no harness executable, no Kitty. This is what the
# Linux CI lane gates on, and the membership rule is mechanical rather than a
# judgement call -- a module belongs here when it spawns no external process at
# all. Everything else is left to the macOS lane, which has the real tools the
# product is developed and released against, so the portable lane never reports
# a pass it did not earn on a machine that has none of them.
#
# This is a strict subset of FAST_MODULES; `tests/test_asl_docs.py` enforces
# that, so a module deleted or renamed cannot survive here as a silent skip.
PORTABLE_MODULES = (
    "tests.test_asl_cli_trace",
    "tests.test_asl_tail",
    "tests.test_asl_assurance_baseline",
    "tests.test_asl_assurance_reconcile",
    "tests.test_asl_assurance_metrics",
    "tests.test_asl_turn_execution",
    "tests.test_asl_guidance_delivery",
    "tests.test_asl_verification_transitions",
    "tests.test_asl_review_transitions",
    "tests.test_asl_review_session_contract",
    "tests.test_asl_review_session_body",
    "tests.test_asl_run_selection",
    "tests.test_asl_run_inputs",
    "tests.test_asl_run_persistence",
    "tests.test_asl_run_reporting",
    "tests.test_asl_live_verification",
    "tests.test_asl_run_termination",
    "tests.test_asl_readiness_gate",
    "tests.test_asl_external_write_evidence",
    "tests.test_asl_workspace_evidence",
    "tests.test_asl_transitions",
    "tests.test_asl_typing_ratchet",
    "tests.test_asl_runner_shrink",
    "tests.test_asl_commit_groups",
    "tests.test_asl_bridge_projection",
    "tests.test_asl_bridge_fixtures",
    "tests.test_asl_agent_mux_contract",
    "tests.test_asl_agent_mux_projection",
    "tests.test_asl_agent_mux_pairing",
    "tests.test_asl_workbench_archive",
    "tests.test_asl_workbench_summary",
    "tests.test_asl_workbench_recovery",
    "tests.test_asl_progress",
    "tests.test_asl_metrics",
    "tests.test_asl_state_migration",
    "tests.test_asl_retention",
    "tests.test_asl_harness_compatibility",
    "tests.test_asl_startup_context",
    "tests.test_asl_literals",
)

# Integration modules: fake-harness end-to-end runs, workbench HTTP servers,
# and subprocess-heavy flows. Ordered longest-first.
INTEGRATION_MODULES = (
    "tests.test_asl_spark",
    "tests.test_asl_harness_integration",
    "tests.test_asl_codex_app_server",
    "tests.test_asl_workbench_server",
    "tests.test_asl_review_lifecycle",
    "tests.test_asl_workbench_static",
    "tests.test_asl_run_gates",
    "tests.test_asl_guards",
    "tests.test_asl_reply_flows",
    "tests.test_asl_e2e",
    "tests.test_asl_review",
    "tests.test_asl_workbench_launch",
    "tests.test_asl_pause_ux",
    "tests.test_asl_bridge_server",
    "tests.test_brainnotes_vault_guard",
)

# Standalone helper-script suites (not part of the tests package).
SCRIPT_SUITES = (
    "skills/asl/scripts/test_asl_autopilot.py",
    "skills/session-resplit/scripts/test_resplit_session.py",
)

_RAN_PATTERN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)


def _modules_for(mode: str) -> tuple[str, ...]:
    if mode == "portable":
        return PORTABLE_MODULES
    if mode == "fast":
        return FAST_MODULES
    return FAST_MODULES + INTEGRATION_MODULES


def _shard_commands(mode: str) -> list[tuple[str, list[str]]]:
    fast_only = mode != "all"
    modules = _modules_for(mode)
    shards = [
        (module, [sys.executable, "-m", "unittest", module]) for module in modules
    ]
    if not fast_only:
        shards.extend(
            (path, [sys.executable, str(REPO_ROOT / path)]) for path in SCRIPT_SUITES
        )
    if not fast_only:
        # Longest-first across the combined list keeps the slowest shards at
        # the front of the queue.
        integration_first = [
            shard
            for module in INTEGRATION_MODULES
            for shard in shards
            if shard[0] == module
        ]
        rest = [shard for shard in shards if shard not in integration_first]
        shards = integration_first + rest
    return shards


def _run_shard(name: str, command: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    ran_match = _RAN_PATTERN.search(output)
    return {
        "name": name,
        "returncode": completed.returncode,
        "duration": time.monotonic() - started,
        "tests": int(ran_match.group(1)) if ran_match else 0,
        "output": output,
    }


def _package_module_count() -> int:
    loader = unittest.defaultTestLoader
    suite = loader.discover(str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    if loader.errors:
        raise SystemExit("test discovery failed:\n" + "\n".join(loader.errors))
    return suite.countTestCases()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--fast",
        action="store_true",
        help="run only the unit/schema/parser modules",
    )
    group.add_argument(
        "--portable",
        action="store_true",
        help="run only the modules that need no external process",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="run every test module plus helper-script suites (default)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        # Half the cores, not all of them: a shard is not a single process. The
        # loop tests each spawn `agent_session_loop run`, which spawns a fake
        # harness of its own, so one shard per core oversubscribes the machine
        # several times over. Under that load the loop tests race their own
        # subprocess timeouts and process-group enumeration, which reads as
        # flaky tests rather than as the overload it is.
        default=max(1, (os.cpu_count() or 4) // 2),
        help="maximum parallel shard processes (default: half the CPU count)",
    )
    args = parser.parse_args(argv)
    mode = "portable" if args.portable else "fast" if args.fast else "all"
    fast_only = mode != "all"

    shards = _shard_commands(mode)
    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(_run_shard, name, command): name for name, command in shards
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            verdict = "ok" if result["returncode"] == 0 else "FAIL"
            print(
                f"{verdict:>4}  {result['name']:<44} "
                f"{result['duration']:6.1f}s  {result['tests']:4d} tests",
                flush=True,
            )
            results.append(result)

    failures = [result for result in results if result["returncode"] != 0]
    total_tests = sum(result["tests"] for result in results)
    wall = time.monotonic() - started
    for result in failures:
        print(f"\n=== output from failed shard {result['name']} ===")
        print(result["output"].rstrip())

    if not fast_only:
        script_tests = sum(
            result["tests"] for result in results if result["name"] in SCRIPT_SUITES
        )
        expected = _package_module_count()
        package_tests = total_tests - script_tests
        if package_tests != expected:
            print(
                f"\nFAIL: shards ran {package_tests} package tests but discovery "
                f"finds {expected}; a test module is missing from run_suite lists"
            )
            return 1

    label = {"portable": "portable suite", "fast": "fast suite"}.get(
        mode, "complete suite"
    )
    verdict = "FAILED" if failures else "passed"
    print(
        f"\n{label} {verdict}: {total_tests} tests across {len(shards)} shards "
        f"in {wall:.1f}s ({len(failures)} failed shard(s))"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
