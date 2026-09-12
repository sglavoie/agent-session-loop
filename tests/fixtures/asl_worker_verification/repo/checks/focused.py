#!/usr/bin/env python3
"""Run one named case.

Usage: ``python3 checks/focused.py <case>``

Run as a script, so ``sys.path[0]`` is this directory and the two sibling
modules below import by name under any working directory.
"""

from __future__ import annotations

import sys

import _observe
import cases


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: focused.py <case>", file=sys.stderr)
        return 2
    case = cases.SESSION_CASES.get(argv[0])
    if case is None:
        known = ", ".join(sorted(cases.SESSION_CASES))
        print(f"unknown case {argv[0]!r}; known cases: {known}", file=sys.stderr)
        return 2
    return _observe.observed(f"focused:{argv[0]}", case)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
