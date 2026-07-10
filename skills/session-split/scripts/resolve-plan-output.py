#!/usr/bin/env python3
"""Create a collision-safe output directory for a split session plan.

Thin wrapper: the canonical implementation lives in
``agent_session_loop.authoring``. This script only preserves the CLI surface
(positional plan file, ``--base-dir``, JSON on stdout) and re-exports the
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
sanitize_slug = authoring.slug_from_source_path
content_sha256 = authoring.sha256_file
source_fingerprint = authoring.source_fingerprint
resolve_plan_output = authoring.resolve_plan_output


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a collision-safe output directory for a split session plan."
    )
    parser.add_argument("plan_file", help="Path to the source plan file")
    parser.add_argument(
        "--base-dir",
        help="Directory under which the resolver should create the output directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = resolve_plan_output(args.plan_file, args.base_dir)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
