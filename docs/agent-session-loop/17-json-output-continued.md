<!-- Continuation of the previous part. -->

`paused_work_commit` is the settlement record a recoverable pause writes, and it
is the same object in three places: on the paused turn's `result.json`, on the
run payload, and under `pause_details.paused_work_commit`, where a supervisor
reading the pause finds it without opening a turn artifact. Its fields are
`guard` (always `paused-work-commit`, naming the boundary that wrote it the way
`session_commit` and `repair_commit` do), the `pause_reason` and `commit_mode`
it settled under, `result`, the three repair counters
`verification_repair_attempt`, `verification_repair_max_attempts`, and
`verification_repair_exhausted` read from the pause details rather than
recounted, the `commit_group` it ran, the failed `commit` when one came back
unsuccessful, an `error` string when the resolution refused before any group
existed, and `residue`. `result` is one of `committed` (the relaunch starts on a
clean worktree), `no-changes` (the turn retained nothing to commit),
`committed-with-residue`, or `failed` (the retained changes stay in the worktree
for a human). `committed-with-residue` is not reachable through today's
`asl commit` boundary: that boundary re-reads the same `git status` and calls a
commit that left its worktree dirty unsuccessful, so the group fails and the
settlement records `failed` first. The residue that matters in practice
therefore arrives on the `failed` branch, where the record would otherwise read
as "nothing was committed" — or, worse, as a clean tree — when neither is true.
The fourth result is kept
because the commit boundary's cleanliness verdict and the settlement's own probe
are two independent measurements, and a record must not claim a clean tree the
run never confirmed. `residue.repositories` lists one row per resolved target
the probe did not find clean, each with its `alias`, `clean`, repository-qualified
`dirty_paths`, and an `error`. The probe is unconditional: every target the
settlement resolved is re-probed whatever the group's verdict was, because
`failed` is one word for the whole set and it is wrong about the repositories
that did commit. So a commit that exited nonzero names everything its target
still holds, a commit whose `HEAD` advanced onto a still-dirty tree names what
it left, and a partial multi-repository failure names the dirty target while
omitting the ones it had already committed clean — those exact paths are what
the next process's startup commit gate will refuse over. A target whose
`git status` cannot be read is recorded as undetermined rather than as clean:
its row carries `clean: null`, empty `dirty_paths`, and the git failure in its
own `error`, because the commit may have failed precisely because that
repository is unreadable and a probe on the way to a pause must not raise. On
every other row `error` is `null`. The record's own top-level `error` is a
different field, reserved for an
exception raised around the commit; a commit that merely failed is carried on
`commit` as `success: false` with its own `status_result`, and on the group as
`status: "failed"`.

The sentence recorded beside that failure is rendered from the same record,
because `failed` covers outcomes that ask the operator for different things. Two
facts select it: whether the failing commit itself landed, and whether the group
had already committed other repositories before it.

A commit that never ran, exited nonzero, left `HEAD` missing, or left it
unchanged wrote nothing. When it was also the group's first repository, nothing
reached the history and `commit_failures[].message` still reads `the work
retained by the <reason> pause of session <id> could not be committed`.

A commit whose `HEAD` advanced — `dirty-after-commit`, or `status-unavailable`
when the tree could not be re-read afterwards — did commit the turn's intended
path, so its message reads `the <reason> pause of session <id> committed the
work it retained, but the worktree still holds <paths>`, naming up to five
repository-qualified residue paths and deferring any beyond that to
`paused_work_commit.residue`. A landed commit whose probe named no path at all
points at the same field instead of naming one.

A multi-repository group can also commit some of its repositories cleanly before
a later one refuses. The group's verdict is `failed` and the refusing commit
landed nothing, but the all-or-nothing sentence would deny commits that are
already in `git log`. So a refusal that follows successful repositories reads
`the <reason> pause of session <id> committed the work it retained in <names>,
but the work it retained in <name> could not be committed`, naming up to three
repositories from the group's own `successful_repositories` and deferring any
beyond that to the same field, and naming the repository the group stopped on
from its `failed_alias`. The residue clause `; the worktree still holds <paths>`
is appended when the probe named any path, and dropped when it named none — an
unreadable target leaves the correction about what committed intact. A group
that failed with successful repositories but no recorded `failed_alias` points
at `commit_group.remaining_aliases` for what is still owed.

Which branch applies is read from the commit's own `status_result` and the
group's own outcome lists rather than re-measured, so the message never
disagrees with the commit boundary about what landed or with the group about
which repositories moved. The same text is what the `commit failed` progress
line prints and what the supervisor audit bundle carries in `commit_failures`.

The record is free-form as far as the state projections are concerned.
`asl status`'s runner-state projection carries no `pause_details` key at all, so
there is no status schema for it to join, and the Recovery Center's pause-detail
copy names no keys, so it surfaces there unchanged. Turn compaction rewrites
only `before`, `after`, and `verification`, so the record survives it intact.
`metrics --json`'s `verification_repair` object counts the settlements it finds:
`paused_work_commits` is the number of settlement events in the window —
including `no-changes` and `failed`, so it is a count of settlements rather than
of commits that landed — and the additive `paused_work_commits_by_result` breaks
the same total down by `result`.

Dry-run `planned` payloads, harness invocation payloads, active turn state,
events, and turn `result.json` artifacts include the same model and effort
metadata. Source values are `flag`, `codex_config`, `copilot_settings`, and
`unresolved`; omitted effort is reported as `null`. The top-level `harness`
field is the startup harness. `turns[*].harness` and `active_turn.harness` are
authoritative for runtime-selected per-turn harnesses after `1`, `2`, or `3`.

`status --json` reports parsed session state, hashes, state paths, and lock
state. Each parsed session includes canonical string `id`; integer-only
sessions also include numeric `number` for compatibility. The top-level
`next_pending_id` is the canonical string ID, while `next_pending` remains a
numeric value for integer-only plans and a string for dotted plans. The
top-level payload also surfaces `active_turn` and `active_verification` (each
`null` when the corresponding phase is not in flight) and a `runner_state`
block with `stale_unfinished` (boolean) and `stale_reason` (string or `null`).
`stale_unfinished` is `true` whenever `state.json` is present without a
`run_finished_at` and no active lock guards it (no lock file, or a stale lock);
`stale_reason` carries a short human-readable explanation. `runner_state` also
carries `paused_at` and `pause_message`, and text output follows the regular
`Run state: run <id> started <ts>` line with the run's outcome:
`Run state: paused (<reason>) at <ts>` plus a `Pause detail: <message>` line,
`Run state: finished <reason> at <ts>`, or `Run state: stopped (<reason>) at
<ts>` for a run that ended neither successfully nor paused. Text output prints
`Run state: stale/orphaned (<reason>)` immediately under the regular
`Run state:` line whenever the runner state is orphaned. When that orphan is
*provably* dead the same call also marks it abandoned (see [Stale unfinished
`state.json` with no lock](20-troubleshooting.md#stale-unfinished-statejson-with-no-lock)), adding an
`abandoned` block to `runner_state` and printing
`Run state: abandoned (<detected_at> by <trigger>: <lock reason>)`.
`status --json` error
payloads echo the original argument in `plan_reference`: a plan directory that
does not exist (or an unresolved label) reports `reason: plan-not-found`, one
that exists but cannot be inspected reports `reason: malformed-plan`, an unreadable state
root reports `reason: state-error`, and a bare label matching more than one
existing plan directory reports `reason: ambiguous-plan` with
`details.state_root` and a sorted `details.candidates` list of canonical paths.
The missing-directory case is the same on every plan-scoped command: `run`,
`doctor`, `questions`, `review-verdict`, `contract-check`, `baseline`,
`register-repositories`, `compact-state`, `gc`, `unlock`, `workbench pin`,
`acknowledge-external-changes` and `accept-ref-updates` all report a `PLAN_DIR`
that does not exist as `reason: plan-not-found` with `paused: false` (a `run`
that never started is not a pause), while each keeps its own reason
(`malformed-plan`, `plan-error`, `plan-structure`, ...) for a plan that exists
but cannot be read.
Error payloads omit `plan_dir`, reserving that key for canonical paths in
successful payloads. In text mode an ambiguous label prints a concise stderr
error instructing the operator to pass a full plan directory path. `doctor
--json` reports the resolved harness executable, generated prompt, argv, and
model/effort metadata without invoking the harness. Text `run` and `doctor`
output print the selected model after `Harness:` and print effort only when an
explicit effort was supplied. `unlock --json` reports whether a lock existed,
whether it was removed, whether it was active or stale, and the lock path.

`sessions --json` prints a success payload with `command: "sessions"`,
`state_root` (canonical path), `filters` (an object with boolean `running`
and `recent`), `limit` (a non-negative integer), `running` (array of running
rows), `recent` (array of recent rows, truncated to `limit`), `stale_locks`,
`skipped`, and `counts`. `counts` mirrors the row arrays with integer
`running`, `recent`, `stale_locks`, and `skipped` fields. Running rows include
`kind` (`"turn"` for an active turn, `"verification"` while the runner is
executing a post-turn `### Verify` recipe, `"loop"` when the lock is alive but
no `active_turn` or `active_verification` exists), `plan_dir`, `state_dir`,
`run_id`, `harness`, `session_id`, `turn_index`, `pid`, `started_at`,
`elapsed_seconds`, `idle_seconds`, `last_activity_at`, `lock_active`, and
`lock_stale`. Verification rows also include `check_index`, `check_count`,
`check_name`, and `check_kind`; the text renderer appends a
`check k/n NAME` suffix and uses `null` for `idle_seconds` because verification
has no idle concept. Plain turn rows leave the verification fields `null`. Recent
rows include `plan_dir`, `state_dir`, `run_id`, `harness`, `session_id`,
`turn_index`, `status`, `finished_at`, `duration_seconds`, and
`artifact_dir`. Plan paths in JSON are always full canonical paths; the
text rendering uses basenames by default and switches to full paths when
`--verbose` is passed. A missing state root returns success with empty
arrays. Invalid parsed inputs such as `--limit 0` produce
`reason: input-error`; catastrophic state-root errors produce
`reason: state-error`.

`preflight --json`, `split --check --json`, and a refused `expand-ingest --json`
share one additive `repository_footprint` object. It is present only when the
plan implicates more than one repository or escapes its bound root, and is
omitted entirely otherwise:

```json
{
  "workspace_root": "/Users/dev/1_dev_projects",
  "bound_root": "/Users/dev/1_dev_projects/tundra_svc",
  "registry_present": false,
  "registry_required": true,
  "detected": [
    {
      "alias": "Kiln",
      "path": "tundra_Kiln",
      "origin": "github.com/tundra/Kiln.git",
      "root": "/Users/dev/1_dev_projects/tundra_Kiln",
      "evidence": [
        {"line": 42, "text": "…", "kind": "contract-path", "confidence": "hard"}
      ]
    }
  ],
  "out_of_root": [
    {"line": 42, "text": "…", "kind": "contract-path", "confidence": "hard"}
  ],
  "external": [
    {"line": 51, "text": "…", "kind": "prose", "confidence": "hard"}
  ],
  "unattributed": [{"line": 88, "text": "…", "candidates": ["Kiln", "SVC"]}],
  "suggested_registry_table": ["| Alias | Path | Origin |", "| --- | --- | --- |"],
  "suggested_scopes": {"1": {"reads": ["Kiln"], "writes": ["Kiln"]}}
}
```

`bound_root` is the root the plan was measured against; `workspace_root` is the
common parent the suggested registry is expressed relative to. `detected` rows
are discovered from disk — exact Git worktree roots, `git remote get-url
origin` normalized to a credential-free identity, and directory basenames
validated as aliases with deterministic numeric dedup — so nothing in
`suggested_registry_table` is invented. `kind` is one of `contract-path`,
`contract-cwd`, `handoff`, or `prose`, and `confidence` is `hard` or `soft`;
`soft` targets are advisory and never on their own force `registry_required`.
`out_of_root` lists the targets that escaped `bound_root` while still belonging
to a Git repository — the ones a registry row can describe, and the only ones
that force `registry_required`. `external` lists the escaping targets that
belong to no Git worktree at all; they are reported as evidence and never force
a registry, so a payload exists only when some other target already required
one. `unattributed` lists
path prose that resolved to no single repository and is the only part requiring
a human decision. `suggested_scopes` is keyed by session ID and derived from the
aliases that session's own checks and handoff entries resolve into, with writes
only where the session declares a handoff target.

`metrics --json` prints `command: "metrics"`, the resolved `state_root`, the
window as `since_days`, `window_start`, and `generated_at`, then
`state_dirs_scanned` and `state_dirs_active` beside the `turns`, `first_pass`,
`pauses`, `verification_repair`, `review_cycles`, `agent_mux_delivery`,
`artifacts`, `compaction`, `cli_invocations`, and `execution_timing` objects.
Every field in those
objects answers for
the window alone, including `review_cycles.unique_materializations` and its
`review_cycles.repairs_materialized` alias, so narrowing `--since-days` never
raises a count. `state_dirs_scanned` counts the whole corpus and
`artifacts.total_bytes` measures it, because size is not a windowed question;
`state_dirs_active` counts only the directories with in-window turn or event
activity, and a directory whose files were all last written before the window
opened is skipped before its event log is read.

`cli_invocations` is additive: every pre-existing metrics field keeps its name,
type, and meaning. It carries `trace_root`, `retention_days`, `records_read`,
`records_ignored`, `starts`, `completions`, `successes`, `nonzero_exits`,
`usage_errors`, `help_calls`, `interruptions`, `exceptions`,
`unmatched_starts`, the `by_command`, `by_help_depth`, `by_exit_code`,
`by_harness`, and `by_origin` count maps, a `durations_ms` object
(`count`, `median`, `p95`, `max`, `total`, with the summaries null when no
invocation completed in the window), and `recent_failures`. An invocation is
windowed by its start timestamp, so `cli_invocations` narrows with
`--since-days` exactly like every other section.

`execution_timing` is additive in the same way: every pre-existing metrics field
keeps its name, type, and meaning. It carries `window_start`, `window_end`, and
`tolerance_seconds`, then five objects.

`coverage` carries `plans_with_evidence`, `runs_with_evidence`, `runs_included`,
`runs_by_classification`, `included_runs_by_classification`, `exclusions`,
`runs_with_known_wall`, `runs_outside_share_basis`, `unattributable_records`,
and `unattributable_turn_results`. The classification maps use the collector's
own `complete`, `partial`, `inconsistent`, and `active` keys, and `exclusions`
uses its `active`, `dry-run`, `outside-window`, and `no-finish-time` reasons.

`phases` carries `wall_seconds`, `seconds`, `runs_with_phase`,
`share_basis_runs`, `share_basis_seconds`, `share_seconds`, and `shares`. All
four per-phase maps are keyed by the same eight phases — `implementation`,
`generated_repair`, `turn_unclassified`, `review`, `verification`, `commits`,
`assurance`, and `unattributed`. `seconds` and `runs_with_phase` cover every
included run; `share_seconds` and `shares` cover only the `complete` ones, and
`shares` is `null` rather than a map of zeroes when that basis is empty or has
no known duration. `implementation` plus `generated_repair` is the collector's
single `worker_execution` figure, split by repair lineage.

`verification_checks` carries `runs_with_checks`, `checks`,
`cumulative_seconds`, `phase_seconds`, `scoped_checks`,
`checks_without_command`, and `scopes`. `cumulative_seconds` is check work and
`phase_seconds` is elapsed verification time; the first may legitimately exceed
the second. Each entry in `scopes` carries `repository`, `working_directory`,
`plan_key`, `command`, `checks`, `runs`, `seconds`, `checks_without_duration`,
and `by_status`, ordered by descending `seconds` with the scope identity
breaking ties. `plan_key` is populated only when neither a repository key nor a
working directory was recorded, which is what keeps two unrelated projects'
same-named checks apart.

`repeated_attempts` carries `sessions_tracked`,
`sessions_with_additional_attempts`, `additional_attempts`,
`additional_attempt_seconds`, `additional_attempts_without_duration`, and
`additional_attempts_by_phase`. An attempt counts as additional when an earlier
attempt of the same session exists anywhere in the supplied history, including
before the window, and the seconds are the time those attempts took rather than
a measure of discarded work.

`review_scope` carries `selections`, `refreshed_selections`, `by_scope`,
`fallback_reasons`, and `selections_without_fallback_reason`, counted over the
selections belonging to included runs.

`plans` is the complete list, one entry per plan aggregated across every
invocation it resumed through, each carrying `plan_key`, `plan_dir`, `runs`,
`runs_with_known_wall`, `wall_seconds`, `attributed_seconds`, `seconds`,
`attempts`, `additional_attempts`, `verification_checks`,
`verification_check_seconds`, and `by_classification`. Ordering is descending
`wall_seconds`, then descending `attributed_seconds`, then `plan_key`, so the
text report's top ten is the head of this list. Because `execution_timing` is
windowed by run finish time and a run contributes its whole duration,
`phases.wall_seconds` may exceed the window itself; narrowing `--since-days`
still never raises a count.
