---
name: plan-iterate
description: Iterative Q&A refinement of a markdown plan. Assesses completeness, asks targeted questions, applies improvements. Use before session-split.
allowed-tools: Read, Edit, Glob, Grep
---

# Plan Iterate

## Purpose

Refine a markdown implementation plan through iterative Q&A. Assess the plan's completeness across key dimensions, ask targeted questions about the weakest areas, and apply the user's answers as concrete edits to the plan file. Designed to run after a planning pass generates the initial plan and before `session-split` breaks it into sessions.

## Input

The user provides a plan file path as the host agent command or skill invocation argument. Example:

```
plan-iterate /tmp/agent-plans/tranquil-singing-lantern.md
```

## Instructions

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

*Context & Problem*:
- "The context section is [brief gap]. What is the core reason this change is needed?"
- Options: 2-3 candidate problem statements derived from the plan content

*Scope Boundaries*:
- "The plan doesn't clarify what is explicitly out of scope. Which of these should be excluded?"
- Options: 2-3 plausible out-of-scope items inferred from the plan

*File & Code References*:
- "Step N mentions [vague reference] without a file path. Which file does this live in?"
- Options: candidate files found via Grep/Glob, or "I'll specify the path"

*Design Decisions*:
- "The plan [does X] but doesn't explain why over [alternative]. What drove this choice?"
- Options: 2-3 alternative approaches with tradeoff descriptions

*Edge Cases*:
- "What should happen when [specific edge case relevant to the plan]?"
- Options: 2-3 concrete behaviors

*Sequencing*:
- "Sessions N and M seem to have a dependency on [shared resource]. Should Session N create it, or should it already exist?"
- Options: reorder, add prerequisite step, or document assumption

*Verification*:
- "How should the implementation be verified? The plan currently has no verification section."
- Options: test command suggestions, manual test steps, or "I'll add verification later"

*Risk & Rollback*:
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

## Important Notes

- Never delete existing plan content unless the user explicitly asks to remove something
- If the plan is very short (< 15 lines), it may be a quick-fix plan — keep iteration lightweight (1 round max, focus only on Context and Verification)
- The conversation context naturally tracks what has been asked, preventing question repetition across rounds — no explicit history tracking is needed
- Codebase validation is opportunistic, not exhaustive — use Glob/Grep only when the plan references specific files and a quick check would improve question quality. Do not perform a full session-expand-style validation.
- If the user's answer to any question is "skip" or "not relevant", respect it and move to the next dimension without editing
- When the plan references a codebase that is not the current working directory, ask the user for the codebase root before attempting Glob/Grep lookups
- Only modify the provided plan file — never create new files
