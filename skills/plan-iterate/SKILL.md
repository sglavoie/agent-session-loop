---
name: plan-iterate
description: Iterative refinement of a markdown plan. Default mode assesses completeness, asks targeted questions, applies improvements. --ideal adds ideal-vision divergence analysis sharing one research pass and edits accepted improvements into the plan; --ideal-only prints the ideal vision read-only. Use before session-split or asl prepare.
allowed-tools: Read, Edit, Glob, Grep, Bash
---

# Plan Iterate

## Purpose

Refine a markdown implementation plan. The skill has three modes behind one entry point:

- **Default mode** (no flags): iterative Q&A refinement. Assess the plan's completeness across key dimensions, ask targeted questions about the weakest areas, and apply the user's answers as concrete edits to the plan file. Ordinary `plan-iterate` behavior when `--ideal` is absent is unchanged by the ideal merge.
- **Ideal mode** (`--ideal`): merge idealization into iteration. Perform one research pass over the codebase that is shared by the ideal divergence analysis and the completeness assessment, rank divergences from the ideal by impact, ask the user to adopt, defer, or reject high-impact items, edit accepted improvements into the plan file, and record consciously deferred items in the plan. The final plan must be suitable for `asl prepare`.
- **Ideal-only mode** (`--ideal-only`): read-only idealization. Produce the ideal vision and divergence report in the conversation without modifying the plan file. This is the legacy `plan-ideal` behavior; the `plan-ideal` skill remains available as a read-only alias for `plan-iterate --ideal-only`.

Designed to run after a planning pass generates the initial plan and before `session-split` breaks it into sessions or `asl prepare` authors, validates, and gates the plan directory end to end.

## Input

The user provides a plan file path plus optional mode flags as the host agent command or skill invocation argument:

```
plan-iterate PLAN.md
plan-iterate PLAN.md --ideal --cwd REPO_ROOT
plan-iterate PLAN.md --ideal-only [--cwd REPO_ROOT]
```

Argument rules:

- The plan file path is the only required argument.
- `--ideal` selects ideal mode (mutating).
- `--ideal-only` selects ideal-only mode (read-only). `--ideal` and `--ideal-only` are mutually exclusive; reject the invocation and stop if both appear.
- `--cwd REPO_ROOT` sets the codebase root used for research. In ideal and ideal-only modes, when `--cwd` is absent, use the current working directory if the plan clearly targets it; otherwise ask the user for the codebase root before researching. In default mode `--cwd` is accepted and used the same way for opportunistic Glob/Grep checks.
- Reject any other unknown flags and stop without making changes.

## Instructions

### Step 0: Select Mode

Parse the invocation arguments per the rules above, resolve the plan path and codebase root, then route:

- No mode flag: run **Default Mode** (Steps 1-5).
- `--ideal`: run **Ideal Mode** (Steps I1-I8).
- `--ideal-only`: run **Ideal-Only Mode** (Steps I1-I4 followed by the read-only report).

## Default Mode

### Step 1: Read and Parse the Plan

Read the entire plan file at the provided path.

Parse the plan to identify:

- **Title**: the `# H1` heading
- **Sections**: all `## H2` and `### H3` headings with their content
- **Line count**: approximate size of the plan
- **Session markers**: any `## Session N` or `### Session N` headings
- **File references**: backtick paths like `src/foo/bar.go`, `internal/replay/replayer.go`
- **Verification sections**: anything titled Verification, Testing, Test plan, or similar

Track fenced code blocks (``` or ~~~) to avoid treating code content as plan structure.

### Step 2: Assess Completeness

Evaluate the plan against eight dimensions. For each, assign a status: **adequate**, **thin**, or **missing**.

**Dimension 1 — Context & Problem**
Does the plan explain what is being done and why? Look for a `## Context` section or equivalent. The problem statement should be specific enough for someone unfamiliar to understand.

**Dimension 2 — Scope Boundaries**
Are in-scope and out-of-scope items clear? For small plans (< 40 lines), implicit scope from the context is acceptable. For larger plans, explicit scope statements matter.

**Dimension 3 — File & Code References**
Does the plan identify specific files, functions, structs, or line numbers that need to change? Vague references like "update the handler" without a file path score poorly. Code blocks with concrete diffs or signatures score well.

**Dimension 4 — Design Decisions**
Are key technical choices stated with rationale? Data model choices, API design, algorithm selection, pattern choices. Trivial plans (bug fixes, small changes) may not need this.

**Dimension 5 — Edge Cases & Error Handling**
Are failure modes, boundary conditions, empty states, and error paths addressed? Look for words like "edge case", "error", "fail", "empty", "nil/null", "boundary", "concurrent", "race".

**Dimension 6 — Sequencing & Dependencies**
For multi-session plans: Is the session order justified? Are inter-session dependencies stated?
For single-session plans: Are implementation steps in a logical order?

**Dimension 7 — Verification**
Is there a concrete verification strategy? Test commands, expected outputs, manual test steps, or acceptance criteria. A bare `## Verification` with a single shell command is adequate. No verification section at all is missing.

**Dimension 8 — Risk & Rollback**
For plans over ~80 lines: Are risks identified? Is there a rollback strategy or "what could go wrong" discussion?
For small plans (< 80 lines): This dimension is automatically adequate (small scope = low risk).

**Scaling rules** — not all dimensions apply to every plan:

- Plans under 30 lines: Only check dimensions 1, 3, and 7. Auto-mark 2, 4, 5, 6, 8 as adequate.
- Plans 30-80 lines: Check dimensions 1-5 and 7. Auto-mark 6 and 8 as adequate unless the plan has session markers.
- Plans over 80 lines: Check all 8 dimensions.

### Step 3: Report Assessment and Check for Early Exit

Print a concise assessment table:

```
Plan: <title> (<N> lines)

| Dimension                | Status   |
|--------------------------|----------|
| Context & Problem        | adequate |
| Scope Boundaries         | thin     |
| File & Code References   | adequate |
| Design Decisions         | missing  |
| Edge Cases               | thin     |
| Sequencing               | adequate |
| Verification             | adequate |
| Risk & Rollback          | adequate |

Weak areas: Scope Boundaries, Design Decisions, Edge Cases
```

**If all dimensions are adequate**: Tell the user the plan looks solid. Use AskUserQuestion with:

- question: "The plan scores well on all completeness dimensions. Would you like to refine it anyway, or proceed to the next step?"
- header: "Plan status"
- options:
  - label: "Looks good" / description: "No changes needed, exit"
  - label: "Refine anyway" / description: "Start a Q&A round targeting depth over gaps"

If user selects "Looks good", print a short confirmation and stop. If "Refine anyway", proceed to Step 4 and focus questions on deepening the strongest areas rather than filling gaps.

**If any dimensions are thin or missing**: Proceed directly to Step 4.

### Step 4: Iterative Q&A Rounds

Run up to **4 rounds** of question-and-edit cycles. Each round proceeds as follows:

#### 4a. Generate Questions

Select the 2-3 weakest dimensions (prefer "missing" over "thin"). For each selected dimension, formulate one targeted question.

**Question design rules**:

- Reference specific parts of the plan by quoting short excerpts
- Each question must have 2-3 options; the first option is the recommendation
- Options must be concrete and plan-specific, not generic
- Include a brief reason why this dimension matters for the plan
- If the plan references files in the codebase, optionally use Glob/Grep to check whether those files exist and inform the question (e.g., "I checked and `internal/replay/replayer.go` does not exist yet — should the plan note this is a new file?")

**Question templates by dimension** (adapt to the specific plan):

_Context & Problem_:

- "The context section is [brief gap]. What is the core reason this change is needed?"
- Options: 2-3 candidate problem statements derived from the plan content

_Scope Boundaries_:

- "The plan doesn't clarify what is explicitly out of scope. Which of these should be excluded?"
- Options: 2-3 plausible out-of-scope items inferred from the plan

_File & Code References_:

- "Step N mentions [vague reference] without a file path. Which file does this live in?"
- Options: candidate files found via Grep/Glob, or "I'll specify the path"

_Design Decisions_:

- "The plan [does X] but doesn't explain why over [alternative]. What drove this choice?"
- Options: 2-3 alternative approaches with tradeoff descriptions

_Edge Cases_:

- "What should happen when [specific edge case relevant to the plan]?"
- Options: 2-3 concrete behaviors

_Sequencing_:

- "Sessions N and M seem to have a dependency on [shared resource]. Should Session N create it, or should it already exist?"
- Options: reorder, add prerequisite step, or document assumption

_Verification_:

- "How should the implementation be verified? The plan currently has no verification section."
- Options: test command suggestions, manual test steps, or "I'll add verification later"

_Risk & Rollback_:

- "What is the main risk if this implementation goes wrong? How would you roll back?"
- Options: specific risk scenarios with mitigation suggestions

#### 4b. Present Questions

Use AskUserQuestion with 2-3 questions. Each question:

- `header`: the dimension name (abbreviated to max 12 chars: "Context", "Scope", "Files", "Decisions", "Edge Cases", "Sequencing", "Verification", "Risk")
- `question`: the specific question text
- `options`: 2-3 options, each with a label and description
- `multiSelect`: false

#### 4c. Apply Edits

Based on the user's answers, use the Edit tool to modify the plan markdown:

- **Missing section**: Add a new `## Section` heading with content derived from the answer. Place it in a logical position relative to existing sections (Context first, Verification near the end).
- **Thin section**: Append to or expand the existing section with the new information.
- **Factual correction**: Fix inline (wrong file path, incorrect function name, etc.)

**Edit rules**:

- Preserve all existing content the user did not ask to change
- Match the plan's existing style (heading levels, bullet format, code block conventions)
- Keep edits minimal — add what the answer provides, do not generate extra content
- If the user selects "Other" and provides free text, incorporate that verbatim
- After each edit, briefly state what was changed (e.g., "Added ## Verification section with test commands")

#### 4d. Re-assess and Continue or Stop

After applying edits, mentally re-assess the affected dimensions. If:

- All dimensions are now adequate: proceed to Step 5 (final report)
- Some dimensions are still thin/missing AND rounds remain: loop back to 4a
- Max rounds (4) reached: proceed to Step 5

### Step 5: Final Report

Print a summary of what was done:

```
Plan iteration complete.

Rounds: <N> of 4
Questions asked: <M>
Edits applied: <K>

Dimensions improved:
- <dimension>: missing -> adequate
- <dimension>: thin -> adequate

Remaining gaps (if any):
- <dimension>: still thin -- consider addressing during execution

Next step: `session-split <plan-path>`
```

If the plan has session markers, remind the user they can proceed to `session-split`. If the plan does not have session markers, note that `session-split` can propose session boundaries.

## Ideal Mode (`--ideal`)

Ideal mode replaces the old two-skill workflow (`plan-ideal` followed by `plan-iterate`) with a single pass so the daily ideal-plus-iterate workflow requires one research pass instead of two, and so accepted ideal improvements land in the plan file rather than only in conversation history.

### Step I1: Read and Deeply Understand the Plan

Read the entire plan file. Parse the same structure as Default Mode Step 1 (title, sections, line count, session markers, file references, verification sections, fenced code blocks). Additionally identify:

- **Goal**: What is the plan trying to achieve?
- **Scope**: What boundaries has the plan drawn (explicitly or implicitly)?
- **Approach**: What technical strategy does it use?
- **Constraints**: What has been scoped out, deferred, or simplified?
- **Assumptions**: What does the plan take for granted about the codebase, architecture, or tooling?

Take careful note of anything that sounds like a compromise: "for now", "simple version", "future work", "out of scope", "v2", "stretch goal", "good enough", "MVP", "later", "ideally", "if time permits". These are seeds for the ideal vision.

### Step I2: One Shared Research Pass

Use Glob, Grep, Read, and read-only Bash to research the codebase rooted at `REPO_ROOT` **exactly once**. The findings from this single pass feed both the ideal divergence analysis (Steps I3-I4) and the completeness assessment (Step I5). Do not run a second research pass later in this mode — reuse what you gathered here.

Focus on:

1. **Architecture**: How is the relevant area currently structured? What are the layers, abstractions, and module boundaries?
2. **Patterns**: What conventions does the codebase follow? (error handling, testing, configuration, module structure)
3. **Pain points**: TODO comments, HACK markers, FIXME notes, or suppressed warnings in the relevant area — latent problems the ideal solution could address.
4. **Adjacent systems**: What nearby code would benefit from or be affected by an ideal version of this plan?
5. **Test coverage**: What testing patterns exist? Where are the gaps?
6. **Plan factual accuracy**: While reading, verify the plan's file paths, function names, and assumptions so the completeness assessment in Step I5 is grounded in the same findings.

**Research rules**:

- Spend real effort here — this is where the merged workflow earns its value. Read key files in the affected area, not just the ones the plan mentions.
- Use `Grep` to find related patterns across the codebase (e.g., if the plan adds a new command, find all existing commands to understand the full pattern).
- Use `Bash` for read-only commands only: `wc`, `find ... -type f | head`, `git log --oneline -20 -- <path>`, `ls`. Never modify files.
- Aim for 5-15 files read, depending on plan complexity. For large plans, prioritize breadth over depth.

### Step I3: Envision the Ideal

For each dimension below, describe what the **unconstrained ideal** would look like — as if time, labor, and session budgets were unlimited. Do not self-censor for feasibility; use extended thinking to reason deeply before writing each dimension.

1. **Architecture & Design** — better abstractions, modules, layers, data models, or ecosystem patterns; designs that make future extensions trivial.
2. **Completeness & Coverage** — edge cases, error paths with graceful recovery, supported inputs/configurations/environments, adjacent features that logically belong together.
3. **User / Developer Experience** — better defaults, clearer errors, richer feedback, more intuitive APIs, simpler or more powerful configuration, documentation strategy.
4. **Performance & Efficiency** — algorithmic improvements, caching/batching/lazy evaluation, I/O patterns, resource footprint.
5. **Testing & Confidence** — unit/integration/end-to-end/property-based tests, fuzzing, benchmarks, regression and snapshot tests, required test infrastructure.
6. **Observability & Operations** — logging, metrics, tracing, health checks, failure detection and diagnosis, debuggability.
7. **Extensibility & Future-Proofing** — plugin points, hooks, configuration-driven behavior, migration paths, anticipated future requirements.
8. **Ecosystem & Integration** — unifying with or replacing adjacent functionality, standardizing cross-cutting concerns, enabling new capabilities elsewhere.

**Scaling rules** — not every dimension applies equally:

- Plans under 30 lines: Focus on dimensions 1, 2, 3, and 5. Treat 4, 6, 7, 8 lightly or skip with a note.
- Plans 30-100 lines: Cover all dimensions but weight 1, 2, 3, 5 most heavily.
- Plans over 100 lines: All dimensions get full treatment.

### Step I4: Identify Divergences and Rank by Impact

Explicitly compare the ideal vision to the current plan. For each significant divergence, describe:

1. **What the plan does**: the current approach or scope
2. **What the ideal does instead**: the unconstrained version
3. **The gap**: what would need to change to move from current to ideal
4. **The value**: what the user gains from the ideal version

Rank divergences by impact:

- **High impact**: Fundamentally changes the outcome or quality
- **Medium impact**: Meaningfully improves maintainability, coverage, or experience
- **Low impact**: Polish, completeness, and long-term investment

### Step I5: Assess Completeness Using the Shared Research

Run the Default Mode Step 2 completeness assessment (same eight dimensions, same scaling rules), grounded in the Step I2 findings — no new codebase research. Print the Default Mode Step 3 assessment table alongside the divergence summary so the user sees both gaps and ambition in one view.

### Step I6: Ask the User to Adopt, Defer, or Reject

Present the divergences for decision, highest impact first:

- For each **high-impact** divergence, use AskUserQuestion (batch 2-3 divergences per call) with:
  - `header`: a short divergence label (max 12 chars)
  - `question`: the divergence summary — what the plan does, what the ideal does instead, the gap, and the value
  - `options`:
    - label: "Adopt" / description: "Edit this improvement into the plan now"
    - label: "Defer" / description: "Record as consciously deferred in the plan"
    - label: "Reject" / description: "Drop it — not worth pursuing"
- For **medium-impact** divergences, ask one grouped multi-select question offering adoption; anything not selected is recorded as deferred unless the user says to drop it.
- **Low-impact** divergences are listed in the final report only; do not ask about them individually.

If completeness dimensions remain thin or missing after the divergence decisions, fold at most one Default Mode Step 4 Q&A round into the same conversation (reusing the Step I2 research — no new research pass) to close the weakest gaps.

### Step I7: Apply the Decisions to the Plan File

Use the Edit tool to make the decisions durable in the plan file:

- **Adopted divergences**: edit the improvement into the relevant plan sections (scope, design, sessions, verification) following the Default Mode Step 4c edit rules. Accepted ideal improvements must be present in the plan file rather than only conversation history.
- **Deferred divergences**: record each under a `## Consciously Deferred` section (create it near the end of the plan if absent) as one bullet per item with a one-line rationale, so future readers know the ceiling was considered and consciously postponed.
- **Rejected divergences**: do not record them in the plan.
- **Q&A answers**: apply as in Default Mode Step 4c.

### Step I8: Ensure the Plan Is Ready for `asl prepare`

The final plan must pass the `asl prepare` structure gate, which otherwise exits with reason `needs-plan-iteration` and per-session blockers before any output directory is created. Verify — and add or repair where missing — that:

- The plan has **session boundaries**: at least two explicit `## Session <id>: Title` (or `### Session <id>`) headings outside code fences.
- Every session block carries an inline `## Completion Contract` section containing:
  - a `### Done when` bullet list stating that session's done criteria, and
  - a `### Verify` fenced ```json block parseable by `contracts.parse_completion_contract` giving the session's verification recipe.
- Contract checks carry `baseline_expectation` (`pass` or `fail`) where a meaningful pre-implementation baseline expectation exists; checks without it carry no baseline obligations.

Derive done criteria and verification recipes from the plan content and the Step I2 research; ask the user (one AskUserQuestion round at most) only when a verification command is genuinely unknowable from the plan and codebase.

### Step I9: Final Report

Print a summary:

```
Plan ideal-iteration complete.

Research pass: 1 (<N> files examined)
Divergences: <H> high / <M> medium / <L> low
Decisions: <A> adopted, <D> deferred, <R> rejected
Edits applied: <K>
Completeness: <improved dimensions or "all adequate">
Prepare gate: session boundaries, completion contracts, and verify recipes present

Next step: `asl prepare <plan-path> --cwd <repo-root>`
```

## Ideal-Only Mode (`--ideal-only`)

Run Steps I1 through I4 (read the plan, one research pass, envision the ideal, rank divergences), then print the full ideal report to the conversation using the structure below. **Never modify the plan file** in this mode — it is purely descriptive and read-only. The legacy `plan-ideal PLAN.md` invocation remains available as an alias for `plan-iterate --ideal-only`.

```
# Ideal Vision: <plan title>

**Plan file**: `<path>`
**Codebase research**: <N> files examined
**Assessment**: <one-sentence summary of how ambitious the current plan is
relative to the ideal>

## The Ideal Version

<2-4 paragraph narrative describing the holistic vision — what the perfect
implementation would look like, how it would feel to use, how it would fit
into the codebase. Write this as a compelling description, not a checklist.>

## Dimension Analysis

### Architecture & Design
### Completeness & Coverage
### User / Developer Experience
### Performance & Efficiency
### Testing & Confidence
### Observability & Operations
### Extensibility & Future-Proofing
### Ecosystem & Integration

## Divergences from Current Plan

### High Impact
- **<divergence title>**: <current> vs <ideal>. Gap: <what changes>.
  Value: <why it matters>.

### Medium Impact
- ...

### Low Impact
- ...

## What This Means for Planning

<2-3 paragraphs of synthesis: which high-impact divergences are worth
pursuing within practical constraints; the minimum viable ideal; what a
phased approach toward the ideal would look like.>

Next step: `plan-iterate <plan-path> --ideal --cwd <repo-root>`
```

## Important Notes

- Never delete existing plan content unless the user explicitly asks to remove something
- If the plan is very short (< 15 lines), it may be a quick-fix plan — keep iteration lightweight (1 round max, focus only on Context and Verification); in ideal mode, scale the vision down per the Step I3 scaling rules
- The conversation context naturally tracks what has been asked, preventing question repetition across rounds — no explicit history tracking is needed
- In default mode, codebase validation is opportunistic, not exhaustive — use Glob/Grep only when the plan references specific files and a quick check would improve question quality. Do not perform a full session-expand-style validation.
- In ideal and ideal-only modes, the Step I2 research pass is the **only** research pass — both the divergence analysis and the completeness assessment consume it. Do not research the codebase twice.
- Ideal-only mode and the `plan-ideal` alias never modify the plan file; all output goes to the conversation
- **Do not constrain the ideal vision** — describe the ceiling, not the floor. If the ideal requires rewriting 50 files across 10 sessions, say so. The user decides what is practical via adopt/defer/reject.
- **Acknowledge what the plan already does well** — the ideal vision is not a criticism. If a dimension is already well-addressed, say so and describe only the incremental ideal beyond it.
- **Bash is read-only in every mode** — only commands that do not modify the filesystem: `ls`, `find`, `wc`, `git log`, `git diff`, `head`, `tail`. Never use Bash for `mkdir`, `touch`, `rm`, `cp`, `mv`, or any write operation. Plan mutations happen exclusively through the Edit tool on the plan file.
- If the user's answer to any question is "skip" or "not relevant", respect it and move on without editing
- When the plan references a codebase that is not the current working directory and no `--cwd` was given, ask the user for the codebase root before attempting research
- Only modify the provided plan file — never create new files
