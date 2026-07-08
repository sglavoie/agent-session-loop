#!/usr/bin/env python3
"""Create a collision-safe output directory for a split session plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any


DEFAULT_DIR_NAME = "agent-plans"
HASH_PREFIX_LENGTH = 12


def default_base_dir() -> str:
    """Return the default directory used to store generated session plans.

    Persistent by design — macOS purges $TMPDIR on reboot, which would destroy
    in-flight multi-session plans. Resolution order:

    1. AGENT_PLANS_DIR (explicit override)
    2. $XDG_DATA_HOME/agent-plans (if set)
    3. ~/.local/share/agent-plans
    """
    agent_plans_dir = os.environ.get("AGENT_PLANS_DIR")
    if agent_plans_dir:
        return os.path.realpath(os.path.expanduser(agent_plans_dir))

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = os.path.join(os.path.expanduser(xdg_data_home), DEFAULT_DIR_NAME)
    else:
        base = os.path.join(os.path.expanduser("~/.local/share"), DEFAULT_DIR_NAME)
    return os.path.realpath(base)


def sanitize_slug(source_path: str) -> str:
    """Build a filesystem-safe slug from the plan filename."""
    filename = os.path.basename(source_path)
    stem, _ext = os.path.splitext(filename)
    slug_source = stem or filename
    slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")
    return slug or "plan"


def content_sha256(source_path: str) -> str:
    digest = hashlib.sha256()
    with open(source_path, "rb") as plan_file:
        for chunk in iter(lambda: plan_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(source_path: str, content_hash: str) -> str:
    canonical_path = os.path.realpath(os.path.abspath(source_path))
    fingerprint_input = canonical_path.encode("utf-8", "surrogateescape")
    fingerprint_input += b"\0"
    fingerprint_input += content_hash.encode("ascii")
    return hashlib.sha256(fingerprint_input).hexdigest()


def create_output_dir(base_dir: str, plan_id: str) -> tuple[str, int]:
    """Atomically create the first available output directory."""
    os.makedirs(base_dir, exist_ok=True)

    collision_index = 1
    while True:
        suffix = "" if collision_index == 1 else f"-{collision_index}"
        candidate = os.path.join(base_dir, f"{plan_id}{suffix}")
        try:
            os.mkdir(candidate)
        except FileExistsError:
            collision_index += 1
            continue
        return candidate, collision_index


def resolve_plan_output(plan_file: str, base_dir: str | None = None) -> dict[str, Any]:
    canonical_source = os.path.realpath(os.path.abspath(os.path.expanduser(plan_file)))
    selected_base_dir = (
        os.path.realpath(os.path.abspath(os.path.expanduser(base_dir)))
        if base_dir is not None
        else default_base_dir()
    )
    slug = sanitize_slug(canonical_source)
    content_hash = content_sha256(canonical_source)
    fingerprint = source_fingerprint(canonical_source, content_hash)
    content_hash_prefix = content_hash[:HASH_PREFIX_LENGTH]
    hash_prefix = fingerprint[:HASH_PREFIX_LENGTH]
    fingerprint_prefix = hash_prefix
    plan_id = f"{content_hash_prefix}-{fingerprint_prefix}"
    output_dir, collision_index = create_output_dir(selected_base_dir, plan_id)

    return {
        "output_dir": output_dir,
        "base_dir": selected_base_dir,
        "slug": slug,
        "source_path": canonical_source,
        "content_sha256": content_hash,
        "source_fingerprint": fingerprint,
        "content_hash_prefix": content_hash_prefix,
        "hash_prefix": hash_prefix,
        "fingerprint_prefix": fingerprint_prefix,
        "plan_id": plan_id,
        "collision_index": collision_index,
    }


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
