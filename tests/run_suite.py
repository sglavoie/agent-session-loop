"""Parallel shard driver for the agent_session_loop test suite.

``just test-fast`` runs the fast lane; ``just test-all`` runs the fast and
integration lanes plus the standalone helper-script suites; ``just
test-portable`` runs the subset that needs nothing but the Python interpreter,
which is what a non-macOS CI lane can honestly gate on. Each shard is one
module run as its own ``python -m unittest`` subprocess.

Lanes are derived, not listed. Every ``tests/test*.py`` module discovery finds
is a fast-lane shard unless it says otherwise with a module-level literal,
read here from its source without importing it:

``ASL_TEST_LANE = "integration"``
    The module runs fake-harness end-to-end flows, workbench servers, or other
    subprocess-heavy orchestration, so only ``--all`` runs it.
``ASL_TEST_PORTABLE = True``
    The module spawns no external process at all -- no ``git``, no shell
    script, no launchd domain, no harness executable -- so the portable lane
    runs it too. Only a fast-lane module can be portable.
``ASL_TEST_SHARDS = ("tests.section_a", ...)``
    The module is a facade: run these (non-discoverable) section modules as
    the shards in its place, under the facade's lane and portability. An empty
    tuple means the module is no shard at all, because the tests it re-exports
    already run in their own modules or it holds only shared helpers.

Every mode also runs a registration shard (``--check-registration``) that
proves, by test id, that the fast and integration shards together run every
test broad discovery finds, each exactly once.

Shard durations are recorded in ``.cache/run_suite_timings.json`` (untracked)
and later runs start the longest shards first, so the slowest one bounds
wall-clock time instead of starting last; shards with no recorded duration go
first, in a stable lane-then-name order.

Usage:
    python3 -m tests.run_suite --fast
    python3 -m tests.run_suite --portable
    python3 -m tests.run_suite --all [--jobs N]
"""

from __future__ import annotations

import argparse
import ast
import collections
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
TIMINGS_PATH = REPO_ROOT / ".cache" / "run_suite_timings.json"
REGISTRATION_SHARD = "run_suite registration"

LANE_MARKER = "ASL_TEST_LANE"
PORTABLE_MARKER = "ASL_TEST_PORTABLE"
SHARDS_MARKER = "ASL_TEST_SHARDS"
_MARKERS = (LANE_MARKER, PORTABLE_MARKER, SHARDS_MARKER)
_LANES = ("fast", "integration")

_RAN_PATTERN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)


class LaneError(Exception):
    """A test module declares its lane in a way the driver cannot honor."""


def _module_path(module: str) -> Path:
    return REPO_ROOT / Path(*module.split(".")).with_suffix(".py")


def _marker_statement(lines: list[str], start: int) -> ast.stmt | None:
    """The statement that begins on ``lines[start]``, parsed on its own.

    Parsing every test module whole costs most of a second, which every import
    of this module would pay; a marker is one short statement, so only it is
    parsed, growing a line at a time until it is complete.
    """
    for end in range(start + 1, min(len(lines), start + 40) + 1):
        try:
            body = ast.parse("".join(lines[start:end])).body
        except SyntaxError:
            continue
        return body[0] if len(body) == 1 else None
    return None


def _read_markers(path: Path) -> dict[str, Any]:
    """The ``ASL_TEST_*`` literals a module assigns at top level."""
    text = path.read_text(encoding="utf-8")
    if "ASL_TEST_" not in text:
        return {}
    lines = text.splitlines(keepends=True)
    found: dict[str, Any] = {}
    for start, line in enumerate(lines):
        if not line.startswith("ASL_TEST_"):
            continue
        node = _marker_statement(lines, start)
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            raise LaneError(f"{path.name}:{start + 1}: unreadable lane marker")
        if not isinstance(target, ast.Name) or target.id not in _MARKERS:
            raise LaneError(f"{path.name}:{start + 1}: unknown lane marker")
        try:
            found[target.id] = ast.literal_eval(value)
        except ValueError as error:
            raise LaneError(f"{path.name}: {target.id} is not a literal") from error
    return found


def _derive_lanes() -> dict[str, tuple[str, ...]]:
    lanes: dict[str, list[str]] = {"fast": [], "integration": [], "portable": []}
    for path in sorted(TESTS_DIR.glob("test*.py")):
        module = f"tests.{path.stem}"
        markers = _read_markers(path)
        lane = markers.get(LANE_MARKER, "fast")
        portable = markers.get(PORTABLE_MARKER, False)
        shards = markers.get(SHARDS_MARKER, (module,))
        if lane not in _LANES:
            raise LaneError(f"{path.name}: {LANE_MARKER} must be one of {_LANES}")
        if not isinstance(portable, bool) or (portable and lane != "fast"):
            raise LaneError(f"{path.name}: {PORTABLE_MARKER} is a fast-lane flag")
        if not isinstance(shards, tuple) or not all(isinstance(s, str) for s in shards):
            raise LaneError(f"{path.name}: {SHARDS_MARKER} must be a tuple of names")
        for shard in shards:
            if shard == module:
                continue
            if shard.startswith("tests.test") or not _module_path(shard).is_file():
                # A discoverable module is already its own shard; naming it
                # again would run its tests twice.
                raise LaneError(
                    f"{path.name}: {SHARDS_MARKER} names {shard}, which is not "
                    "an undiscoverable section module in tests/"
                )
        lanes[lane].extend(shards)
        if portable:
            lanes["portable"].extend(shards)
    counts = collections.Counter(lanes["fast"] + lanes["integration"])
    if repeated := sorted(name for name, count in counts.items() if count > 1):
        raise LaneError(f"shards claimed twice: {', '.join(repeated)}")
    return {name: tuple(modules) for name, modules in lanes.items()}


_LANE_MODULES = _derive_lanes()
FAST_MODULES = _LANE_MODULES["fast"]
INTEGRATION_MODULES = _LANE_MODULES["integration"]
PORTABLE_MODULES = _LANE_MODULES["portable"]

# Standalone helper-script suites (not part of the tests package).
SCRIPT_SUITES = tuple(
    path.relative_to(REPO_ROOT).as_posix()
    for path in sorted(REPO_ROOT.glob("skills/*/scripts/test_*.py"))
)


def _modules_for(mode: str) -> tuple[str, ...]:
    if mode == "portable":
        return PORTABLE_MODULES
    if mode == "fast":
        return FAST_MODULES
    return INTEGRATION_MODULES + FAST_MODULES


def _load_timings() -> dict[str, float]:
    try:
        recorded = json.loads(TIMINGS_PATH.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    if not isinstance(recorded, dict):
        return {}
    return {
        name: float(seconds)
        for name, seconds in recorded.items()
        if isinstance(name, str) and isinstance(seconds, (int, float))
    }


def _record_timings(results: list[dict[str, Any]]) -> None:
    """Merge this run's shard durations into the cache; best effort.

    Entries for shards that no longer exist (a renamed or deleted module) are
    dropped, so the cache only ever describes the current suite.
    """
    current = {REGISTRATION_SHARD, *_modules_for("all"), *SCRIPT_SUITES}
    timings = {
        name: seconds for name, seconds in _load_timings().items() if name in current
    }
    timings.update({result["name"]: round(result["duration"], 2) for result in results})
    try:
        TIMINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        staging = TIMINGS_PATH.with_name(f"{TIMINGS_PATH.name}.{os.getpid()}.tmp")
        staging.write_text(json.dumps(timings, indent=1, sort_keys=True) + "\n")
        os.replace(staging, TIMINGS_PATH)
    except OSError as error:
        print(f"note: shard timings not recorded: {error}")


def _shard_commands(
    mode: str, timings: dict[str, float] | None = None
) -> list[tuple[str, list[str]]]:
    """Every shard ``mode`` runs, longest recorded duration first.

    The registration check is a shard like any other, so its imports overlap
    with the tests rather than adding to wall-clock time.
    """
    shards = [
        (
            REGISTRATION_SHARD,
            [sys.executable, "-m", "tests.run_suite", "--check-registration"],
        )
    ]
    shards.extend(
        (module, [sys.executable, "-m", "unittest", module])
        for module in _modules_for(mode)
    )
    if mode == "all":
        shards.extend(
            (path, [sys.executable, str(REPO_ROOT / path)]) for path in SCRIPT_SUITES
        )
    recorded = _load_timings() if timings is None else timings
    # Stable: shards with equal (or no) recorded duration keep lane order.
    return sorted(shards, key=lambda shard: -recorded.get(shard[0], float("inf")))


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


def _test_ids(suite: Any) -> set[str]:
    ids: set[str] = set()
    pending: list[Any] = [suite]
    while pending:
        case = pending.pop()
        if isinstance(case, unittest.TestSuite):
            pending.extend(case)
            continue
        ids.add(case.id())
    return ids


def _discovered_test_ids() -> set[str]:
    """Every test broad discovery finds.

    Test ids, not counts or module names, are what answer "does some shard run
    this test". A count reads a module-level ``SkipTest`` (``Ran 0 tests``) as
    an unregistered module, and a module name stops matching once a facade
    star-imports its ``*_cases_*`` or ``*_section_*`` helpers: the classes it
    runs report the helper as their ``__module__``. Ids are the same whichever
    module a class is reached through.
    """
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS_DIR), top_level_dir=str(REPO_ROOT))
    if loader.errors:
        raise SystemExit("test discovery failed:\n" + "\n".join(loader.errors))
    return _test_ids(suite)


def _registered_test_ids(modules: set[str]) -> dict[str, set[str]]:
    """The test ids each shard module runs, keyed by module name."""
    loader = unittest.TestLoader()
    registered = {name: _test_ids(loader.loadTestsFromName(name)) for name in modules}
    if loader.errors:
        raise SystemExit(
            "loading run_suite modules failed:\n" + "\n".join(loader.errors)
        )
    return registered


def _defining_modules(ids: set[str]) -> list[str]:
    return sorted({test_id.rsplit(".", 2)[0] for test_id in ids})


def check_registration() -> int:
    """Fail unless the fast and integration shards run each test exactly once."""
    discovered = _discovered_test_ids()
    registered = _registered_test_ids(set(FAST_MODULES) | set(INTEGRATION_MODULES))
    owners: dict[str, list[str]] = collections.defaultdict(list)
    for name, ids in registered.items():
        for test_id in ids:
            owners[test_id].append(name)
    problems = []
    if unregistered := discovered - owners.keys():
        problems.append(
            f"discovery finds tests in {', '.join(_defining_modules(unregistered))} "
            f"that no run_suite shard runs; a facade's {SHARDS_MARKER} leaves "
            "them out"
        )
    twice = [sorted(names) for names in owners.values() if len(names) > 1]
    if twice:
        pairs = sorted({" and ".join(names) for names in twice})
        problems.append(
            f"{len(twice)} tests run in more than one shard ({'; '.join(pairs)}); "
            f"a facade that re-exports them needs {SHARDS_MARKER}"
        )
    empty = sorted(name for name, ids in registered.items() if not ids & discovered)
    if empty:
        problems.append(
            f"run_suite shards {', '.join(empty)} hold no test broad discovery "
            f"finds; mark a helper-only module with {SHARDS_MARKER} = ()"
        )
    for problem in problems:
        print(f"FAIL: {problem}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--fast", action="store_true", help="run only the fast-lane modules"
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
    group.add_argument(
        "--check-registration",
        action="store_true",
        help="only check that the shards run every discovered test exactly once",
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
    if args.check_registration:
        return check_registration()
    mode = "portable" if args.portable else "fast" if args.fast else "all"

    shards = _shard_commands(mode)
    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(_run_shard, name, command) for name, command in shards]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            verdict = "ok" if result["returncode"] == 0 else "FAIL"
            print(
                f"{verdict:>4}  {result['name']:<44} "
                f"{result['duration']:6.1f}s  {result['tests']:4d} tests",
                flush=True,
            )
            results.append(result)
    _record_timings(results)

    failures = [result for result in results if result["returncode"] != 0]
    total_tests = sum(result["tests"] for result in results)
    wall = time.monotonic() - started
    for result in failures:
        print(f"\n=== output from failed shard {result['name']} ===")
        print(result["output"].rstrip())
    # A module that ran nothing is not a failure -- the two installed-pair gates
    # skip when the program they judge is not installed -- but it is not a
    # success either, and a suite that printed only `ok` for it would report
    # coverage it did not have.
    modules = set(_modules_for(mode))
    for result in sorted(results, key=lambda result: result["name"]):
        if result["name"] in modules and result["tests"] == 0:
            print(f"\nnote: {result['name']} ran no tests (skipped)")

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
