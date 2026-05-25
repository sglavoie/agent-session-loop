---
name: plan-ideal
description: Envision the optimal, unconstrained version of a plan. Deeply researches the codebase and describes what the ideal implementation would look like if time and labor were unlimited. Read-only output to conversation only.
allowed-tools: Read, Glob, Grep, Bash
---

# Plan Ideal

## Purpose

Given a plan file, produce an ambitious, deeply-reasoned description of what the **optimal, unconstrained version** of that plan would look like — as if time, labor, and session budgets were unlimited. This is a visionary step: it describes rather than executes, reimagines rather than refines. The output challenges the plan's ambition level and gives the user a ceiling to aim toward.

This is explicitly **not** scoped to a single session. The ideal vision may span many sessions and touch many files. That is the point.

Use before the `plan-iterate` skill to inform what to prioritize, or standalone to evaluate whether a plan is thinking big enough.

## Input

The user provides a plan file path as the host agent command or skill invocation argument. Example:

```
plan-ideal ~/.local/share/agent-plans/tranquil-singing-lantern.md
```

## Instructions

### Step 1: Read and Deeply Understand the Plan

Read the entire plan file at the provided path.

Parse the plan to identify:
- **Goal**: What is the plan trying to achieve?
- **Scope**: What boundaries has the plan drawn (explicitly or implicitly)?
- **Approach**: What technical strategy does it use?
- **Constraints**: What has been scoped out, deferred, or simplified?
- **Assumptions**: What does the plan take for granted about the codebase, architecture, or tooling?

Take careful note of anything that sounds like a compromise: "for now", "simple version", "future work", "out of scope", "v2", "stretch goal", "good enough", "MVP", "later", "ideally", "if time permits". These are seeds for the ideal vision.

### Step 2: Research the Codebase

Use Glob, Grep, and Read to understand the current state of the codebase as it relates to the plan's domain. Focus on:

1. **Architecture**: How is the relevant area currently structured? What are the layers, abstractions, and module boundaries?
2. **Patterns**: What conventions does the codebase follow? (error handling, testing, configuration, module structure)
3. **Pain points**: Are there TODO comments, HACK markers, FIXME notes, or suppressed warnings in the relevant area? These reveal latent problems the ideal solution could address.
4. **Adjacent systems**: What nearby code would benefit from or be affected by an ideal version of this plan?
5. **Test coverage**: What testing patterns exist? Where are the gaps?

**Research rules**:
- Spend real effort here — this is where the skill earns its value. Read key files in the affected area, not just the ones the plan mentions.
- Use `Grep` to find related patterns across the codebase (e.g., if the plan adds a new command, find all existing commands to understand the full pattern).
- Use `Bash` for read-only commands only: `wc`, `find ... -type f | head`, `git log --oneline -20 -- <path>`, `ls`. Never modify files.
- Aim for 5-15 files read, depending on plan complexity. For large plans, prioritize breadth over depth.

### Step 3: Envision the Ideal Across Eight Dimensions

For each dimension below, describe what the **unconstrained ideal** would look like. Think as if you had unlimited sessions, unlimited time, and a team of expert collaborators. Do not self-censor for feasibility. Use extended thinking to reason deeply before writing each dimension.

**Dimension 1 — Architecture & Design**
What would the ideal structural design be? Consider:
- Would the ideal version introduce new abstractions, modules, or layers?
- Should the data model be different? More normalized? More denormalized?
- Are there better patterns from the broader ecosystem (open source, industry standards) that should be adopted?
- Would the ideal design make future extensions trivial rather than merely possible?

**Dimension 2 — Completeness & Coverage**
What would full coverage look like?
- What edge cases would the ideal version handle that the current plan ignores?
- What error paths would be covered with graceful recovery?
- What inputs, configurations, or environments would be supported?
- Are there adjacent features that logically belong together but were carved off?

**Dimension 3 — User / Developer Experience**
What would the ideal experience be for the end user and for developers maintaining this code?
- Would the ideal version have better defaults, clearer errors, or richer feedback?
- Would the API surface be more intuitive?
- Would the configuration be simpler or more powerful?
- Would documentation be generated, inline, or example-driven?

**Dimension 4 — Performance & Efficiency**
What would the ideal performance characteristics be?
- Are there algorithmic improvements that a time-unlimited version would pursue?
- Would caching, batching, or lazy evaluation help?
- Are there I/O patterns that could be optimized?
- What would the ideal resource footprint look like?

**Dimension 5 — Testing & Confidence**
What would the ideal testing strategy be?
- Unit tests, integration tests, end-to-end tests, property-based tests, fuzzing?
- What test infrastructure would need to exist?
- Would the ideal version include benchmarks, regression tests, or snapshot tests?
- What would continuous verification look like?

**Dimension 6 — Observability & Operations**
What would the ideal operational story be?
- Logging, metrics, tracing, health checks?
- How would failures be detected and diagnosed?
- Would the ideal version be more debuggable?
- What monitoring or alerting would accompany it?

**Dimension 7 — Extensibility & Future-Proofing**
What would make this solution age well?
- Plugin points, hooks, or extension mechanisms?
- Configuration-driven behavior instead of hardcoded logic?
- Migration paths for future changes?
- Would the ideal version anticipate known future requirements?

**Dimension 8 — Ecosystem & Integration**
What would the ideal integration with the broader system look like?
- Would the ideal version unify with or replace adjacent functionality?
- Are there cross-cutting concerns (auth, caching, error handling) that should be standardized?
- Would it enable new capabilities in other parts of the system?

**Scaling rules** — not every dimension applies equally:
- Plans under 30 lines: Focus on dimensions 1, 2, 3, and 5. Treat 4, 6, 7, 8 lightly or skip with a note.
- Plans 30-100 lines: Cover all dimensions but weight 1, 2, 3, 5 most heavily.
- Plans over 100 lines: All dimensions get full treatment.

### Step 4: Identify Divergences from the Current Plan

Explicitly compare the ideal vision to the current plan. For each significant divergence, describe:

1. **What the plan does**: the current approach or scope
2. **What the ideal does instead**: the unconstrained version
3. **The gap**: what would need to change to move from current to ideal
4. **The value**: what the user gains from the ideal version

Organize divergences by impact:
- **High impact**: Fundamentally changes the outcome or quality
- **Medium impact**: Meaningfully improves maintainability, coverage, or experience
- **Low impact**: Polish, completeness, and long-term investment

### Step 5: Synthesize and Present

Print the full ideal description to the conversation using this structure:

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
<findings>

### Completeness & Coverage
<findings>

### User / Developer Experience
<findings>

### Performance & Efficiency
<findings>

### Testing & Confidence
<findings>

### Observability & Operations
<findings>

### Extensibility & Future-Proofing
<findings>

### Ecosystem & Integration
<findings>

## Divergences from Current Plan

### High Impact
- **<divergence title>**: <current> vs <ideal>. Gap: <what changes>.
  Value: <why it matters>.

### Medium Impact
- ...

### Low Impact
- ...

## What This Means for Planning

<2-3 paragraphs of synthesis:
- Which high-impact divergences are worth pursuing even within practical
  constraints?
- What is the minimum viable ideal — the smallest set of changes that
  captures the most value from the ideal vision?
- What would a phased approach look like if the user wanted to eventually
  reach the ideal?>

Next step: `plan-iterate <plan-path>`
```

## Important Notes

- **Never modify the plan file** — all output goes to the conversation. This skill is purely descriptive and read-only.
- **Do not constrain the vision** — the whole point is to describe the ceiling, not the floor. If the ideal requires rewriting 50 files across 10 sessions, say so. The user will decide what is practical.
- **Use extended thinking** — this skill benefits from deep reasoning. Take time to think through each dimension thoroughly before writing the output.
- **Codebase research is essential** — the ideal vision must be grounded in the actual codebase state, not generic best practices. The difference between "you should add tests" and "the `internal/replay/` package has zero test files, and `replayer.go` has 4 exported functions that are untested" is what makes this skill valuable.
- **Acknowledge what the plan already does well** — the ideal vision is not a criticism. If a dimension is already well-addressed by the current plan, say so and describe only the incremental ideal beyond it.
- **Scale depth to plan complexity** — a 15-line bug fix plan does not need 8 paragraphs of architecture analysis. Use the scaling rules in Step 3. For small plans, the entire output might be 30-50 lines. For large plans, it could be 200+.
- **This is multi-session thinking** — do not worry about whether the ideal fits in one session. The ideal might be a 12-session undertaking. That is fine. The user will use `plan-iterate` and `session-split` to make it practical.
- **Bash is read-only** — only use Bash for commands that do not modify the filesystem: `ls`, `find`, `wc`, `git log`, `git diff`, `head`, `tail`. Never use Bash for `mkdir`, `touch`, `rm`, `cp`, `mv`, or any write operation.
