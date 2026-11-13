---
name: plan-iterate
description: Iterative refinement of a markdown plan. Default mode assesses completeness, asks targeted questions, applies improvements. --ideal adds ideal-vision divergence analysis sharing one research pass, edits accepted improvements into the plan, and leaves it ready for asl prepare; --ideal-only prints the ideal vision read-only. Use before asl prepare (or the by-hand session-split route).
allowed-tools: AskUserQuestion, Read, Edit, Glob, Grep, Bash
disable-model-invocation: true
---

# Plan Iterate

## Purpose

Refine a markdown implementation plan before it is prepared for execution. The skill has three modes behind one entry point:

- **Default mode** (no flags): iterative Q&A refinement. Assess the plan's completeness across key dimensions, ask targeted questions about the weakest areas, and apply the user's answers as concrete edits to the plan file.
- **Ideal mode** (`--ideal`): one research pass over the codebase that is shared by the ideal divergence analysis and the completeness assessment. Rank divergences from the ideal by impact, ask the user to adopt, defer, or reject high-impact items, edit accepted improvements into the plan file, and record consciously deferred items in the plan. The final plan must be suitable for `asl prepare`.
- **Ideal-only mode** (`--ideal-only`): read-only idealization. Produce the ideal vision and divergence report in the conversation without modifying the plan file.

Where it sits: planning pass → `plan-iterate` → `asl prepare` (split, validate, baseline, final preflight in one command) → `plan-preflight-iterate` while preflight is not green → `asl run`. `session-split` then `session-expand` is the by-hand alternative to `asl prepare`.

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
- `--cwd REPO_ROOT` sets the codebase root used for research. When it is absent, use the current working directory if the plan clearly targets it; otherwise ask the user for the codebase root before researching. In default mode it only scopes opportunistic Glob/Grep checks.
- Reject any other unknown flags and stop without making changes.

## Instructions

### Step 0: Select Mode

Parse the invocation arguments per the rules above, resolve the plan path and codebase root, then route:

- No mode flag: run **Default Mode** (Steps 1-5).
- `--ideal`: run **Ideal Mode** (Steps I1-I9).
- `--ideal-only`: run **Ideal-Only Mode** (Steps I1-I4 followed by the read-only report).

## Default Mode

### Step 1: Read and Parse the Plan

Read the entire plan file and identify:

- **Title**: the `# H1` heading
- **Sections**: all `## H2` and `### H3` headings with their content
- **Line count**: approximate size of the plan
- **Session markers**: `Session <id>` headings as accepted by `agent_session_loop.authoring_parse.parse_plan_document`, which is authoritative for the grammar (heading levels, separators, zero-padded dotted IDs). Do not restate or second-guess it: run `asl split --check --plan-file <plan> --json` when a heading's validity matters.
- **File references**: backtick paths like `src/foo/bar.go`
- **Verification sections**: anything titled Verification, Testing, Test plan, or similar
- **Completion contracts**: inline `## Completion Contract` sections per session (Step I8 describes the required shape)

Ignore content inside fenced code blocks (``` or ~~~) when identifying plan structure.

A heading that looks like a session marker but fails the parser is a malformed candidate: surface malformed-candidate parser errors directly rather than treating them as ordinary content or as an absence of markers. Accepted headings count immediately, without a normalization question or source rewrite. Reserve `session-split`'s boundary-proposal workflow for a plan that canonical parsing confirms has no explicit session headings.

### Step 2: Assess Completeness

Evaluate the plan against eight dimensions. For each, assign a status: **adequate**, **thin**, or **missing**.

1. **Context & Problem** — Does the plan explain what is being done and why, specifically enough for someone unfamiliar to understand?
2. **Scope Boundaries** — Are in-scope and out-of-scope items clear? Implicit scope is acceptable for small plans (< 40 lines).
3. **File & Code References** — Does the plan name specific files, functions, structs, or line numbers? "Update the handler" without a path scores poorly; concrete diffs or signatures score well.
4. **Design Decisions** — Are key technical choices (data model, API, algorithm, pattern) stated with rationale? Trivial plans may not need this.
5. **Edge Cases & Error Handling** — Are failure modes, boundary conditions, empty states, concurrency, and error paths addressed?
6. **Sequencing & Dependencies** — Multi-session plans: is the session order justified and are inter-session dependencies stated? Single-session plans: are steps in a logical order?
   **Convergence rules** (multi-session plans): every session must state its own verification command and its own done-condition — never a shared criterion deferred to a later session. Size sessions from evidence: the smallest useful deliverable, time to meaningful feedback, integration uncertainty, and likely repeated work. File count and context size are supporting evidence, not a ceiling. Mark this dimension thin when a session lacks a verification command or done-condition, when boundaries defer the first real integration proof too late, or when a proposed scope reduction has not been explicitly decided by the user.
7. **Verification** — Is there a concrete strategy: test commands, expected outputs, manual steps, or acceptance criteria? A bare `## Verification` with one shell command is adequate; none at all is missing.
8. **Risk & Rollback** — Are risks identified, and is there a rollback strategy?

**Scaling rules** — not all dimensions apply to every plan:

- Plans under 30 lines: check dimensions 1, 3, and 7. Auto-mark the rest adequate.
- Plans 30-80 lines: check dimensions 1-5 and 7. Auto-mark 6 and 8 adequate unless the plan has session markers.
- Plans over 80 lines: check all 8 dimensions.

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

**If all dimensions are adequate**, use AskUserQuestion:

- question: "The plan scores well on all completeness dimensions. Would you like to refine it anyway, or proceed to the next step?"
- header: "Plan status"
- options:
  - label: "Looks good" / description: "No changes needed, exit"
  - label: "Refine anyway" / description: "Start a Q&A round targeting depth over gaps"

"Looks good" runs Step 3.5, then Step 5, skipping Step 4 — the early exit skips the Q&A rounds, never the premortem. "Refine anyway" proceeds through Step 3.5 to Step 4, deepening the strongest areas rather than filling gaps.

**If any dimensions are thin or missing**: proceed to Step 3.5, then Step 4.

### Step 3.5: Premortem

Run exactly one AskUserQuestion round here, at **every plan size** and even when every dimension is adequate. This overrides the Risk & Rollback auto-adequate scaling rules: the dimensions ask what is present, never how the plan fails, and small plans are where one wrong assumption travels furthest unnoticed.

**Seed with base rates before writing the options.** Read the historical failure classes:

```bash
asl metrics --json
```

- `assurance.issues.by_category` gives the failure categories that actually occur.
- `assurance.issues.recurrence_after_resolution_by_category` maps each category to the in-window sightings that came back *after* being resolved — the strongest priors, since the last fix did not really close them. Promote recurring categories the plan plausibly touches, highest count first. Prefer a prior over an invented failure mode.
- **Degrade silently.** No `asl` on PATH, no ledger, or an empty window means the premortem runs on the plan alone. Never block planning on assurance history and never report its absence as an error.

**The question is fixed in form.** Do not soften it into "what are the risks?":

> This plan failed. Every session ran, verification passed, and the outcome is still wrong. Why?

Offer 2-3 concrete failure modes drawn from the plan text, the priors, and opportunistic Glob/Grep, plus the standing option for the user to supply a different one. Each option must name a mechanism specific to this plan ("the backfill runs before the index exists, so every check passes and the query path is still slow"), never a category label ("performance issues"). Use the Step 4b field contract with `header` `Premortem`; this is the one round where `multiSelect` is `true`.

**Route the answers** with the Step 4c edit rules:

- A failure mode that **can** be expressed as a check becomes one. Where the plan already carries `## Completion Contract` sections, add a check to the responsible session's `### Verify` recipe per Step I8. Otherwise add the concrete command to the plan's verification section so it can be lifted into a contract later.
- Every failure mode that **cannot** becomes one bullet under a `## Risks` section, placed per the Step I7 durable-section rule.

### Step 4: Iterative Q&A Rounds

Run up to **4 rounds** of question-and-edit cycles.

#### 4a. Generate Questions

Select the 2-3 weakest dimensions (prefer "missing" over "thin") and formulate one targeted question for each:

- Quote short excerpts of the plan the question is about, and say briefly why the dimension matters for this plan.
- Offer 2-3 concrete, plan-specific options; the first is the recommendation.
- Where the plan references files, a quick Glob/Grep can sharpen the question ("`internal/replay/replayer.go` does not exist yet — should the plan note it is a new file?").

Question angles by dimension (adapt to the plan):

- _Context_: the core reason the change is needed; options are candidate problem statements.
- _Scope_: which plausible adjacent items are explicitly out of scope.
- _Files_: which file a vague reference lives in; options are Grep/Glob candidates or "I'll specify the path".
- _Decisions_: why this approach over a named alternative; options carry tradeoffs.
- _Edge Cases_: what should happen in a specific edge case; options are concrete behaviors.
- _Sequencing_: an unstated dependency between sessions (reorder, add a prerequisite, or document the assumption); a boundary that delays the first integration proof (earlier coherent increment, or keep it with the risk recorded); a scope reduction that changes the deliverable (an explicit decision, never a silent cut to meet a session count); a session with no verification command or done-condition (candidates evidenced by the repo's own test/lint recipes). Never invent sessions merely to satisfy `asl prepare`'s multi-session format.
- _Verification_: test commands, manual steps, or "I'll add verification later".
- _Risk_: the main failure scenario and how to roll back.

#### 4b. Present Questions

Use AskUserQuestion with 2-3 questions. Each question:

- `header`: the dimension name, max 12 chars ("Context", "Scope", "Files", "Decisions", "Edge Cases", "Sequencing", "Verification", "Risk")
- `question`: the specific question text
- `options`: 2-3 options, each with a label and description
- `multiSelect`: false

#### 4c. Apply Edits

Apply the answers to the plan with the Edit tool:

- **Missing section**: add a `## Section` in a logical position (Context first, Verification near the end).
- **Thin section**: expand the existing section.
- **Factual correction**: fix inline (wrong path, wrong function name).

Edit rules: preserve content the user did not ask to change; match the plan's style; add only what the answer provides; incorporate "Other" free text verbatim; after each edit, state in one line what changed.

#### 4d. Re-assess and Continue or Stop

Re-assess the affected dimensions. Go to Step 5 when all are adequate or 4 rounds are done; otherwise loop back to 4a.

### Step 5: Final Report

```
Plan iteration complete.

Rounds: <N> of 4
Questions asked: <M>
Edits applied: <K>
Premortem: <P> failure mode(s) surfaced -> <C> verification check(s), <R> risk bullet(s)
Base-rate priors: <"N recurring categories from asl metrics" or "unavailable">

Dimensions improved:
- <dimension>: missing -> adequate

Remaining gaps (if any):
- <dimension>: still thin -- consider addressing during execution

Next step: <see below>
```

Choose the next step from the plan's shape, because `asl prepare` refuses (`needs-plan-iteration`) a plan without at least two session headings and a complete `## Completion Contract` per session:

- **Session headings and complete contracts present**: `asl prepare <plan-path> --cwd <repo-root>`.
- **Otherwise**: `plan-iterate <plan-path> --ideal --cwd <repo-root>`, which authors the missing boundaries and contracts in Step I8, or the by-hand route `session-split <plan-path>`, which authors contracts during the split and can propose session boundaries.

## Ideal Mode (`--ideal`)

### Step I1: Read and Deeply Understand the Plan

Parse the plan as in Default Mode Step 1. Additionally identify:

- **Goal**: what the plan is trying to achieve
- **Scope**: the boundaries it draws, explicitly or implicitly
- **Approach**: its technical strategy
- **Constraints**: what has been scoped out, deferred, or simplified
- **Assumptions**: what it takes for granted about the codebase, architecture, or tooling

Note anything that sounds like a compromise — "for now", "simple version", "future work", "out of scope", "v2", "MVP", "later", "if time permits". These are seeds for the ideal vision.

### Step I2: One Shared Research Pass

Research the codebase rooted at `REPO_ROOT` **exactly once** with Glob, Grep, Read, and read-only Bash. The findings feed both the ideal divergence analysis (Steps I3-I4) and the completeness assessment (Step I5). Do not run a second research pass later in this mode — reuse what you gathered here.

Focus on:

1. **Architecture**: layers, abstractions, and module boundaries in the relevant area.
2. **Patterns**: conventions for error handling, testing, configuration, module structure.
3. **Pain points**: TODO/HACK/FIXME markers and suppressed warnings the ideal could address.
4. **Adjacent systems**: nearby code that would benefit from or be affected by the ideal version.
5. **Test coverage**: existing testing patterns and gaps.
6. **Plan factual accuracy**: whether the plan's paths, names, and assumptions hold, so Step I5 is grounded in the same findings.

Spend real effort here: read key files in the affected area, not just those the plan names, and Grep for related patterns across the codebase (a new command → all existing commands). Aim for 5-15 files depending on plan size; for large plans, prefer breadth over depth.

### Step I3: Envision the Ideal

For each dimension, describe the **unconstrained ideal** — as if time, labor, and session budgets were unlimited. Do not self-censor for feasibility; think each dimension through before writing it.

1. **Architecture & Design** — better abstractions, modules, data models; designs that make future extensions trivial.
2. **Completeness & Coverage** — edge cases, graceful error recovery, supported inputs and environments, adjacent features that belong together.
3. **User / Developer Experience** — defaults, error messages, feedback, API ergonomics, configuration, documentation.
4. **Performance & Efficiency** — algorithms, caching/batching/laziness, I/O patterns, resource footprint.
5. **Testing & Confidence** — unit, integration, end-to-end, property-based, regression tests and the infrastructure they need.
6. **Observability & Operations** — logging, metrics, tracing, failure detection and diagnosis.
7. **Extensibility & Future-Proofing** — extension points, configuration-driven behavior, migration paths.
8. **Ecosystem & Integration** — unifying or replacing adjacent functionality, standardizing cross-cutting concerns.
9. **Subtraction & Consolidation** — what the ideal *removes* rather than adds: code, options, flags, layers, or steps it never builds; duplicate mechanisms collapsed into one; invariants and guarantees it retires. Ask what disappears by deleting something, and what the plan looks like if the new capability replaced an existing one instead of sitting beside it.

**Scaling rules**:

- Plans under 30 lines: focus on dimensions 1, 2, 3, and 5; treat 4, 6, 7, 8 lightly or skip with a note.
- Plans 30-100 lines: cover all dimensions, weighting 1, 2, 3, 5 most heavily.
- Plans over 100 lines: full treatment for every dimension.
- Dimension 9 gets full treatment at **every** plan size: removal is cheapest when little is built, and it is the only dimension that reduces future obligation.

### Step I4: Identify Divergences and Rank by Impact

Compare the ideal to the current plan. For each significant divergence, state:

1. **What the plan does**
2. **What the ideal does instead**
3. **The gap**: what would need to change
4. **The value**: what the user gains
5. **Ongoing cost**: the permanent obligation adopting it creates — what must be maintained, held invariant, documented, migrated, or kept backward-compatible forever after. Answer "and then what?" for the world after the merge, not the work up to it, and name what it displaces against a fixed budget. Say explicitly when the cost is zero or negative (it retires an obligation — see dimension 9); that is a strong signal.

Rank by value weighed **against** ongoing cost, not by value alone:

- **High impact**: fundamentally changes outcome or quality, and the value clearly outweighs the obligation
- **Medium impact**: meaningfully improves maintainability, coverage, or experience at a proportionate cost
- **Low impact**: polish and long-term investment — or high value bought at a cost the owner would not choose to carry

### Step I4.5: Premortem

Run the Default Mode Step 3.5 premortem (same fixed question, priors, silent degradation, one round at every plan size), with three differences:

- Seed failure modes from the Step I2 research as well as the plan and priors — the architecture, pain points, and adjacent systems already read are the richest source of plan-specific mechanisms. No new research.
- A divergence the user is about to adopt is fair game: "we adopted the ideal here and the outcome was still wrong, why?" is often the sharpest option. Dimension 9 candidates and the ongoing-cost fields feed it.
- Apply no edits here; Step I8 binds the answers to contract checks and `## Risks` bullets.

### Step I5: Assess Completeness Using the Shared Research

Run the Default Mode Step 2 assessment grounded in the Step I2 findings, and print the Step 3 table alongside the divergence summary so the user sees gaps and ambition in one view.

### Step I6: Ask the User to Adopt, Defer, or Reject

Present divergences highest impact first:

- **High impact**: AskUserQuestion, 2-3 divergences per call, each with
  - `header`: a short divergence label (max 12 chars)
  - `question`: what the plan does, what the ideal does instead, the gap, and the value
  - `options`:
    - label: "Adopt" / description: "Edit this improvement into the plan now"
    - label: "Defer" / description: "Record as consciously deferred in the plan"
    - label: "Reject" / description: "Drop it — not worth pursuing"
- **Medium impact**: one grouped multi-select question offering adoption; anything not selected is recorded as deferred unless the user says to drop it.
- **Low impact**: listed in the final report only.

If completeness dimensions remain thin or missing afterwards, fold in at most one Default Mode Step 4 Q&A round, reusing the Step I2 research.

### Step I7: Apply the Decisions to the Plan File

- **Adopted divergences**: edit the improvement into the relevant sections (scope, design, sessions, verification) per the Step 4c edit rules. Accepted ideal improvements must be present in the plan file rather than only conversation history.
- **Deferred divergences**: one bullet each under `## Consciously Deferred` (create it if absent) with a one-line rationale, including the ongoing cost when that was the deciding factor.
- **Rejected divergences**: not recorded.
- **Q&A answers**: apply per Step 4c.
- **Premortem output**: routed by Step I8.

**Durable-section placement** — `## Consciously Deferred` and `## Risks` are plan-level sections. When the plan has session headings, put them **above the first session heading**: `parse_plan_document` treats only the lines before the first session heading as shared context, so a section after the last heading is absorbed into that session and never reaches `context.md`. In a plan with no session headings yet, place them near the end.

### Step I8: Ensure the Plan Is Ready for `asl prepare`

`asl prepare` otherwise exits with reason `needs-plan-iteration` and per-session blockers before creating any directory. Verify — and add or repair where missing — that:

- The plan has **session boundaries**: at least two headings accepted by `parse_plan_document` outside code fences. Preserve existing accepted forms; when adding or repairing a boundary, author the canonical `## Session <id>: Title` form.
- Every session block carries an inline `## Completion Contract` with:
  - a `### Done when` bullet list stating that session's own done criteria, and
  - a `### Verify` fenced ```json block parseable by `contracts.parse_completion_contract` with that session's own verification recipe.
  Neither may be blank, generic, or deferred to a later session — a session only a successor can prove finished cannot converge on its own.
- Contract session IDs use the canonical unpadded ID the heading parser returns.
- Checks carry `baseline_expectation` (`pass` or `fail`) where a meaningful pre-implementation expectation exists; checks without it carry no baseline obligation.
- `review_replay: true` marks only unconditional gates asserting durable current state that every later review should recheck. Never mark point-in-time assertions (HEAD identity, dirty tree, intermediate files, baseline-only facts) or an `on_failure_of` diagnostic.
- Author every check to the contract authoring rules in `session-split` Step 5 rather than restating them here (notably: `python3`, never bare `python`; no ambiguity sentinels; `timeout_seconds <= 3600`; in a registered plan, a `repo` alias in the session's read scope with repository-relative paths and no `..`).
- The session structure is deliberate and evidence-based. Keep the unconstrained ideal distinct from the next executable increment. Do not add artificial sessions solely for `asl prepare`, and do not narrow scope unless the user explicitly chooses that reduction (recording it under `## Consciously Deferred`).

Derive done criteria and verification recipes from the plan and the Step I2 research; ask the user (one AskUserQuestion round at most) only when a verification command is genuinely unknowable from both.

**Bind the premortem output to enforcement.** Every Step I4.5 failure mode lands in exactly one of two places:

- **Expressible as a check → it becomes one**: a `command`, `file_exists`, or `regex_in_file` entry in the `### Verify` recipe of the session whose work would produce the failure. Prefer the narrowest check that would actually catch it, not a suite rerun. Give it `baseline_expectation: "fail"` unless it genuinely passes before implementation — it asserts the property whose absence is the predicted failure.
- **Not expressible → a `## Risks` bullet** naming the mechanism and what to watch for, placed by the Step I7 rule. "Not expressible" means no check can distinguish failure from success, not that the check is inconvenient to write.

Both survive preparation: `## Risks` above the first session heading flows into `context.md`, and inline contracts are lifted into each session file.

### Step I9: Final Report

```
Plan ideal-iteration complete.

Research pass: 1 (<N> files examined)
Divergences: <H> high / <M> medium / <L> low
Subtraction: <S> removal/consolidation opportunit(ies) identified, <SA> adopted
Decisions: <A> adopted, <D> deferred, <R> rejected
Edits applied: <K>
Premortem: <P> failure mode(s) surfaced -> <C> contract check(s), <R> ## Risks bullet(s)
Base-rate priors: <"N recurring categories from asl metrics" or "unavailable">
Completeness: <improved dimensions or "all adequate">
Prepare gate: session boundaries, completion contracts, and verify recipes present

Next step: `asl prepare <plan-path> --cwd <repo-root>`
```

## Ideal-Only Mode (`--ideal-only`)

Run Steps I1 through I4, then print the ideal report below to the conversation. **Never modify the plan file** in this mode.

```
# Ideal Vision: <plan title>

**Plan file**: `<path>`
**Codebase research**: <N> files examined
**Assessment**: <one sentence on how ambitious the plan is relative to the ideal>

## The Ideal Version

<2-4 paragraph narrative of the holistic vision — how the perfect
implementation would work, feel to use, and fit into the codebase.>

## Dimension Analysis

### Architecture & Design
### Completeness & Coverage
### User / Developer Experience
### Performance & Efficiency
### Testing & Confidence
### Observability & Operations
### Extensibility & Future-Proofing
### Ecosystem & Integration
### Subtraction & Consolidation

## Divergences from Current Plan

Ranked by value weighed against ongoing cost.

### High Impact
- **<divergence title>**: <current> vs <ideal>. Gap: <what changes>.
  Value: <why it matters>. Ongoing cost: <the permanent obligation this
  creates, or "none — retires <obligation>">.

### Medium Impact
- ...

### Low Impact
- ...

## What This Means for Planning

<2-3 paragraphs: which high-impact divergences are worth pursuing within
practical constraints, the minimum viable ideal, and a phased path toward it.>

Next step: `plan-iterate <plan-path> --ideal --cwd <repo-root>`
```

## Important Notes

- Mental models: read the sibling reference `../references/mental-models.md` on demand rather than inlining it, and cite at most 3 models by name per report. This skill's assigned models are **inversion** — the Step 3.5 / Step I4.5 premortem, which asks how the plan fails instead of whether it is complete — **via negativa**, dimension 9's question of what the ideal removes rather than adds, and **second-order thinking**, the Step I4 ongoing-cost field's "and then what?"
- Never delete existing plan content unless the user explicitly asks; only modify the provided plan file and never create new files.
- Very short plans (< 15 lines) are likely quick fixes: at most 1 Q&A round, focused on Context and Verification; in ideal mode, scale the vision down per Step I3. The premortem and dimension 9 still run.
- In default mode, codebase checks are opportunistic, not exhaustive — Glob/Grep only when a quick check improves a question. Do not perform `session-expand`-style validation.
- In ideal and ideal-only modes, the Step I2 research pass is the **only** research pass. Do not research the codebase twice.
- **Do not constrain the ideal vision** — describe the ceiling, not the floor. If the ideal means rewriting 50 files across 10 sessions, say so; the user decides what is practical via adopt/defer/reject.
- **Acknowledge what the plan already does well** — where a dimension is already well-addressed, say so and describe only the incremental ideal beyond it.
- **Bash is read-only in every mode**: `ls`, `find`, `wc`, `head`, `tail`, `git log`, `git diff`, `asl split --check --plan-file <plan> --json`, and `asl metrics --json` (only with `--json`, `--since-days`, or `--state-dir`; its one side effect is a 0-byte lock file in the state root, which is acceptable). Never write with Bash; plan mutations happen only through the Edit tool on the plan file.
- If the user answers "skip" or "not relevant", move on without editing.
