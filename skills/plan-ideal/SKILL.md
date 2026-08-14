---
name: plan-ideal
description: Envision the optimal, unconstrained version of a plan. Read-only alias for `plan-iterate --ideal-only` — researches the codebase once and describes what the ideal implementation would look like without modifying the plan file.
allowed-tools: Read, Glob, Grep, Bash
---

# Plan Ideal

## Purpose

Legacy read-only entry point for plan idealization. This skill is an alias for `plan-iterate --ideal-only`: it produces the same ideal vision and impact-ranked divergence report, printed to the conversation only, and **never modifies the plan file**.

The idealization workflow now lives in the `plan-iterate` skill so the daily ideal-plus-iterate workflow shares one research pass and accepted improvements land in the plan file. Use this alias when you only want the read-only vision; use `plan-iterate PLAN.md --ideal --cwd REPO_ROOT` when you want the vision plus adopt/defer/reject decisions edited into the plan.

## Input

The user provides a plan file path as the host agent command or skill invocation argument:

```
plan-ideal PLAN.md
plan-ideal PLAN.md --cwd REPO_ROOT
```

`--cwd REPO_ROOT` optionally sets the codebase root for research; when absent and the plan clearly references a codebase other than the current working directory, ask the user for the root before researching.

## Instructions

1. Resolve the plan path and codebase root from the invocation arguments. Reject unknown flags and stop without making changes.
2. Read the sibling skill file `../plan-iterate/SKILL.md` (the `plan-iterate` skill directory installed alongside this one).
3. Follow its **Ideal-Only Mode (`--ideal-only`)** instructions with the resolved plan path and codebase root: Steps I1-I4 (read and deeply understand the plan, one shared research pass, envision the ideal across that mode's dimensions, identify divergences ranked by impact), then print the full ideal report using that mode's output template.
4. End the report with `Next step: plan-iterate <plan-path> --ideal --cwd <repo-root>` so the user can turn the vision into durable plan edits.

## Important Notes

- **Never modify the plan file** — all output goes to the conversation. This skill is purely descriptive and read-only.
- **Bash is read-only** — only commands that do not modify the filesystem: `ls`, `find`, `wc`, `git log`, `git diff`, `head`, `tail`. Never use Bash for any write operation.
- This alias delegates to `plan-iterate --ideal-only` so the idealization instructions have a single source of truth; do not duplicate or fork them here.
- Never create new files.
