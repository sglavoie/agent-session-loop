"""Render the always-loaded ASL startup card into harness context files.

The card is a compact route, not a copy of the workflow: it names
``asl supervisor-guide`` and the handful of supervision commands an agent needs
before it decides whether to load the whole skill. Every command comes from
``supervisor_guide.guide_payload`` so the card cannot drift from the guide.

Each managed file keeps its own content and receives one marked block. The
start marker carries a digest of the block body, so a stale block is detectable
without rewriting the file (``--check``).

The context files belong to the operator, not to this checkout, so they are
addressed by their installed path under a home directory. Where such a file is
a symlink into another repository the write lands in that repository's tracked
file, which is the change that repository then commits. The bundled reference
is the one file this checkout owns.

Only harness roots that already exist are rendered into (``~/.claude``,
``~/.codex``, ``~/.copilot``, plus any Claude profile named by ``$CLAUDE_CONFIG_DIR`` or
``$ASL_CLAUDE_CONFIG_DIRS``); a missing harness directory is reported as
skipped and never created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import durability, install_verify_common, supervisor_guide

COMMAND_NAME = "startup-context"

START_MARKER_PREFIX = "<!-- ASL_CONTEXT_START"

END_MARKER = "<!-- ASL_CONTEXT_END -->"

#: First line of every rendered card body. It is the only structural signal that
#: identifies card prose that has escaped its markers.
CARD_HEADING = "## ASL Supervision"

#: Card-only prose and command labels used to recognize residue after a marker
#: or heading is damaged. These signals are deliberately narrower than a bare
#: ``asl`` mention so unrelated startup instructions remain unmanaged.
CARD_RESIDUE_PREFIXES: tuple[str, ...] = (
    "Supervising an `asl run` starts here.",
    "Run the route command for anything not listed",
    "- Route: `asl ",
    "- Skill:",
    "- Reference:",
    "- supervise: `asl ",
    "- reply: `asl ",
    "- guidance: `asl ",
    "- status: `asl ",
)

#: Operations lifted from the guide model onto the card, in card order.
CARD_OPERATIONS: tuple[str, ...] = ("supervise", "reply", "guidance", "status")

REFERENCE_PATH = "skills/asl/references/supervisor-context.md"

#: Path of the generated reference relative to an installed skill directory.
INSTALLED_REFERENCE_PATH = "asl/references/supervisor-context.md"

#: The reference is shared by every harness, so it lists all of them.
REFERENCE_HARNESS = supervisor_guide.AUTO_HARNESS

#: Rendering never consults the ambient environment: generated files must be a
#: pure function of the guide model.
RENDER_ENV: Mapping[str, str] = {}

#: The command that creates a missing context file and refreshes a stale one.
#: ``./install.py`` alone links skills but never renders the card.
SYNC_REMEDY = "just sync-skills"


@dataclass(frozen=True)
class ContextTarget:
    """One startup-context file, addressed relative to a home directory."""

    installed_path: str
    harness: str


#: Every harness that loads a rendered card at session start.
CONTEXT_HARNESSES: tuple[str, ...] = tuple(install_verify_common.CONTEXT_FILES)


def context_targets(
    roots: Iterable[install_verify_common.HarnessRoot],
) -> tuple[ContextTarget, ...]:
    """Return the context file of every discovered root whose harness has one.

    Only roots that already exist are passed in (see
    ``install_verify_common.discover_harness_roots``), so rendering never
    creates a harness configuration directory.
    """
    targets: list[ContextTarget] = []
    for root in roots:
        context_file = root.context_file
        if context_file is not None:
            targets.append(ContextTarget(context_file, root.harness))
    return tuple(targets)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def card_lines(payload: Mapping[str, Any]) -> list[str]:
    """Build the card body from the guide model."""
    invocations = payload["skills"][0]["invocations"]
    if len(invocations) == 1:
        (invocation,) = invocations.values()
        skill_line = f"- Skill: `{invocation}`"
    else:
        rendered = "; ".join(
            f"{harness} `{invocation}`" for harness, invocation in invocations.items()
        )
        skill_line = f"- Skill: {rendered}"

    commands = {
        operation["name"]: operation["command"] for operation in payload["operations"]
    }
    lines = [
        CARD_HEADING,
        "",
        "Supervising an `asl run` starts here. The `supervise` command below is the",
        "loop: run it first, reissue it after every IDLE, and stop on DONE or LOST.",
        "Run the route command for anything not listed - the detached helper launch,",
        "`asl inbox resolve`, and which leaf `--help` to read - and load the full",
        "skill only when the route is not enough.",
        "",
        "- Route: `asl supervisor-guide` (add `--json` for the same model as data).",
        skill_line,
        f"- Reference: `{INSTALLED_REFERENCE_PATH}` under the skills directory.",
    ]
    lines.extend(f"- {name}: `{commands[name]}`" for name in CARD_OPERATIONS)
    return lines


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def render_block(harness: str) -> str:
    """Render the marked card block for one harness, without a trailing newline."""
    payload = supervisor_guide.guide_payload(harness=harness, env=RENDER_ENV)
    body = "\n".join(card_lines(payload))
    return f"{START_MARKER_PREFIX} {_digest(body)} -->\n{body}\n{END_MARKER}"


def start_marker(harness: str) -> str:
    """Return the current start marker for one harness."""
    return render_block(harness).split("\n", 1)[0]


def _is_boundary(line: str) -> bool:
    """Report whether ``line`` starts content an orphaned card body cannot own."""
    return line.startswith("#") or line.startswith("<!--")


def _is_card_residue(line: str) -> bool:
    """Report whether ``line`` is structurally specific to the startup card."""
    stripped = line.strip()
    if stripped == CARD_HEADING:
        return True
    if stripped.startswith("## ") and not stripped.startswith("### "):
        heading = stripped[3:].strip().casefold()
        if "asl" in heading and heading.startswith("asl supervis"):
            return True
    return stripped.startswith(CARD_RESIDUE_PREFIXES)


def _orphan_span_end(lines: Sequence[str], start: int) -> int:
    """Return the last line of the card body under an unterminated start marker.

    A damaged or missing card heading is recoverable when the following prose
    still carries a card-specific structural signal. The candidate runs to the
    last card-specific line before the next heading or HTML comment, so
    neighbouring generated blocks, unrelated sections, and the operator's own
    prose written directly after the card are never absorbed. When the body
    cannot be identified the span is the marker line alone and any recognizable
    residue is left for ``block_problem`` to reject.
    """
    if start + 1 >= len(lines):
        return start

    candidate_start = start + 1
    end = start
    scan_start = candidate_start
    if lines[candidate_start].startswith("#"):
        # A heading directly under the marker is the card's, damaged or not,
        # once card prose below it confirms that.
        scan_start += 1
    elif _is_boundary(lines[candidate_start]):
        return start

    for index in range(candidate_start, len(lines)):
        line = lines[index]
        if index >= scan_start and _is_boundary(line):
            break
        if _is_card_residue(line):
            end = index
    if end == start:
        return start
    return end


def _span_end(lines: Sequence[str], start: int) -> int:
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line == END_MARKER:
            return index
        if line.startswith(START_MARKER_PREFIX):
            break
    return _orphan_span_end(lines, start)


def _block_spans(lines: Sequence[str]) -> list[tuple[int, int]]:
    """Return inclusive line spans of managed blocks, malformed markers included.

    A start marker is recognized by its prefix alone, so a corrupted digest is
    still treated as a managed block rather than as unrelated text. A start
    marker pairs only with an end marker that precedes any later start marker;
    with no such end marker the span covers the orphaned card body when that
    body can be safely identified, and otherwise the marker line alone. A stray
    end marker spans its own line only.
    """
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(START_MARKER_PREFIX):
            end = _span_end(lines, index)
            spans.append((index, end))
            index = end + 1
            continue
        if line == END_MARKER:
            spans.append((index, index))
        index += 1
    return spans


def managed_blocks(text: str) -> list[str]:
    """Return every managed block in ``text``, malformed ones included."""
    lines = text.split("\n")
    return ["\n".join(lines[start : end + 1]) for start, end in _block_spans(lines)]


def apply_block(text: str, block: str) -> str:
    """Collapse every managed block into one canonical block.

    All bytes outside the managed blocks are preserved, including trailing
    blank lines at end of file; only blank lines directly around the block are
    normalized so the result is idempotent.
    """
    trailing_newlines = len(text) - len(text.rstrip("\n"))
    if text == "":
        # Newly created generated files follow the repository's text-file
        # convention even though there are no pre-existing bytes to preserve.
        trailing_newlines = 1
    body = text[:-trailing_newlines] if trailing_newlines else text
    lines = body.split("\n") if body else []

    spans = _block_spans(lines)
    if spans:
        head = lines[: spans[0][0]]
        managed = {index for start, end in spans for index in range(start, end + 1)}
        tail = [
            line
            for index, line in enumerate(lines)
            if index > spans[0][1] and index not in managed
        ]
    else:
        head = list(lines)
        tail = []

    while head and not head[-1].strip():
        head.pop()
    while tail and not tail[0].strip():
        tail.pop(0)

    rendered = list(head)
    if head:
        rendered.append("")
    rendered.extend(block.split("\n"))
    if tail:
        rendered.append("")
        rendered.extend(tail)
    return "\n".join(rendered) + ("\n" * trailing_newlines)


def _card_text_outside_blocks(text: str) -> bool:
    """Report whether card prose survives outside every managed block."""
    lines = text.split("\n")
    managed = {
        index for start, end in _block_spans(lines) for index in range(start, end + 1)
    }
    return any(
        _is_card_residue(line)
        for index, line in enumerate(lines)
        if index not in managed
    )


def block_problem(text: str, expected_block: str) -> str | None:
    """Describe why ``text`` fails to carry exactly one ``expected_block``."""
    expected_marker = expected_block.split("\n", 1)[0]
    blocks = managed_blocks(text)
    if not blocks:
        return f"is missing the ASL startup block {expected_marker}"
    if len(blocks) > 1:
        return (
            f"carries {len(blocks)} ASL startup blocks; "
            f"expected exactly one {expected_marker}"
        )
    found = blocks[0]
    if found == expected_block:
        if _card_text_outside_blocks(text):
            return (
                "carries ASL card text outside "
                f"{expected_marker}; remove the unmanaged copy"
            )
        return None
    if found.split("\n", 1)[0] == expected_marker:
        return f"carries a modified ASL block body under {expected_marker}"
    return f"carries a stale ASL block; expected {expected_marker}"


def render_reference() -> str:
    """Render the fully generated supervisor reference bundled with the skill."""
    payload = supervisor_guide.guide_payload(harness=REFERENCE_HARNESS, env=RENDER_ENV)
    lines = [
        "# ASL Supervisor Context",
        "",
        "Generated by `just sync-skills`; edit",
        "`agent_session_loop/startup_context.py` instead of this file.",
        "",
        "The block below is the same card the harness loads at session start.",
        "",
        render_block(REFERENCE_HARNESS),
        "",
        "## Full guide",
        "",
        "`asl supervisor-guide` is authoritative; this copy exists so the skill",
        "can answer without shelling out.",
        "",
        "```text",
        supervisor_guide.render_text(payload),
        "```",
    ]
    return "\n".join(lines) + "\n"


def _replace_file(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, keeping its mode and its link.

    A reader (a harness starting up mid-sync) sees the old file or the new one,
    never a truncated one. A symlinked context file is written at its target,
    so the link survives and the change lands in the repository that owns the
    file; an existing file keeps its permission bits.
    """
    target = path.resolve() if path.is_symlink() else path
    try:
        permissions: int | None = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        permissions = None
    durability.replace_text(target, text, permissions=permissions)


def _with_line_endings_of(path: Path, text: str) -> str:
    """Return ``text`` in the line endings ``path`` already uses.

    ``read_text`` translates CRLF, so every comparison here runs on LF text;
    writing that back as-is would silently convert an operator's CRLF file.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return text
    if raw.count(b"\r\n") * 2 > raw.count(b"\n"):
        return text.replace("\n", "\r\n")
    return text


def _file_record(
    path: Path, root: Path, current: str, desired: str, *, missing: bool = False
) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(root)),
        "stale": current != desired,
        "missing": missing,
        "unreadable": None,
    }


def _unreadable_record(path: Path, root: Path, error: str) -> dict[str, Any]:
    """A context file that exists but cannot be read as UTF-8 text.

    It is neither stale nor missing: nobody can tell what it holds, so a sync
    leaves it exactly as it is rather than rendering over an operator's file.
    """
    return {
        "path": str(path.relative_to(root)),
        "stale": False,
        "missing": False,
        "unreadable": error,
    }


def _read_current(path: Path) -> str:
    """``path``'s text, or ``""`` when it does not exist yet.

    Raises ``OSError`` or ``UnicodeDecodeError`` for a file that exists but
    cannot be read; the caller reports it instead of rewriting it.
    """
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def sync(
    repo_root: str | Path | None = None,
    *,
    home: str | Path | None = None,
    check: bool = False,
    reference_only: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Render every managed startup-context file, or report stale ones.

    ``reference_only`` limits the pass to the bundled reference, which is what
    a machine with no harness installed (CI) can judge. Context files are
    rendered only under harness roots that already exist; ``env`` (default:
    the process environment) supplies the extra Claude roots.
    """
    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    home_root = Path(home).expanduser() if home is not None else Path.home()
    discovery = install_verify_common.discover_harness_roots(home_root, env)
    files: list[dict[str, Any]] = []

    for target in () if reference_only else context_targets(discovery.found):
        path = home_root / target.installed_path
        exists = path.is_file()
        try:
            current = _read_current(path)
        except (OSError, UnicodeDecodeError) as error:
            files.append(_unreadable_record(path, home_root, str(error)))
            continue
        desired = apply_block(current, render_block(target.harness))
        files.append(
            _file_record(path, home_root, current, desired, missing=not exists)
        )
        if not check and current != desired:
            _replace_file(path, _with_line_endings_of(path, desired))

    reference = root / REFERENCE_PATH
    exists = reference.is_file()
    try:
        current = _read_current(reference)
    except (OSError, UnicodeDecodeError) as error:
        files.append(_unreadable_record(reference, root, str(error)))
    else:
        desired = render_reference()
        files.append(
            _file_record(reference, root, current, desired, missing=not exists)
        )
        if not check and current != desired:
            reference.parent.mkdir(parents=True, exist_ok=True)
            _replace_file(reference, desired)

    # A missing file is also stale (its desired content differs from nothing),
    # so ``stale`` keeps naming every file a sync would write; ``missing`` is
    # the subset that does not exist yet and that a sync would create.
    stale = [record["path"] for record in files if record["stale"]]
    missing = [record["path"] for record in files if record["missing"]]
    unreadable = [record["path"] for record in files if record["unreadable"]]
    return {
        "ok": not unreadable and not (check and stale),
        "command": COMMAND_NAME,
        "repo_root": str(root),
        "home": str(home_root),
        "checked": check,
        "files": files,
        "stale": stale,
        "missing": missing,
        "unreadable": unreadable,
        "remedy": SYNC_REMEDY,
        "written": [] if check else stale,
        "harness_roots": {**discovery.payload(), "note": discovery.note()},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the ASL startup card into harness context files.",
    )
    parser.add_argument(
        "--repo-root",
        help="Checkout root to render into (default: inferred from this module)",
    )
    parser.add_argument(
        "--home",
        help="Home directory whose harness context files are rendered (default: $HOME)",
    )
    parser.add_argument(
        "--reference-only",
        action="store_true",
        help="Render or check only the bundled skill reference",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale generated blocks without rewriting any file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = sync(
        args.repo_root,
        home=args.home,
        check=args.check,
        reference_only=args.reference_only,
    )
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload["ok"] else 1
    if not args.reference_only:
        print(payload["harness_roots"]["note"])
    for record in payload["files"]:
        if record["unreadable"]:
            print(
                f"unreadable: {record['path']} ({record['unreadable']}); "
                "left as it is: save it as UTF-8 to render the startup card"
            )
    if args.check:
        if payload["stale"]:
            missing = set(payload["missing"])
            for path in payload["stale"]:
                print(f"missing: {path}" if path in missing else f"stale: {path}")
            print(f"run `{SYNC_REMEDY}` to render the startup card")
        elif not payload["unreadable"]:
            print("startup context is up to date")
    else:
        for path in payload["written"]:
            print(f"rendered: {path}")
        if not payload["written"] and not payload["unreadable"]:
            print("startup context is up to date")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
