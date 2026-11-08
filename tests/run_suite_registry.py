"""Derive suite lanes and verify that each discovered test has one shard."""

from __future__ import annotations

import ast
import collections
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
LANE_MARKER = "ASL_TEST_LANE"
PORTABLE_MARKER = "ASL_TEST_PORTABLE"
SHARDS_MARKER = "ASL_TEST_SHARDS"
_MARKERS = (LANE_MARKER, PORTABLE_MARKER, SHARDS_MARKER)
_LANES = ("fast", "integration")


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
