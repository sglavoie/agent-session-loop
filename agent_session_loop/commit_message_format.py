"""Commit message format."""

from __future__ import annotations

import json
import textwrap
from typing import cast

from . import commit_message_diff as diff
from . import commit_message_models as models

# ---------------------------------------------------------------------------
# Message extraction & formatting (ported from format-commit.py)
# ---------------------------------------------------------------------------


def looks_like_api_error(response: str) -> bool:
    """True when the harness returned an error body instead of a message.

    Catches both ``API Error: 500 {"type":"error",...}`` and a bare error
    object forwarded as if it were message text.  The guard matters far more
    headless than interactive: nobody is watching to notice that the "commit
    message" is a stack of JSON.
    """
    stripped = response.strip()
    if stripped.startswith("API Error:"):
        return True
    # A conventional title line means a real message, even when its body ends
    # in an error-shaped JSON example. A title holding a brace is still checked,
    # so an ``error: {"type": "error", ...}`` dump cannot pass as a title.
    title = stripped.split("\n", 1)[0].strip()
    if "{" not in title and models.COMMIT_TITLE_PATTERN.match(title):
        return False
    brace_pos = stripped.find("{")
    if brace_pos != -1:
        try:
            parsed = json.loads(stripped[brace_pos:])
        except ValueError:
            return False
        return isinstance(parsed, dict) and parsed.get("type") == "error"
    return False


def _is_disallowed_trailer(line: str) -> bool:
    return line.strip().lower().startswith(models._DISALLOWED_TRAILERS)


def strip_disallowed_trailers(text: str) -> str:
    """Drop attribution trailer lines and the blank lines they left behind.

    Some harnesses ignore the no-trailer prompt rule and append
    ``Co-authored-by:`` or ``Signed-off-by:`` anyway.  The match is anchored
    to the start of the line rather than run as a substring search, so a body
    sentence *about* trailers survives while a real trailer does not.
    """
    lines = [line for line in text.split("\n") if not _is_disallowed_trailer(line)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def extract_commit_message(text: str) -> str:
    """Return the message from its first conventional-commit title onward.

    Preamble such as "I've read the diff..." is dropped.  Text with no title
    line at all falls back to the whole response; either way disallowed
    trailers are stripped.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if models.COMMIT_TITLE_PATTERN.match(line.strip()):
            return strip_disallowed_trailers("\n".join(lines[index:]))
    return strip_disallowed_trailers(text)


def detect_line_type(line: str) -> tuple[str, str | tuple[str, str, str]]:
    """Classify a line as ``blank``, ``bullet``, or ``paragraph``.

    A bullet decomposes into its indent, its marker, and the text after the
    marker; everything else is returned unchanged.
    """
    if not line.strip():
        return "blank", line

    bullet_match = models._BULLET_PATTERN.match(line)
    if bullet_match:
        indent = bullet_match.group(1)
        bullet = bullet_match.group(2)
        return "bullet", (indent, bullet, line[bullet_match.end() :])

    return "paragraph", line


def format_bullet(indent: str, bullet: str, text: str, width: int = 80) -> str:
    """Wrap one bullet, hanging continuation lines under its text column."""
    first_line_prefix = f"{indent}{bullet} "
    return textwrap.fill(
        text,
        width=width,
        initial_indent=first_line_prefix,
        subsequent_indent=" " * len(first_line_prefix),
        break_long_words=False,
        break_on_hyphens=False,
    )


def format_paragraph(
    text: str,
    width: int = 80,
    initial_indent: str = "",
    subsequent_indent: str = "",
) -> str:
    """Wrap a paragraph, inheriting its own leading indent when none is given."""
    if not initial_indent:
        match = models._LEADING_INDENT_PATTERN.match(text)
        if match:
            initial_indent = match.group(1)
            subsequent_indent = initial_indent

    return textwrap.fill(
        text.strip(),
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def format_commit_message(text: str, width: int = 80, *, extract: bool = True) -> str:
    """Normalize a generated message into committable form.

    The first non-blank line is the title and is kept verbatim, however long
    it is; paragraphs and bullets below it are re-wrapped at ``width`` after
    consecutive lines are joined, and blank lines are preserved as written.
    ``extract=False`` skips the preamble cut for a message an operator wrote,
    whose title need not be conventional, so only disallowed trailers go.
    """
    text = extract_commit_message(text) if extract else strip_disallowed_trailers(text)
    lines = text.split("\n")
    result: list[str] = []

    seen_title = False

    index = 0
    while index < len(lines):
        line = lines[index]
        line_type, content = detect_line_type(line)

        if line_type == "blank":
            result.append("")
            index += 1
            continue

        if not seen_title:
            result.append(line.rstrip())
            seen_title = True
            index += 1
            continue

        if line_type == "bullet":
            indent, bullet, bullet_text = cast(tuple[str, str, str], content)
            # Unbroken paragraph lines below a bullet are its continuation.
            text_parts = [bullet_text.strip()]
            while index + 1 < len(lines):
                next_type, _ = detect_line_type(lines[index + 1])
                if next_type != "paragraph":
                    break
                index += 1
                text_parts.append(lines[index].strip())
            result.append(format_bullet(indent, bullet, " ".join(text_parts), width))
            index += 1
            continue

        paragraph_lines = [line]
        while index + 1 < len(lines):
            next_type, _ = detect_line_type(lines[index + 1])
            if next_type != "paragraph":
                break
            index += 1
            paragraph_lines.append(lines[index])
        combined = " ".join(entry.strip() for entry in paragraph_lines)
        result.append(format_paragraph(combined, width))
        index += 1

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_truncation_note(diff_result: models.DiffResult) -> str:
    """Tell the model what the budget removed; empty when nothing was removed.

    This is the prompt's prose counterpart to ``build_truncation_notice``,
    which is the terse notice the ``--prepare-only`` header carries.
    """
    if not diff_result.is_truncated:
        return ""

    parts: list[str] = []
    if diff_result.binary_files:
        parts.append(f"{len(diff_result.binary_files)} binary file(s)")
    if diff_result.truncated_files:
        parts.append(f"{len(diff_result.truncated_files)} file(s) partially shown")
    if diff_result.omitted_files:
        listed = diff._capped_file_list(
            diff_result.omitted_files, models._PROMPT_NAME_LIMIT
        )
        parts.append(f"{len(diff_result.omitted_files)} file(s) omitted: {listed}")
    summary = "; ".join(parts)

    return f"""
Note: This diff has been truncated to fit within size limits. {summary}.
Generate a commit message based on the changes shown. If the changes seem incomplete,
focus on what IS visible rather than speculating about omitted content.
"""


def build_commit_prompt(
    diff_result: models.DiffResult, guidance: str | None = None
) -> str:
    """Build the Conventional Commits prompt around an already-budgeted diff.

    The diff is always inlined under ``## Git Diff`` rather than written to a
    file for the harness to read: reading it back requires granting a Read
    tool, and Copilot's streaming filter drops the final assistant message
    once tool requests interleave.  The 150 KB budget exists so that inlining
    is always affordable.
    """
    guidance_note = f"\n## User Guidance\n\n{guidance}\n" if guidance else ""
    truncation_note = build_truncation_note(diff_result)

    return f"""\
You are a commit message generator. Analyze the following git diff and generate
a properly formatted commit message following the Conventional Commits specification.

## Format Requirements

Use this format:
<type>: <short description (max 72 chars)>

<body paragraph explaining WHAT changed and WHY>

<optional bullet points for multi-aspect commits>

## Type Conventions

- feat: New feature or capability
- fix: Bug fix
- docs: Documentation changes only
- refactor: Code restructuring without behavior change
- test: Test additions or modifications
- chore: Maintenance tasks (dependencies, configs)
- perf: Performance improvements
- style: Code style/formatting changes

## Content Rules

- Use PRESENT TENSE ("Add" not "Added")
- Focus on WHAT changed and WHY it matters
- Do NOT describe HOW the code works (implementation details)
- No spec-specific language (no "phase 1", "task 2", "spec section")
- No markdown syntax (no **bold**, _italic_, or `code`)
- No author attribution (no "Co-authored-by:", "Signed-off-by:", or similar trailers)
- Keep title under 72 characters
- Wrap body at 80 characters
- For multi-aspect commits, use bullet points with 2-space indent

## Output Format

Return ONLY the commit message text. No code blocks, no explanations, no
surrounding text. Title on first line, blank line, then body paragraphs.
{guidance_note}{truncation_note}
## Git Diff

{diff_result.diff}

Generate the commit message:"""
