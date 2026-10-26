"""Static repair hints keyed by preflight finding category and check id."""

from __future__ import annotations

from .preflight_models import PreflightCategory, RepairHint

REPAIR_HINTS: dict[PreflightCategory, RepairHint] = {
    "plan-structure": RepairHint(
        category="plan-structure",
        check_id="plan-structure.recreate",
        severity_hint="red",
        message=(
            "Recreate the plan directory with a valid context.md, manifest.md, "
            "and at least one canonical session-N.md file."
        ),
        question_template=(
            "Plan directory at {path} is unreadable or missing required files. "
            "Should I recreate it from the source plan?"
        ),
        fix_category="plan-recreate",
    ),
    "canonical-session-id": RepairHint(
        category="canonical-session-id",
        check_id="canonical-session-id.rename",
        severity_hint="red",
        message=(
            "Rename the non-canonical session file to the canonical session-N.md "
            "form shown in the finding detail."
        ),
        question_template=(
            "Session filename at {path} is non-canonical ({detail}). Apply the rename?"
        ),
        fix_category="filename-rename",
    ),
    "manifest-readiness": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.repair",
        severity_hint="red",
        message=(
            "Re-run `asl preflight <plan-dir> --json --mark-loop-ready` to "
            "refresh the manifest readiness block, or fix the manifest by hand."
        ),
        question_template=(
            "Manifest readiness metadata needs attention: {message}. "
            "Should I rerun preflight to refresh it?"
        ),
        fix_category="manifest-update",
    ),
    "missing-contract": RepairHint(
        category="missing-contract",
        check_id="missing-contract.add",
        severity_hint="red",
        message=(
            "Add a `## Completion Contract` section to the session file with "
            "`### Done when` bullets and a fenced ```json``` Verify recipe."
        ),
        question_template=(
            "Session {session_id} is missing a Completion Contract. "
            "Should I help you draft one?"
        ),
        fix_category="plan-edit",
    ),
    "malformed-contract": RepairHint(
        category="malformed-contract",
        check_id="malformed-contract.repair",
        severity_hint="red",
        message=(
            "Fix the Completion Contract syntax reported by the parser error "
            "in the finding message."
        ),
        question_template=(
            "Session {session_id} has a malformed Completion Contract: {message}. "
            "Should I walk you through the fix?"
        ),
        fix_category="plan-edit",
    ),
    "ambiguity-sentinel": RepairHint(
        category="ambiguity-sentinel",
        check_id="ambiguity-sentinel.replace",
        severity_hint="yellow",
        message=(
            "Replace the ambiguity sentinel (TBD/TODO/FIXME/etc.) with "
            "concrete instructions before running the session."
        ),
        question_template=(
            "Session {session_id} contains an ambiguity sentinel at line {line}: "
            "{detail}. Resolve it now?"
        ),
        fix_category="plan-edit",
    ),
    "path-reference": RepairHint(
        category="path-reference",
        check_id="path-reference.normalize",
        severity_hint="yellow",
        message=(
            "Rewrite the relative contract path so it stays beneath the bound "
            "root without '..' traversal."
        ),
        question_template=(
            "Session {session_id} relative contract path traverses outside "
            "--cwd ({detail}). Rewrite it?"
        ),
        fix_category="plan-edit",
    ),
    "repository-footprint": RepairHint(
        category="repository-footprint",
        check_id="repository-footprint.register",
        severity_hint="red",
        message=(
            "This plan reaches outside the single root it is bound to, so it "
            "cannot stay in legacy single-repository mode. Run "
            "`asl register-repositories <plan-dir> --cwd <repo-root>` to "
            "migrate it: the command derives from disk the `## Repositories` "
            "table, the per-session **Reads repositories**: and **Writes "
            "repositories**: scopes, and the `repo` alias plus "
            "repository-relative `path`/`cwd` on every contract check, then "
            "prints the `asl prepare --resume --rebind-workspace` command to "
            "run next. It writes nothing and names the missing fact when a "
            "fact cannot be observed."
        ),
        question_template=(
            "Plan reaches outside its bound root ({detail}). Should I run "
            "`asl register-repositories` to migrate it to a registered "
            "workspace?"
        ),
        fix_category="plan-edit",
    ),
    "repository-registry-required": RepairHint(
        category="repository-registry-required",
        check_id="repository-registry-required.declare",
        severity_hint="red",
        message=(
            "This plan intends to reach more than one Git repository, so it "
            "cannot run in legacy single-repository mode and no single child "
            "checkout may be selected as its root. Run "
            "`asl register-repositories <plan-dir> --cwd <repo-root>` to "
            "declare the registry: the command derives from disk the "
            "top-level `## Repositories` table, the per-session **Reads "
            "repositories**: and **Writes repositories**: scopes, and the "
            "`repo` alias plus repository-relative `path`/`cwd` attributing "
            "every contract check, then prints the `asl prepare --resume "
            "--rebind-workspace` command to run next. It writes nothing and "
            "names the missing fact when a fact cannot be observed."
        ),
        question_template=(
            "Plan intends to reach several Git repositories with no "
            "`## Repositories` registry ({detail}). Should I run "
            "`asl register-repositories` to declare the registry and "
            "per-session repository scopes?"
        ),
        fix_category="plan-edit",
    ),
    "verification-scope": RepairHint(
        category="verification-scope",
        check_id="verification-scope.narrow",
        severity_hint="red",
        message=(
            "Narrow the verification recipe so it targets only the package(s) "
            "this session actually changes; ASL session verification must not "
            "run repo-wide `./operator health` commands."
        ),
        question_template=(
            "Session {session_id} contains an overly broad verification "
            "command ({detail}). Should I narrow the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "verification-duplicate": RepairHint(
        category="verification-duplicate",
        check_id="verification-duplicate.remove",
        severity_hint="yellow",
        message=(
            "Remove the duplicate command check. Keep one authoritative gate, "
            "or make a genuinely narrower check conditional with "
            "`on_failure_of` when it adds failure diagnostics."
        ),
        question_template=(
            "Session {session_id} repeats an identical verification command "
            "({detail}). Remove the duplicate?"
        ),
        fix_category="plan-edit",
    ),
    "command-not-runnable": RepairHint(
        category="command-not-runnable",
        check_id="command-not-runnable.fix",
        severity_hint="red",
        message=(
            "Fix the contract command so it can run in this environment: use "
            "`python3` (not `python`) if only python3 is on PATH, install the "
            "missing runner, or switch to the repo's actual test convention "
            "(e.g. `python3 -m unittest`)."
        ),
        question_template=(
            "Session {session_id} contract command is not runnable ({detail}). "
            "Should I fix the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "progress-journal-missing": RepairHint(
        category="progress-journal-missing",
        check_id="progress-journal-missing.backfill",
        severity_hint="yellow",
        message=(
            "Run `asl backfill-progress --force <plan-dir>` to reconstruct "
            "missing progress.md, or the same command when the journal "
            "already exists but is stale or malformed."
        ),
        question_template=(
            "Progress journal problem: {message}. "
            "Should I run `asl backfill-progress` to reconstruct it?"
        ),
        fix_category="manifest-update",
    ),
    "baseline-missing": RepairHint(
        category="baseline-missing",
        check_id="baseline-missing.refresh",
        severity_hint="red",
        message=(
            "Record baseline evidence before the first session by running "
            "`asl baseline <plan-dir> --cwd <repo-root>`; the readiness gate "
            "requires current evidence when no session is complete."
        ),
        question_template=(
            "Session {session_id} declares a baseline expectation but no "
            "evidence is recorded ({detail}). Should I run `asl baseline`?"
        ),
        fix_category="manifest-update",
    ),
    "baseline-stale": RepairHint(
        category="baseline-stale",
        check_id="baseline-stale.refresh",
        severity_hint="yellow",
        message=(
            "Refresh stale baseline evidence with "
            "`asl baseline <plan-dir> --cwd <repo-root>`; the recorded "
            "repository fingerprint no longer matches the working tree."
        ),
        question_template=(
            "Session {session_id} baseline evidence is stale ({detail}). "
            "Should I re-run `asl baseline` to refresh it?"
        ),
        fix_category="manifest-update",
    ),
    "baseline-unexpected": RepairHint(
        category="baseline-unexpected",
        check_id="baseline-unexpected.fix",
        severity_hint="red",
        message=(
            "Resolve the baseline mismatch: fix the pre-existing failure, or "
            "correct the check's `baseline_expectation` in the session recipe "
            "so it matches reality."
        ),
        question_template=(
            "Session {session_id} baseline check did not match its declared "
            "expectation ({detail}). Should I fix the recipe or the code?"
        ),
        fix_category="session-recipe-edit",
    ),
    "baseline-timeout": RepairHint(
        category="baseline-timeout",
        check_id="baseline-timeout.fix",
        severity_hint="red",
        message=(
            "The baseline check exceeded its budget. Narrow the recipe to the "
            "session's own targeted tests, or raise the budget only if the "
            "slow command is genuinely required."
        ),
        question_template=(
            "Session {session_id} baseline check timed out ({detail}). "
            "Should I narrow the recipe?"
        ),
        fix_category="session-recipe-edit",
    ),
    "baseline-near-timeout": RepairHint(
        category="baseline-near-timeout",
        check_id="baseline-near-timeout.split",
        severity_hint="yellow",
        message=(
            "The baseline check ran within 80% of its budget. Consider "
            "narrowing the recipe to targeted tests so it does not begin "
            "timing out as the repository grows."
        ),
        question_template=(
            "Session {session_id} baseline check is near its timeout budget "
            "({detail}). Should I narrow the recipe?"
        ),
        fix_category="session-recipe-edit",
    ),
    "workspace-root": RepairHint(
        category="workspace-root",
        check_id="workspace-root.use-cwd",
        severity_hint="red",
        message=(
            "Pass the existing common workspace directory via `--cwd`; for a "
            "registered plan it does not need to be a Git repository."
        ),
        question_template=(
            "Workspace root {path} is invalid ({message}). Which directory "
            "should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "repository-registry": RepairHint(
        category="repository-registry",
        check_id="repository-registry.repair",
        severity_hint="red",
        message=(
            "Repair the manifest `## Repositories` table so aliases, relative "
            "paths, and credential-free expected origins are valid and unique."
        ),
        question_template=(
            "The repository registry is invalid ({message}). Should I repair "
            "the manifest table?"
        ),
        fix_category="plan-edit",
    ),
    "repository-root": RepairHint(
        category="repository-root",
        check_id="repository-root.repair",
        severity_hint="red",
        message=(
            "Restore the registered checkout as an exact Git worktree root "
            "with readable Git evidence."
        ),
        question_template=(
            "A registered repository root is invalid ({message}). Should I "
            "inspect the checkout layout?"
        ),
        fix_category="env-state",
    ),
    "repository-origin": RepairHint(
        category="repository-origin",
        check_id="repository-origin.correct-checkout",
        severity_hint="red",
        message=(
            "Use the checkout whose credential-free origin matches the registry, "
            "or correct the registry if the declared origin is wrong."
        ),
        question_template=(
            "A registered repository has the wrong origin ({message}). Should I "
            "inspect the checkout and manifest entry?"
        ),
        fix_category="env-state",
    ),
    "repository-overlap": RepairHint(
        category="repository-overlap",
        check_id="repository-overlap.separate",
        severity_hint="red",
        message=(
            "Use disjoint repository paths and exact Git top-levels; nested or "
            "duplicate registered roots are not supported."
        ),
        question_template=(
            "Registered repository roots overlap ({message}). Should I repair "
            "the registry paths?"
        ),
        fix_category="plan-edit",
    ),
    "repository-scope": RepairHint(
        category="repository-scope",
        check_id="repository-scope.repair",
        severity_hint="red",
        message=(
            "Add exactly one valid Reads repositories field and Writes "
            "repositories field; writes must be a subset of reads and all aliases "
            "must exist in the registry."
        ),
        question_template=(
            "Session {session_id} has invalid repository scope ({message}). "
            "Should I repair its scope fields?"
        ),
        fix_category="plan-edit",
    ),
    "repository-contract": RepairHint(
        category="repository-contract",
        check_id="repository-contract.repair",
        severity_hint="red",
        message=(
            "Give every registered completion check a valid repository alias and "
            "keep path/cwd values canonical and beneath that repository."
        ),
        question_template=(
            "Session {session_id} has an invalid repository-aware contract "
            "({message}). Should I repair the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "env-repo-root": RepairHint(
        category="env-repo-root",
        check_id="env-repo-root.use-cwd",
        severity_hint="red",
        message=(
            "Pass an existing repository directory via `--cwd` when invoking "
            "the command."
        ),
        question_template=(
            "Repo root {path} does not exist or is not a directory. "
            "Which path should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "env-git": RepairHint(
        category="env-git",
        check_id="env-git.repair",
        severity_hint="red",
        message=(
            "Run from inside a git working tree, or resolve the git status "
            "problem shown in the finding detail."
        ),
        question_template=("Git environment problem: {message}. Should I try a fix?"),
        fix_category="env-state",
    ),
    "env-harness": RepairHint(
        category="env-harness",
        check_id="env-harness.install-cli",
        severity_hint="red",
        message=(
            "Install the harness CLI on PATH, or point it at the right "
            "executable in `~/.config/agent-session-loop/harnesses.json` "
            "(`$XDG_CONFIG_HOME/agent-session-loop/harnesses.json` when "
            "`XDG_CONFIG_HOME` is set)."
        ),
        question_template=(
            "Harness CLI problem: {message}. Should I help configure harnesses.json?"
        ),
        fix_category="env-install",
    ),
    "env-lock": RepairHint(
        category="env-lock",
        check_id="env-lock.repair",
        severity_hint="red",
        message=(
            "Run `asl unlock <plan-dir> --json` to clear a stale lock, or "
            "wait for the active runner to finish."
        ),
        question_template=(
            "Lock contention: {message}. "
            "Should I run `asl unlock <plan-dir> --json` or wait for the "
            "runner?"
        ),
        fix_category="env-state",
    ),
    "env-amx": RepairHint(
        category="env-amx",
        check_id="env-amx.optional",
        severity_hint="green",
        message=(
            "Agent Mux integration is optional and never blocks loop-ready. "
            "No fix required."
        ),
        question_template=(
            "Agent Mux hooks are optional ({message}). No action is required."
        ),
        fix_category="informational",
    ),
    "env-turn-budget": RepairHint(
        category="env-turn-budget",
        check_id="env-turn-budget.healthy",
        severity_hint="green",
        message=("Turn budget is within the expected range. No fix required."),
        question_template=(
            "Turn budget is healthy ({message}). No action is required."
        ),
        fix_category="informational",
    ),
}
