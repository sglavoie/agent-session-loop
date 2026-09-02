"""The markdown body of a generated review session, and what bounds its scope.

:mod:`agent_session_loop.review_session_contract` says what a generated
review session must *prove*. This module says what the reviewer is actually
handed: the instructions, the repository base, the delta scope, and the
handoff stub — assembled into the one file the review harness opens.

Four things live here together because they are one document read from four
directions:

* **Assembly.** ``build_generated_review_session_body`` renders the whole
  file, splicing the contract section authored next door between the scope
  it computed and the handoff stub it appends. Its repository-aware branch
  is chosen by one input — a resolved workspace — which selects the
  repository-aware artifact and session versions together, so a body can
  never advertise one and validate against the other.
* **Scope.** ``_review_scope_preamble_lines``, ``_review_scope_detail_lines``,
  and ``_delta_review_scope_lines`` render the ``## Delta Review Scope``
  section. Scope is fail-open: ``REVIEW_SCOPE_FULL`` is what any delta that
  Git could not prove degrades to, and the rendered prose says so in the
  reviewer's own words rather than leaving a narrow-looking section behind.
* **Re-scoping in place.** ``rescoped_review_session_body`` and
  ``refreshed_review_repository_base`` rewrite one section of an existing
  body against current evidence. Both are bounded by the ``## `` headings
  this module owns, and both carry the surrounding text through verbatim, so
  a refresh can never rewrite the instructions or the contract.
* **Discovery.** ``find_pending_generated_review_session`` and
  ``ensure_generated_review_session`` decide whether a review session must
  be authored at all. Reuse is the default: a pending generated review keeps
  the body it was written with, and ``delta`` narrows a newly written session
  only.

Unlike the extractions before it, this module owns one durable write.
``ensure_generated_review_session`` writes ``session-<id>.md`` into the plan
directory when no pending generated review exists, and that write is the
point of the function — a review session that is not on disk is not a
session the loop can select. Everything else here returns strings and line
lists the caller decides what to do with.

Three invariants survive the move out of ``runner.py``:

* ``_DELTA_DIFF_FLAGS`` is carried both by the delta selector's name probes
  and by the diff commands the rendered scope tells the reviewer to run, so
  the patch a reviewer reads covers exactly the changes the file list was
  derived from. It is shared with the delta selector rather than copied;
  ``review_delta`` imports it from here.
* A scope section that declares a delta but is missing either of its two
  required sub-headings is a ``plans.PlanError`` rather than a silent
  rewrite. A body that declares no ``## Delta Review Scope`` at all already
  tells the reviewer to read everything, so ``rescoped_review_session_body``
  returns ``None`` instead of widening what is already widest.
* Reuse never re-authors. ``ensure_generated_review_session`` returns
  ``(session, False)`` for a pending review it found, so a re-review's body
  is stable across the cycles that look for it.

Nothing here imports the runner. What the loop owns enters as a parameter:
the plan directory, the harness name, the delta, the resolved workspace, and
the repository review bases. It reads ``review.*`` at its owning module
rather than through the runner's alias names.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import plans, review, review_session_contract, workspaces

# Scope of a generated review session. ``full`` is the fail-open default: any
# re-review that cannot prove a narrower scope reads everything again.
REVIEW_SCOPE_FULL = "full"
REVIEW_SCOPE_DELTA = "delta"

# Headings that delimit the re-review scope the runner owns and re-derives.
REVIEW_SCOPE_HEADING = "## Delta Review Scope"
REVIEW_REPOSITORY_BASE_HEADING = "## Ordered repository review base"
REVIEW_PRIOR_FINDINGS_HEADING = "### Prior findings to verify"
REVIEW_SCOPE_DETAIL_HEADING = "### Scope"

# Carried both by the delta selector's name probes and by the diff commands the
# rendered scope tells the reviewer to run, so the patch a reviewer reads covers
# exactly the changes the file list was derived from. ``--no-renames`` keeps
# both sides of a rename: with detection on, ``--name-only`` prints only the
# destination, so the source path would drop out of the scope. ``--no-relative``
# keeps a `diff.relative=true` config from narrowing the diff to `cwd` and
# printing `cwd`-relative paths, which erases every repository-root change from
# a run — or a reviewer — working in a nested directory. ``--no-textconv`` and
# ``--no-ext-diff`` pin the rendered patches to git's built-in diff over raw
# bytes: a textconv or external diff driver — configured in `.gitattributes` and
# git config, both of which a repair turn could itself write — whose rendering is
# constant makes git print no patch at all for a path the name probes still
# select, leaving the reviewer a file list whose bytes no command in the section
# would show.
_DELTA_DIFF_FLAGS: tuple[str, ...] = (
    "--no-renames",
    "--no-relative",
    "--no-textconv",
    "--no-ext-diff",
)


def _review_scope_preamble_lines(*, narrowed: bool) -> list[str]:
    if narrowed:
        intro = (
            "- This is a re-review following a repair cycle. Prior findings "
            "and the scope below replace a from-scratch reading of the plan."
        )
    else:
        intro = (
            "- This is a re-review following a repair cycle, but no Git-"
            "verified diff bounds the repair work, so this review stays full "
            "scope: read the plan and the repository as if reviewing from "
            "scratch. The prior findings below are extra required checks, "
            "not a narrower scope."
        )
    return [
        REVIEW_SCOPE_HEADING,
        "",
        intro,
        (
            "- Verify each fix below actually closes its finding; report any "
            "that is unresolved as a finding again."
        ),
        ("- Report newly introduced problems inside this scope as new findings."),
        "",
    ]


def _review_scope_detail_lines(delta: Mapping[str, Any] | None) -> list[str]:
    lines = [REVIEW_SCOPE_DETAIL_HEADING, ""]
    if delta is None or delta.get("mode") != REVIEW_SCOPE_DELTA:
        lines.append(
            "- No Git-verified diff of the repair work is available, so this "
            "scope is not narrowed: review the whole plan and every file it "
            "touched, exactly as a first review would."
        )
        lines.append("")
        return lines

    repositories = delta.get("repositories")
    if isinstance(repositories, Sequence) and not isinstance(
        repositories, (str, bytes)
    ):
        lines.append(
            "- Review the combined repair delta in every repository below; "
            "keep aliases attached to all path evidence so namespaces never "
            "merge. Each command is rooted explicitly at its repository."
        )
        for repository in repositories:
            if not isinstance(repository, Mapping):
                continue
            alias = repository["alias"]
            path = repository["path"]
            base_sha = repository["base_sha"]
            flags = " ".join(_DELTA_DIFF_FLAGS)
            lines.append(f"- `{alias}` (`{path}`), base `{base_sha}`:")
            lines.append(
                f"  - Run `git -C {shlex.quote(str(path))} diff {flags} "
                f"{base_sha}` and `git -C {shlex.quote(str(path))} diff "
                f"--cached {flags} {base_sha}`."
            )
            changed_files = repository.get("changed_files") or ()
            if changed_files:
                lines.append("  - Qualified changed paths:")
                lines.extend(f"    - `{item}`" for item in changed_files)
            else:
                lines.append("  - No files changed since this repository base.")
            if repository.get("baseline_dirty"):
                lines.append(
                    "  - The reviewed base included pre-existing dirty paths; "
                    "they remain included even if a repair restored them."
                )
        lines.append("")
        return lines

    base_sha = delta["base_sha"]
    flags = " ".join(_DELTA_DIFF_FLAGS)
    lines.append(
        f"- Review the repair work since `{base_sha}`: run "
        f"`git diff {flags} {base_sha}` for the base-to-worktree patch and "
        f"`git diff --cached {flags} {base_sha}` for the base-to-index patch, "
        "which is the only one that shows a repair that staged an edit and "
        "then restored the working file. Both carry the flags the file list "
        "below was derived with, so they cover the whole repository from any "
        "directory, name both sides of a rename, and print git's own diff of "
        "the raw bytes rather than whatever a configured textconv or external "
        "diff driver would render."
    )
    changed_files = delta.get("changed_files") or ()
    if changed_files:
        lines.append(
            f"- Files changed since `{base_sha}` (both patches above plus "
            "untracked files, which neither patch shows):"
        )
        lines.extend(f"  - `{path}`" for path in changed_files)
        if delta.get("baseline_dirty"):
            lines.append(
                "- The reviewed tree carried uncommitted changes on top of "
                f"`{base_sha}`, so the list also names every path that was "
                "uncommitted then; a repair may have restored one, in which "
                "case neither patch above still shows it."
            )
    else:
        lines.append(f"- No files changed since `{base_sha}`.")
    lines.append("")
    return lines


def _delta_review_scope_lines(delta: Mapping[str, Any]) -> list[str]:
    """Render the `## Delta Review Scope` section of a re-review session.

    At most the section narrows what the reviewer must *read*, and only when
    Git proved the narrower scope; it never relaxes the output grammar, the
    required artifacts, or verdict enforcement, all of which stay
    scope-agnostic.
    """
    narrowed = delta.get("mode") == REVIEW_SCOPE_DELTA
    lines = _review_scope_preamble_lines(narrowed=narrowed)
    lines.append(REVIEW_PRIOR_FINDINGS_HEADING)
    lines.append("")
    for finding in delta.get("prior_findings") or ():
        lines.append(f"- `{finding['id']}` ({finding['severity']}): {finding['title']}")
        for evidence in finding.get("evidence") or ():
            symbol = evidence.get("symbol")
            if symbol:
                lines.append(f"  - Evidence: `{evidence['path']}` — `{symbol}`")
            else:
                lines.append(f"  - Evidence: `{evidence['path']}`")
        for bullet in finding.get("done_criteria") or ():
            lines.append(f"  - Done when: {bullet}")
    lines.append("")
    lines.extend(_review_scope_detail_lines(delta))
    return lines


def rescoped_review_session_body(
    text: str, delta: Mapping[str, Any] | None
) -> str | None:
    """Rebuild a generated review session's scope from current evidence.

    Returns the new body, or ``None`` when the session declares no delta scope
    at all — such a session already tells the reviewer to read everything, so
    no refresh can widen it. The prior-findings block is carried over verbatim
    because it records what the earlier review found, not what is in scope now.
    """
    lines = text.split("\n")
    if REVIEW_SCOPE_HEADING not in lines:
        return None
    start = lines.index(REVIEW_SCOPE_HEADING)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    section = lines[start:end]
    if (
        REVIEW_PRIOR_FINDINGS_HEADING not in section
        or REVIEW_SCOPE_DETAIL_HEADING not in section
    ):
        raise plans.PlanError(
            f"a `{REVIEW_SCOPE_HEADING}` section without both a "
            f"`{REVIEW_PRIOR_FINDINGS_HEADING}` and a "
            f"`{REVIEW_SCOPE_DETAIL_HEADING}` heading cannot be re-scoped"
        )
    findings_at = start + section.index(REVIEW_PRIOR_FINDINGS_HEADING)
    detail_at = start + section.index(REVIEW_SCOPE_DETAIL_HEADING)
    if findings_at > detail_at:
        raise plans.PlanError(
            f"`{REVIEW_PRIOR_FINDINGS_HEADING}` must precede "
            f"`{REVIEW_SCOPE_DETAIL_HEADING}` in a re-scopable review session"
        )
    narrowed = delta is not None and delta.get("mode") == REVIEW_SCOPE_DELTA
    return "\n".join(
        [
            *lines[:start],
            *_review_scope_preamble_lines(narrowed=narrowed),
            *lines[findings_at:detail_at],
            *_review_scope_detail_lines(delta),
            *lines[end:],
        ]
    )


def build_generated_review_session_body(
    session_id: plans.SessionId,
    *,
    review_harness: str,
    plan_dir: str | Path,
    delta: Mapping[str, Any] | None = None,
    workspace: workspaces.ResolvedWorkspace | None = None,
    review_bases: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Render the markdown body of a generated review session file."""
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    artifact_version = (
        review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION
        if workspace is not None
        else review.REVIEW_ARTIFACT_VERSION
    )
    generated_version = (
        review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
        if workspace is not None
        else review.GENERATED_REVIEW_SESSION_VERSION
    )
    handoff_files = "(none)" if workspace is not None else "(none yet)"
    repository_aliases = workspace.registry.aliases if workspace is not None else ()
    contract_section = review_session_contract._generated_review_contract_section(
        session_id,
        plan_dir=canonical_plan_dir,
        repository_aliases=repository_aliases,
    )
    scope_lines = _delta_review_scope_lines(delta) if delta is not None else []
    header_lines = [f"# {session_id.display_label}: Final Review", ""]
    repository_lines: list[str] = []
    if workspace is not None:
        scope = plans.SessionScope(
            reads=workspace.registry.aliases,
            writes=(),
        )
        header_lines.extend(plans.render_session_scope(scope).rstrip().splitlines())
        header_lines.append("")
        repository_lines = _repository_review_base_lines(workspace, review_bases)
    return "\n".join(
        [
            *header_lines,
            review.REVIEW_SESSION_MARKER,
            review.review_session_version_marker(generated_version),
            "",
            "## Instructions",
            "",
            (
                f"- Executed by the `{review_harness}` harness as a generated "
                "final-review session."
            ),
            (
                "- Perform a critical review of the work completed in the prior "
                "sessions of this plan."
            ),
            (
                f"- Write review findings to `{review.FINAL_REVIEW_FILENAME}` "
                "inside this plan directory."
            ),
            (
                "- Include at least three distinct source, documentation, or "
                "test path-like references as evidence."
            ),
            (
                "- Include at least one review-oriented section heading such "
                "as Findings, Risks, Concerns, Assessment, Review, or Summary."
            ),
            (
                "- End the file with exactly one machine-readable status line: "
                f"`{review.REVIEW_STATUS_ALL_CLEAR}` or "
                "`Status: N Findings (H high, M med, L low)`, where "
                "`H + M + L == N`."
            ),
            (
                "- Also write the structured review artifact "
                f"`{review.FINAL_REVIEW_ARTIFACT_FILENAME}` inside this plan "
                'directory: a JSON object with `"version": '
                f'{artifact_version}`, `"outcome"` '
                '(`"all-clear"` or `"findings"`), and `"findings"` '
                "(a list, empty for all-clear)."
            ),
            *(
                [
                    (
                        "- In repository-aware findings, every finding requires "
                        "a primary `repo` alias, every evidence `path` must use "
                        "qualified `Alias:path` syntax, and every verification "
                        "check requires its own `repo` alias."
                    )
                ]
                if workspace is not None
                else []
            ),
            (
                "- Each finding object requires every one of these keys: "
                "`id` (unique string), `severity` (`high`|`med`|`low`), "
                "`title`, `evidence` (non-empty list of objects with a "
                "`path` and optional `symbol`), `explanation`, "
                "`repair_group` (findings sharing one fix share one group), "
                "`done_criteria` (non-empty list of strings), and "
                "`verification_checks` (non-empty list of completion-contract "
                "check objects: `command`, `file_exists`, or "
                "`regex_in_file`)."
            ),
            (
                "- A verification check accepts only the keys its own type "
                "documents; any other key is rejected. `file_exists` asserts "
                "the path EXISTS and `regex_in_file` asserts the pattern is "
                "PRESENT, and neither has absence semantics: assert that "
                "something is gone with a `command` check that exits non-zero "
                "while it is still there."
            ),
            (
                "- Set `review_replay: true` only on durable current-state "
                "verification gates that should be rerun by every later "
                "generated review. Leave HEAD, dirty-tree, intermediate-file, "
                "and other point-in-time checks unmarked."
            ),
            (
                f"- The `{review.FINAL_REVIEW_ARTIFACT_FILENAME}` outcome and "
                "per-severity finding totals must agree exactly with the "
                f"final `Status:` line of `{review.FINAL_REVIEW_FILENAME}`."
            ),
            "- Do not modify any files in the repository under review.",
            (
                "- Mark only this session's handoff complete; do not change any "
                "other handoff status."
            ),
            "",
            *repository_lines,
            *scope_lines,
            *contract_section.split("\n"),
            "## Handoff",
            "",
            "<!-- Fill this section when the session is complete -->",
            "",
            "- **Status**: pending",
            f"- **Files modified**: {handoff_files}",
            "- **Key decisions**: (none yet)",
            "- **Blockers for next session**: (none)",
            "- **Notes**:",
            "",
        ]
    )


def _repository_review_base_lines(
    workspace: workspaces.ResolvedWorkspace,
    review_bases: Sequence[Mapping[str, Any]],
) -> list[str]:
    lines = [
        REVIEW_REPOSITORY_BASE_HEADING,
        "",
        (
            "Review all repositories as one system while keeping every path "
            "qualified as `Alias:path`. The base below records the HEAD and "
            "pre-existing dirty paths the review started from."
        ),
        "",
    ]
    bases_by_alias = {
        str(item.get("alias")): item
        for item in review_bases
        if isinstance(item, Mapping)
    }
    for repository in workspace.repositories:
        base = bases_by_alias.get(repository.alias, {})
        head = base.get("head") or "(unborn HEAD)"
        dirty_paths = base.get("dirty_paths") or ()
        lines.append(
            f"- `{repository.alias}`: path `{repository.repository.path}`; "
            f"origin `{repository.origin}`; HEAD `{head}`."
        )
        if dirty_paths:
            lines.append("  - Pre-existing dirty paths:")
            lines.extend(f"    - `{path}`" for path in dirty_paths)
        else:
            lines.append("  - Pre-existing dirty paths: (none)")
    lines.append("")
    return lines


def refreshed_review_repository_base(
    text: str,
    workspace: workspaces.ResolvedWorkspace,
    review_bases: Sequence[Mapping[str, Any]],
) -> str:
    """Replace the generated review's repository base before execution."""
    lines = text.split("\n")
    if REVIEW_REPOSITORY_BASE_HEADING not in lines:
        raise plans.PlanError(
            "repository-aware generated review is missing its ordered "
            "repository review base"
        )
    start = lines.index(REVIEW_REPOSITORY_BASE_HEADING)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    return "\n".join(
        [
            *lines[:start],
            *_repository_review_base_lines(workspace, review_bases),
            *lines[end:],
        ]
    )


def find_pending_generated_review_session(
    plan_dir: str | Path,
) -> plans.SessionFile | None:
    """Return the first pending generated review session file, if any."""
    canonical_dir = plans.canonicalize_path(plan_dir)
    for session in plans.discover_session_files(canonical_dir):
        if not review_session_contract.is_generated_review_session_file(session.path):
            continue
        status = plans.read_session_status(session)
        if status.status == "pending":
            return session
    return None


def ensure_generated_review_session(
    plan_dir: str | Path,
    *,
    review_harness: str,
    delta: Mapping[str, Any] | None = None,
    workspace: workspaces.ResolvedWorkspace | None = None,
    review_bases: Sequence[Mapping[str, Any]] = (),
) -> tuple[plans.SessionFile, bool]:
    """Reuse a pending generated review session, or append a new one.

    Returns the canonical ``SessionFile`` and ``True`` when a new session file
    was written. The next top-level session id is one greater than the max
    first component across all existing session ids. ``delta`` narrows the
    scope of a newly written session only; a reused pending session keeps the
    body it was written with.
    """
    canonical_dir = plans.canonicalize_path(plan_dir)
    pending = find_pending_generated_review_session(canonical_dir)
    if pending is not None:
        return pending, False

    session_id = plans.next_top_level_session_id(canonical_dir)
    body = build_generated_review_session_body(
        session_id,
        review_harness=review_harness,
        plan_dir=canonical_dir,
        delta=delta,
        workspace=workspace,
        review_bases=review_bases,
    )
    target_path = canonical_dir / session_id.filename
    target_path.write_text(body, encoding="utf-8")
    return plans.SessionFile(session_id=session_id, path=target_path), True
