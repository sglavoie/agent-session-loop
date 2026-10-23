# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import run_suite_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .run_suite_shared import *
from .run_suite_part_1 import *
from .run_suite_part_2 import *

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

    The registration ratchet used to compare test *counts* -- discovery's total
    against the sum of what the shards reported running. A count is only a proxy
    for the question being asked, and it answers a different one as soon as a
    module declines to run: a module-level ``SkipTest`` reports ``Ran 0 tests``,
    which the count read as a module nobody had registered. Two modules in this
    suite skip that way by design, both because the thing they judge is an
    installed program rather than this source tree, so the proxy failed the
    suite for a legitimately unavailable dependency.

    Module names were the next proxy, and they broke once suites were split: a
    ``test_*`` module that star-imports its ``*_cases_*`` or ``*_section_*``
    helpers runs classes whose ``__module__`` is the helper, so the module a
    test is defined in and the module a shard runs stopped being the same name.
    Test ids answer the actual question -- does some shard run this test -- and
    are the same whichever module a class is reached through.
    """
    loader = unittest.TestLoader()
    suite = loader.discover(str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    if loader.errors:
        raise SystemExit("test discovery failed:\n" + "\n".join(loader.errors))
    return _test_ids(suite)


def _registered_test_ids(modules: set[str]) -> dict[str, set[str]]:
    """The test ids each registered module runs, keyed by module name."""
    loader = unittest.TestLoader()
    registered = {name: _test_ids(loader.loadTestsFromName(name)) for name in modules}
    if loader.errors:
        raise SystemExit(
            "loading run_suite modules failed:\n" + "\n".join(loader.errors)
        )
    return registered


def _defining_modules(ids: set[str]) -> list[str]:
    return sorted({test_id.rsplit(".", 2)[0] for test_id in ids})


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
        registered = set(FAST_MODULES) | set(INTEGRATION_MODULES)
        discovered = _discovered_test_ids()
        registered_ids = _registered_test_ids(registered)
        unregistered = discovered - set().union(*registered_ids.values())
        if unregistered:
            print(
                "\nFAIL: discovery finds tests in "
                f"{', '.join(_defining_modules(unregistered))} that no run_suite "
                "module runs; a test module is missing from run_suite lists"
            )
            return 1
        undiscovered = sorted(
            name for name, ids in registered_ids.items() if not ids & discovered
        )
        if undiscovered:
            print(
                f"\nFAIL: run_suite lists {', '.join(undiscovered)} but broad "
                "discovery finds no test there; the list names a module that "
                "moved, was renamed, or no longer holds tests"
            )
            return 1
        # A module that ran nothing is not a failure -- the two installed-pair
        # gates skip when the program they judge is not installed -- but it is
        # not a success either, and a suite that printed only `ok` for it would
        # report coverage it did not have.
        for result in results:
            if result["name"] in registered and result["tests"] == 0:
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
