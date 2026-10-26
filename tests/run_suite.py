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

``--slice K/N`` runs only the K-th of N disjoint slices of a mode's shards,
so CI can spread one suite over N parallel jobs. Shards are dealt round-robin
in lane-then-name order -- never by recorded duration, which a fresh checkout
does not have and two jobs could disagree on -- so every job computes the same
partition and the N slices together run each shard exactly once. The
registration shard runs in slice 1 only.

Each shard runs in its own session under a timeout (``--shard-timeout``, or
``$ASL_TEST_SHARD_TIMEOUT``; default 900 seconds, 0 disables it). A shard that
overruns has its whole process group killed and is reported as ``TIMEOUT``
with the output it produced. A failed shard's output is shown as its last
200 lines unless ``--verbose`` (or ``$ASL_TEST_VERBOSE=1``) asks for all of
it; the full output is always kept under ``.cache/run_suite_logs/``, which is
what CI uploads from a failed job, and a shard that passes clears its own.

Usage:
    python3 -m tests.run_suite --fast
    python3 -m tests.run_suite --portable
    python3 -m tests.run_suite --all [--jobs N] [--shard-timeout S] [--verbose]
    python3 -m tests.run_suite --all --slice 2/4
"""

from __future__ import annotations

import argparse
import ast
import collections
import concurrent.futures
import contextlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
TIMINGS_PATH = REPO_ROOT / ".cache" / "run_suite_timings.json"
LOGS_DIR = REPO_ROOT / ".cache" / "run_suite_logs"
REGISTRATION_SHARD = "run_suite registration"

TIMEOUT_ENV = "ASL_TEST_SHARD_TIMEOUT"
VERBOSE_ENV = "ASL_TEST_VERBOSE"
DEFAULT_SHARD_TIMEOUT = 900.0
#: How much of a failed shard's output is shown without ``--verbose``.
FAILED_OUTPUT_LINES = 200
#: How long a killed shard's pipes get to drain before its output is taken as is.
_DRAIN_SECONDS = 10.0

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


def parse_slice(value: str) -> tuple[int, int]:
    """``"2/4"`` as ``(2, 4)``: the second of four slices, counted from one."""
    index, sep, count = value.partition("/")
    try:
        parsed = (int(index), int(count))
    except ValueError:
        parsed = (0, 0)
    if not sep or not 1 <= parsed[0] <= parsed[1]:
        raise argparse.ArgumentTypeError(
            f"expected K/N with 1 <= K <= N, not {value!r}"
        )
    return parsed


def _shard_commands(
    mode: str,
    timings: dict[str, float] | None = None,
    slice_: tuple[int, int] = (1, 1),
) -> list[tuple[str, list[str]]]:
    """Every shard ``mode`` runs in ``slice_``, longest recorded duration first.

    The registration check is a shard like any other, so its imports overlap
    with the tests rather than adding to wall-clock time.
    """
    shards = [
        (module, [sys.executable, "-m", "unittest", module])
        for module in _modules_for(mode)
    ]
    if mode == "all":
        shards.extend(
            (path, [sys.executable, str(REPO_ROOT / path)]) for path in SCRIPT_SUITES
        )
    index, count = slice_
    shards = shards[index - 1 :: count]
    if index == 1:
        shards.insert(
            0,
            (
                REGISTRATION_SHARD,
                [sys.executable, "-m", "tests.run_suite", "--check-registration"],
            ),
        )
    recorded = _load_timings() if timings is None else timings
    # Stable: shards with equal (or no) recorded duration keep lane order.
    return sorted(shards, key=lambda shard: -recorded.get(shard[0], float("inf")))


#: Shards still running, so an interrupted driver can take them down with it:
#: each runs in its own session, out of reach of the terminal's Ctrl-C.
_LIVE: dict[int, subprocess.Popen[str]] = {}
_LIVE_LOCK = threading.Lock()


def _kill_group(process: subprocess.Popen[str]) -> None:
    """SIGKILL the shard's process group (its pgid is its pid).

    Only while the leader is unreaped: once it is, its pid -- and so the group
    id -- may already belong to another shard's new session.
    """
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _kill_live_shards() -> None:
    with _LIVE_LOCK:
        live = list(_LIVE.values())
    for process in live:
        _kill_group(process)


def _drain(process: subprocess.Popen[str]) -> str:
    """What a killed shard wrote, without waiting on a pipe an escapee holds."""
    try:
        output, _ = process.communicate(timeout=_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        process.wait()
        return (
            "(no output recovered: a process outside the shard's group still "
            "holds its output pipe)"
        )
    return output or ""


def _tests_ran(output: str) -> int:
    """The last ``Ran N tests`` line: a suite that prints a nested run's
    summary before its own must be counted by its own."""
    counts = _RAN_PATTERN.findall(output)
    return int(counts[-1]) if counts else 0


def describe_returncode(returncode: int) -> str:
    """``rc=1``, or ``rc=-9 (SIGKILL)`` for a shard a signal ended."""
    if returncode < 0:
        try:
            return f"rc={returncode} ({signal.Signals(-returncode).name})"
        except ValueError:
            pass
    return f"rc={returncode}"


def _run_shard(
    name: str, command: list[str], timeout: float | None = None
) -> dict[str, Any]:
    """Run one shard in its own session; kill its process group on timeout."""
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with _LIVE_LOCK:
        _LIVE[process.pid] = process
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout or None)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        output = _drain(process)
    finally:
        with _LIVE_LOCK:
            _LIVE.pop(process.pid, None)
    output = output or ""
    return {
        "name": name,
        "command": command,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "duration": time.monotonic() - started,
        "tests": _tests_ran(output),
        "output": output,
    }


def _verdict(result: dict[str, Any]) -> str:
    if result.get("timed_out"):
        return "TIMEOUT"
    return "ok" if result["returncode"] == 0 else "FAIL"


def _log_path(name: str) -> Path:
    return LOGS_DIR / (re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".log")


def _keep_log(result: dict[str, Any]) -> Path | None:
    """Write a failed shard's full output under ``.cache``; best effort."""
    path = _log_path(result["name"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result["output"], encoding="utf-8")
    except OSError:
        return None
    return path


def _clear_log(result: dict[str, Any]) -> None:
    """Drop a passing shard's log, so what is left describes this run only."""
    with contextlib.suppress(OSError):
        _log_path(result["name"]).unlink(missing_ok=True)


def failure_report(
    result: dict[str, Any], *, verbose: bool, timeout: float | None = None
) -> str:
    """The section printed for one failed or timed-out shard."""
    if result.get("timed_out"):
        header = (
            f"=== TIMEOUT: shard {result['name']} killed after {timeout or 0:g}s "
            f"({describe_returncode(result['returncode'])}); partial output ==="
        )
    else:
        header = (
            f"=== output from failed shard {result['name']} "
            f"({describe_returncode(result['returncode'])}) ==="
        )
    lines = result["output"].rstrip().splitlines()
    shown = lines if verbose else lines[-FAILED_OUTPUT_LINES:]
    parts = [header]
    # Kept whether or not the tail below is cut short: a CI job uploads the
    # log directory, and a shard missing from it reads as one that passed.
    log = _keep_log(result)
    if len(shown) < len(lines):
        where = f"full output: {log}; " if log else ""
        parts.append(
            f"... {len(lines) - len(shown)} earlier lines omitted ({where}"
            f"--verbose or {VERBOSE_ENV}=1 shows all)"
        )
    parts.extend(shown)
    if log:
        parts.append(f"--- full output kept in: {log}")
    if command := result.get("command"):
        parts.append(f"--- rerun this shard: {shlex.join(command)}")
    return "\n".join(parts)


def shard_timeout(raw: str) -> float:
    """``--shard-timeout``: a finite, non-negative number of seconds (0 disables).

    A negative timeout would otherwise reach ``communicate`` as an instant
    expiry and kill every shard on start.
    """
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not seconds >= 0 or math.isinf(seconds):
        raise argparse.ArgumentTypeError(
            f"must be a finite, non-negative number of seconds (0 disables), "
            f"not {raw!r}"
        )
    return seconds


def _env_timeout() -> float:
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_SHARD_TIMEOUT
    try:
        return shard_timeout(raw)
    except argparse.ArgumentTypeError as error:
        raise SystemExit(f"{TIMEOUT_ENV} {error}") from error


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
        # flaky tests rather than as the overload it is. Half rounded up, not
        # down: a three-core CI runner would otherwise run every shard serially.
        default=max(1, ((os.cpu_count() or 4) + 1) // 2),
        help="maximum parallel shard processes (default: half the CPU count)",
    )
    parser.add_argument(
        "--shard-timeout",
        type=shard_timeout,
        default=None,
        metavar="SECONDS",
        help=(
            f"kill a shard's process group after this long (default: "
            f"${TIMEOUT_ENV} or {DEFAULT_SHARD_TIMEOUT:g}; 0 disables)"
        ),
    )
    parser.add_argument(
        "--slice",
        type=parse_slice,
        default=(1, 1),
        metavar="K/N",
        help="run only the K-th of N disjoint slices of the shards (default: 1/1)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=os.environ.get(VERBOSE_ENV, "") not in ("", "0"),
        help=(
            f"print a failed shard's whole output, not its last "
            f"{FAILED_OUTPUT_LINES} lines (also ${VERBOSE_ENV}=1)"
        ),
    )
    args = parser.parse_args(argv)
    if args.check_registration:
        return check_registration()
    mode = "portable" if args.portable else "fast" if args.fast else "all"
    timeout = _env_timeout() if args.shard_timeout is None else args.shard_timeout

    shards = _shard_commands(mode, slice_=args.slice)
    started = time.monotonic()
    results = []
    # A SIGTERM (a stress run's cleanup trap, a CI cancel) must take the
    # shards down too, which an exception unwinding through here does.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs))
    try:
        futures = [
            pool.submit(_run_shard, name, command, timeout) for name, command in shards
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            verdict = _verdict(result)
            suffix = (
                f"  {describe_returncode(result['returncode'])}"
                if verdict != "ok"
                else ""
            )
            print(
                f"{verdict:>4}  {result['name']:<44} "
                f"{result['duration']:6.1f}s  {result['tests']:4d} tests{suffix}",
                flush=True,
            )
            if verdict == "ok":
                _clear_log(result)
            results.append(result)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        _kill_live_shards()
        raise
    pool.shutdown()
    _record_timings(results)

    failures = [result for result in results if _verdict(result) != "ok"]
    total_tests = sum(result["tests"] for result in results)
    wall = time.monotonic() - started
    for result in failures:
        print()
        print(failure_report(result, verbose=args.verbose, timeout=timeout))
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
    if args.slice != (1, 1):
        label += f" (slice {args.slice[0]}/{args.slice[1]})"
    verdict = "FAILED" if failures else "passed"
    print(
        f"\n{label} {verdict}: {total_tests} tests across {len(shards)} shards "
        f"in {wall:.1f}s ({len(failures)} failed shard(s))"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
