"""Command-line options and environment defaults for the suite driver."""

from __future__ import annotations

import argparse
import math
import os

TIMEOUT_ENV = "ASL_TEST_SHARD_TIMEOUT"
VERBOSE_ENV = "ASL_TEST_VERBOSE"
DEFAULT_SHARD_TIMEOUT = 900.0
FAILED_OUTPUT_LINES = 200


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


def parse_args(argv: list[str] | None, description: str | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    return parser.parse_args(argv)
