"""Source-plan capture, content hashing, and collision-safe split output paths."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    plans_files,
    plans_readiness,
    plans_snapshot,
)
from .plans_errors import PlanError

DEFAULT_AGENT_PLANS_DIR_NAME = "agent-plans"
HASH_PREFIX_LENGTH = 12


class DuplicateSplitError(PlanError):
    """Raised when a split plan id already has durable output directories."""

    def __init__(self, plan_id: str, existing_outputs: Sequence[str]) -> None:
        self.plan_id = plan_id
        self.existing_outputs = tuple(existing_outputs)
        outputs = "; ".join(self.existing_outputs)
        super().__init__(f"split output already exists for plan {plan_id}: {outputs}")


# ---------------------------------------------------------------------------
# Inline-source capture and output resolution (importable APIs)
# ---------------------------------------------------------------------------


def default_base_dir() -> str:
    """Return the persistent agent-plans base directory.

    Resolution order mirrors the standalone scripts: ``AGENT_PLANS_DIR`` wins,
    then ``$XDG_DATA_HOME/agent-plans``, then ``~/.local/share/agent-plans``.
    ``$TMPDIR`` is intentionally never consulted (macOS purges it on reboot).
    """
    agent_plans_dir = os.environ.get("AGENT_PLANS_DIR")
    if agent_plans_dir:
        return os.path.realpath(os.path.expanduser(agent_plans_dir))

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = os.path.join(
            os.path.expanduser(xdg_data_home), DEFAULT_AGENT_PLANS_DIR_NAME
        )
    else:
        base = os.path.join(
            os.path.expanduser("~/.local/share"), DEFAULT_AGENT_PLANS_DIR_NAME
        )
    return os.path.realpath(base)


def _resolve_base_dir(base_dir: str | os.PathLike[str] | None) -> str:
    if base_dir is None:
        return default_base_dir()
    return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(base_dir))))


def slugify(value: str) -> str:
    """Collapse a string to a filesystem-safe ``[a-z0-9-]`` slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "plan"


def slug_from_source_path(source_path: str | os.PathLike[str]) -> str:
    """Build a slug from a plan filename stem."""
    filename = os.path.basename(os.fspath(source_path))
    stem, _ext = os.path.splitext(filename)
    return slugify(stem or filename)


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_file(source_path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(source_path, "rb") as plan_file:
        for chunk in iter(lambda: plan_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(source_path: str | os.PathLike[str], content_hash: str) -> str:
    """Fingerprint a plan by canonical source path plus content hash."""
    canonical_path = os.path.realpath(os.path.abspath(os.fspath(source_path)))
    fingerprint_input = canonical_path.encode("utf-8", "surrogateescape")
    fingerprint_input += b"\0"
    fingerprint_input += content_hash.encode("ascii")
    return hashlib.sha256(fingerprint_input).hexdigest()


def extract_inline_plan(raw_input: str) -> str:
    """Return the last ``<proposed_plan>`` block, else the raw input.

    Raises :class:`ValueError` when no non-empty plan content is present.
    """
    matches = re.findall(
        r"<proposed_plan>\s*(.*?)\s*</proposed_plan>",
        raw_input,
        flags=re.IGNORECASE | re.DOTALL,
    )
    content = (matches[-1] if matches else raw_input).strip()
    if not content:
        raise ValueError("no plan content found on stdin")
    return content + "\n"


def title_from_plan(content: str) -> str:
    """Return the first ``# H1`` title in a plan, or ``"plan"``."""
    return plans_files.plan_title_from_markdown(content) or "plan"


def _write_source_file(
    content: str, sources_dir: str, slug: str, content_hash: str
) -> tuple[str, bool]:
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


def capture_inline_plan(
    raw_input: str, base_dir: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Persist pasted plan markdown as a durable source file.

    Canonical implementation behind ``asl split --inline-stdin`` (formerly the
    retired ``capture-inline-plan.py`` script); returns JSON-serializable keys.
    """
    selected_base_dir = _resolve_base_dir(base_dir)
    sources_dir = os.path.join(selected_base_dir, "sources")
    content = extract_inline_plan(raw_input)
    title = title_from_plan(content)
    slug = slugify(title)
    content_hash = sha256_text(content)
    source_path, created = _write_source_file(content, sources_dir, slug, content_hash)

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


def _describe_existing_split_output(path: Path) -> str:
    canonical = path.resolve()
    if not canonical.is_dir():
        return f"{canonical} [not-a-directory]"
    try:
        plans_snapshot.inspect_plan(canonical)
        metadata = plans_readiness.read_readiness_metadata(canonical)
    except PlanError as error:
        return f"{canonical} [invalid: {error}]"
    readiness = metadata.state if metadata is not None else "unknown"
    return f"{canonical} [{readiness}]"


def _existing_split_outputs(base_dir: str, plan_id: str) -> tuple[str, ...]:
    pattern = re.compile(rf"^{re.escape(plan_id)}(?:-(?P<suffix>[1-9][0-9]*))?$")
    matches: list[tuple[int, str]] = []
    try:
        for entry in Path(base_dir).iterdir():
            matched = pattern.fullmatch(entry.name)
            if matched is None:
                continue
            sort_key = (
                1 if matched.group("suffix") is None else int(matched.group("suffix"))
            )
            matches.append((sort_key, _describe_existing_split_output(entry)))
    except OSError as error:
        raise PlanError(
            f"cannot scan existing split outputs under {base_dir}: {error}"
        ) from error
    matches.sort(key=lambda item: item[0])
    return tuple(description for _sort_key, description in matches)


def _create_output_dir(
    base_dir: str, plan_id: str, *, force_new: bool = False
) -> tuple[str, int]:
    os.makedirs(base_dir, exist_ok=True)
    if not force_new:
        existing_outputs = _existing_split_outputs(base_dir, plan_id)
        if existing_outputs:
            raise DuplicateSplitError(plan_id, existing_outputs)

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


def resolve_plan_output(
    plan_file: str | os.PathLike[str],
    base_dir: str | os.PathLike[str] | None = None,
    *,
    force_new: bool = False,
) -> dict[str, object]:
    """Create a fresh split output directory for a plan.

    Canonical implementation behind ``asl split`` output resolution (formerly
    the retired ``resolve-plan-output.py`` script); returns JSON-serializable
    keys. Duplicate plan ids are refused unless ``force_new=True``.
    """
    canonical_source = os.path.realpath(
        os.path.abspath(os.path.expanduser(os.fspath(plan_file)))
    )
    selected_base_dir = _resolve_base_dir(base_dir)
    slug = slug_from_source_path(canonical_source)
    content_hash = sha256_file(canonical_source)
    fingerprint = source_fingerprint(canonical_source, content_hash)
    content_hash_prefix = content_hash[:HASH_PREFIX_LENGTH]
    fingerprint_prefix = fingerprint[:HASH_PREFIX_LENGTH]
    plan_id = f"{content_hash_prefix}-{fingerprint_prefix}"
    output_dir, collision_index = _create_output_dir(
        selected_base_dir, plan_id, force_new=force_new
    )

    return {
        "output_dir": output_dir,
        "base_dir": selected_base_dir,
        "slug": slug,
        "source_path": canonical_source,
        "content_sha256": content_hash,
        "source_fingerprint": fingerprint,
        "content_hash_prefix": content_hash_prefix,
        "hash_prefix": fingerprint_prefix,
        "fingerprint_prefix": fingerprint_prefix,
        "plan_id": plan_id,
        "collision_index": collision_index,
    }
