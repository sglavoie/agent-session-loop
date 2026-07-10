#!/usr/bin/env python3
"""Capture pasted plan markdown as a durable source file for session-split.

Thin wrapper: the canonical implementation lives in
``agent_session_loop.authoring``. This script only preserves the CLI surface
(stdin plan content, ``--base-dir``, JSON on stdout) and re-exports the
historical function names for existing importers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_session_loop import authoring  # noqa: E402


DEFAULT_DIR_NAME = authoring.DEFAULT_AGENT_PLANS_DIR_NAME
HASH_PREFIX_LENGTH = authoring.HASH_PREFIX_LENGTH

default_base_dir = authoring.default_base_dir
sanitize_slug = authoring.slugify
extract_plan = authoring.extract_inline_plan
title_from_plan = authoring.title_from_plan
content_sha256 = authoring.sha256_text
capture_inline_plan = authoring.capture_inline_plan


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture pasted session-split plan markdown as a durable source file."
    )
    parser.add_argument(
        "--base-dir",
        help="Directory under which the sources/ directory should be created",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    raw_input = sys.stdin.read()
    try:
        result = capture_inline_plan(raw_input, args.base_dir)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
