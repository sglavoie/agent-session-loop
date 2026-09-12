#!/usr/bin/env python3
"""Run every case this repository's sessions own.

This is the broad gate a trial's Completion Contract names. It is what the
runner executes after a worker turn, and running it is not part of a worker's
job unless something the worker did makes it one.
"""

from __future__ import annotations

import _observe
import cases


def main() -> int:
    failures: list[str] = []

    def body() -> None:
        for name in sorted(cases.SESSION_CASES):
            try:
                cases.SESSION_CASES[name]()
            except AssertionError as error:
                failures.append(f"{name}: {error}")
        if failures:
            raise AssertionError("; ".join(failures))

    return _observe.observed("contract", body)


if __name__ == "__main__":
    raise SystemExit(main())
