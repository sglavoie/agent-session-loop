"""Parallel shard driver for the agent_session_loop test suite.

``just test-fast`` runs the unit/schema/parser modules; ``just test-all`` runs
every test module plus the standalone helper-script suites, one isolated
subprocess per shard. Shards are ordered longest-first so the slowest module
starts immediately and bounds wall-clock time.

Usage:
    python3 -m tests.run_suite --fast
    python3 -m tests.run_suite --all [--jobs N]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Unit/schema/parser modules: no fake-harness runs, no HTTP servers, no
# multi-second subprocess orchestration. Ordered longest-first.
FAST_MODULES = (
    "tests.test_asl_cli",
    "tests.test_asl_preflight",
    "tests.test_asl_verification",
    "tests.test_asl_baseline",
    "tests.test_asl_prepare",
    "tests.test_asl_tail",
    "tests.test_asl_docs",
    "tests.test_asl_core",
    "tests.test_asl_qna",
    "tests.test_asl_prompts",
    "tests.test_asl_control",
    "tests.test_asl_workbench_archive",
    "tests.test_asl_workbench_summary",
    "tests.test_asl_workbench_recovery",
    "tests.test_asl_progress",
    "tests.test_asl_metrics",
    "tests.test_asl_compaction",
    "tests.test_asl_authoring",
    "tests.test_session_overview",
)

# Integration modules: fake-harness end-to-end runs, workbench HTTP servers,
# and subprocess-heavy flows. Ordered longest-first.
INTEGRATION_MODULES = (
    "tests.test_asl_harness_integration",
    "tests.test_asl_workbench_server",
    "tests.test_asl_review_lifecycle",
    "tests.test_asl_workbench_static",
    "tests.test_asl_run_gates",
    "tests.test_asl_reply_flows",
    "tests.test_asl_e2e",
    "tests.test_asl_review",
    "tests.test_asl_workbench_launch",
    "tests.test_asl_pause_ux",
)

# Standalone helper-script suites (not part of the tests package).
SCRIPT_SUITES = (
    "skills/asl/scripts/test_asl_autopilot.py",
    "skills/session-resplit/scripts/test_resplit_session.py",
)

_RAN_PATTERN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)


def _shard_commands(fast_only: bool) -> list[tuple[str, list[str]]]:
    modules = FAST_MODULES if fast_only else FAST_MODULES + INTEGRATION_MODULES
    shards = [(module, [sys.executable, "-m", "unittest", module]) for module in modules]
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


def _run_shard(name: str, command: list[str]) -> dict[str, object]:
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
        "--all",
        action="store_true",
        help="run every test module plus helper-script suites (default)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=os.cpu_count() or 4,
        help="maximum parallel shard processes (default: CPU count)",
    )
    args = parser.parse_args(argv)
    fast_only = args.fast

    shards = _shard_commands(fast_only)
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

    label = "fast suite" if fast_only else "complete suite"
    verdict = "FAILED" if failures else "passed"
    print(
        f"\n{label} {verdict}: {total_tests} tests across {len(shards)} shards "
        f"in {wall:.1f}s ({len(failures)} failed shard(s))"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
