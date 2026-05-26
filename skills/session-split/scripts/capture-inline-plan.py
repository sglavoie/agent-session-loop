#!/usr/bin/env python3
"""Capture pasted plan markdown as a durable source file for session-split."""

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
    agent_plans_dir = os.environ.get("AGENT_PLANS_DIR")
    if agent_plans_dir:
        return os.path.realpath(os.path.expanduser(agent_plans_dir))

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = os.path.join(os.path.expanduser(xdg_data_home), DEFAULT_DIR_NAME)
    else:
        base = os.path.join(os.path.expanduser("~/.local/share"), DEFAULT_DIR_NAME)
    return os.path.realpath(base)


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "plan"


def extract_plan(raw_input: str) -> str:
    """Return the last proposed_plan block, or the raw input if no block exists."""
    matches = re.findall(
        r"<proposed_plan>\s*(.*?)\s*</proposed_plan>",
        raw_input,
        flags=re.IGNORECASE | re.DOTALL,
    )
    content = matches[-1] if matches else raw_input
    content = content.strip()
    if not content:
        raise ValueError("no plan content found on stdin")
    return content + "\n"


def title_from_plan(content: str) -> str:
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return "plan"


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_source_file(content: str, sources_dir: str, slug: str, content_hash: str) -> tuple[str, bool]:
    os.makedirs(sources_dir, exist_ok=True)

    hash_prefix = content_hash[:HASH_PREFIX_LENGTH]
    collision_index = 1
    while True:
        suffix = "" if collision_index == 1 else f"-{collision_index}"
        path = os.path.join(sources_dir, f"{slug}-{hash_prefix}{suffix}.md")

        try:
            with open(path, "x", encoding="utf-8") as source_file:
                source_file.write(content)
            return path, True
        except FileExistsError:
            with open(path, encoding="utf-8") as source_file:
                if source_file.read() == content:
                    return path, False
            collision_index += 1


def capture_inline_plan(raw_input: str, base_dir: str | None = None) -> dict[str, Any]:
    selected_base_dir = (
        os.path.realpath(os.path.abspath(os.path.expanduser(base_dir)))
        if base_dir is not None
        else default_base_dir()
    )
    sources_dir = os.path.join(selected_base_dir, "sources")
    content = extract_plan(raw_input)
    title = title_from_plan(content)
    slug = sanitize_slug(title)
    content_hash = content_sha256(content)
    source_path, created = write_source_file(content, sources_dir, slug, content_hash)

    return {
        "base_dir": selected_base_dir,
        "sources_dir": sources_dir,
        "source_path": os.path.realpath(source_path),
        "title": title,
        "slug": slug,
        "content_sha256": content_hash,
        "hash_prefix": content_hash[:HASH_PREFIX_LENGTH],
        "created": created,
    }


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
