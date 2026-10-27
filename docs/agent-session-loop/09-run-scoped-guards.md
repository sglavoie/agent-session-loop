<!-- Continuation of the previous part. -->

The four queued subcommands — `stop-after-current`, `abort`, `select-harness`,
and `set-model` — accept `--wait` and `--timeout SECONDS`, which turn the
fire-and-forget append into a receipt. Without `--wait`, nothing changes: the
command is appended, a best-effort wakeup is sent, and the handler returns 0
whether or not a runner ever sees it. With `--wait` the handler tails
`events.jsonl` every `0.5` seconds for the `control-command-observed`
run-control record carrying the `command_id` it just enqueued. The runner
appends that receipt for *every* command it accepts, including one that changes
no state, so a duplicate or already-applied command is distinguishable from a
lost one. On success the payload gains a `wait` object reporting `observed`,
`terminal_action`, `run_finished`, `timeout_seconds`, and `elapsed_seconds`.
`asl control abort --wait` additionally waits past the receipt for the real
outcome — the `abort-turn-honored` or `abort-turn-satisfied-by-worker` action
recorded after it, reported as `wait.terminal_action` — and then past that
outcome for the `run-finished` record, reported as `wait.run_finished`. Only
`abort-turn-honored` means the run is stopping; `abort-turn-satisfied-by-worker`
reports that the turn ended by itself inside the window, which leaves the run
paused or still running, so read the run's own terminal state before calling it
stopped. A successful `abort --wait` therefore means the run is over and the
plan lock is released, not merely that the turn was killed.

A wait that runs out exits 1 and names which observation failed, with the
`command_id`, `queue_path`, `signal`, and `wait` evidence under `details`: no
runner is listening (no active lock, or the wakeup reported `no-runner`) fails
with `reason: no-runner`; a runner that holds the lock but did not drain in time
fails with `reason: receipt-timeout`; an abort whose receipt landed but whose
outcome has not been recorded yet fails with `reason: abort-timeout`; and an
abort whose outcome landed while the run is still finalizing fails with
`reason: finalization-timeout`, carrying `wait.finalizing` (the `phase`,
`elapsed_seconds`, and `cancellable` fields of the run's `active_finalization`)
and a `hint` naming the second abort that cancels it. That last reason is not a
hang: it says the turn is gone and the terminal audit is still running. The
command remains durably queued in every case — the timeout is about the
receipt, never the append. `--timeout` requires `--wait` and must be between 0
and 1800 seconds; otherwise the command fails with `reason: invalid-timeout`
before anything is enqueued. The default wait is `60` seconds, which is shorter
than the default abort grace window, so `abort --wait` usually wants an explicit
`--timeout` above the grace it requested.

When a runner observes a queued abort while a turn is in flight, its poll thread
records the `abort-turn-requested` action, stamps the resulting
`control.abort_deadline`, and — when the mailbox protocol is active — appends a
guidance record naming the reason, the grace window, and the deadline, so a
worker can wind down and pause the run itself. From there an abort has three
distinct outcomes, and only the first of them stops the run:

- **Killed at the deadline.** The turn was still running when the window
  expired, so the harness process group is killed, the session returns to
  `pending` with a Handoff note explaining that it holds no completion,
  `abort-turn-honored` is recorded, and the run exits with `reason: stopped`.
  This outcome alone is the clean early stop: it never pauses the run and reuses
  no pause reason.
- **Self-paused inside the window.** The worker wound down and emitted
  `RUNNER_PAUSE`, so `abort-turn-satisfied-by-worker` is recorded, nothing is
  killed, and the worker's own pause owns the outcome — the run exits `paused`
  with the worker's pause reason and `ok: false`, never `reason: stopped`, and
  the session keeps whatever status the worker left it with and carries no abort
  Handoff note.
- **Completed inside the window.** The worker finished its session instead of
  winding down, so `abort-turn-satisfied-by-worker` is recorded, the completion
  stands, and the run continues to the next session. Recording that action
  consumes the abort, so stopping the run afterwards needs a fresh
  `asl control` request.

An abort observed between turns has nothing to kill and degrades to
stop-after-current instead. It is recorded as `abort-turn-degraded` rather than
`abort-turn-honored`, and `abort --wait` reports that as its
`wait.terminal_action` once the run finishes at the session boundary.

Killing the turn is not the end of the run. Once the last turn is over the
runner still owes the plan its terminal supervisor audit, which is an ordinary
harness turn with its own answer and delivery budgets and can legitimately take
tens of minutes. That work is published as a phase rather than left to look like
a stall: `runner_state.active_finalization` names the `phase`
(`assurance-audit`), when it started, how long it has been running, and whether
it is `cancellable`, `asl status` prints the same as a `Run state: finalizing`
line, and `finalization_started` / `finalization_heartbeat` /
`finalization_finished` events carry it into `events.jsonl` on a `30`-second
heartbeat. The run still holds the plan lock throughout, and it still drains the
control queue.

Repeating the request cancels it. An abort observed while the run is finalizing
arms immediately with no grace window — there is no session to wind down and no
worker to warn, only an advisory read-only audit — so the audit's harness is
killed, `finalization-abort-honored` is recorded, and the run finishes within
seconds. Cancelling the audit costs the audit and nothing else: assurance is
recorded as `unavailable` with `reason: interrupted`, and the run's own outcome,
reason, and exit code are whatever the stop already made them.

### Run-scoped guards

A supervisor who decides mid-run that a worker must not run a particular
command, or must not touch a particular file, has without this only one lever:
telling it so. That works when the worker cooperates and fails silently when it
does not, and it has to be restated for every harness. `asl control guard` is
the durable version of the same intention, and it does not depend on the model
reading anything.

```
asl control guard add PLAN_DIR_OR_ID [--deny-command "CMD ARG ..."] [--protect-path ALIAS:PATH] [--reason TEXT] [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control guard remove PLAN_DIR_OR_ID --id GUARD_ID [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control guard list PLAN_DIR_OR_ID [--state-dir DIR] [--json]
```

Both flags are repeatable and may be combined in one `add`; each rule becomes
its own guard with its own ID, so they are removed independently. An `add`
carrying neither is refused with `empty-guard-rule`. `asl control guard` is the
**only** supported mutation surface for guards: the Workbench and the bridge
expose no guard-mutation endpoint, deliberately, because a guard is an operator
instruction and the CLI is where an operator already proves which run they mean.

#### Choosing a lever

The four instruments overlap enough to be confused for one another, and
choosing wrong is how a run either drifts or stalls:

- **Guidance** (`asl inbox guidance`) for the *direction* of the work — what to
  implement, what approach to take, what to prioritize. It is delivered to the
  model and its effect depends entirely on the model acting on it. Nothing
  enforces it; see [Guidance delivery](03-live-mailbox-and-future-turn-model-control.md#guidance-delivery) for what delivery
  does and does not promise.
- **A command guard** (`--deny-command`) for an *action* that must not happen —
  `git push`, a deploy script, a destructive migration. Use it when naming the
  command is the whole instruction.
- **A protected-path guard** (`--protect-path`) for *files that must end the
  turn as they started* — a config the worker keeps "fixing", a lockfile, a
  neighbouring session's plan file. Use it when what matters is the file's final
  state rather than the route taken to it.
- **Abort** (`asl control abort`) only when the *active turn itself* must be
  terminated. It is the loudest instrument and it discards work in flight;
  neither guard kind cancels a running turn, by design.

#### Command rules

A **command rule** is an argv prefix, tokenized with POSIX `shlex`
rules and matched **case-sensitively and exactly** against the front of an
invocation's argv. `git push origin main` denies `git push origin main` and
`git push origin main --force`; it does not deny `git push`, `git Push origin
main`, or `git status`. A one-token rule such as `git` denies every invocation
of it.

Rules describe a command, never a shell fragment, and every other construct is
refused before anything is enqueued:

- `missing-guard-rule` — `add` was given no rule at all.
- `empty-guard-rule` — the same refusal seen from the parser: neither a
  `--deny-command` nor a `--protect-path` was supplied.
- `empty-command-rule` — a rule that tokenizes to nothing.
- `unparsable-command-rule` — a rule `shlex` cannot tokenize (unbalanced quote).
- `empty-token` — a rule containing an empty argument.
- `executable-not-bare-name` — the first token is a path (`/usr/bin/git`,
  `./git`). A rule names a command to be resolved on `PATH`, because that
  resolution is precisely what the gate intercepts.
- `shell-operator-in-rule` — the rule contains `|`, `&`, `;`, `<`, `>`, `(`,
  `)`, a newline, or a `$` expansion.
- `glob-in-rule` — the rule contains `*`, `?`, or `[`/`]`. Matching is exact; a
  pattern that looks like it would match more would deny less than it reads.
- `environment-assignment-in-rule` — a leading `NAME=VALUE` token.

#### Protected-path rules

A **protected-path rule** is written `Alias:glob` and names paths that must end
each turn byte-identical to how that turn found them. The qualifier is a
repository alias the *active plan* declares: a registered plan requires one of
its own aliases (`api:config/settings.toml`), and a legacy single-repository
plan requires the literal `.` (`.:notes.md`). The `.` qualifier names the
repository's Git top level, not the directory the run was started in, so a run
started in `repo/sub` protects `repo/sub/notes.md` as `.:sub/notes.md`.
Patterns are relative POSIX paths
matched **case-sensitively** against normalized paths, and support literal
segments, `*` and `?` within one path segment, and `**` across segments —
`docs/**/*.md` matches `docs/guide.md` and `docs/deep/nested/guide.md` alike.

Path rules are validated against the plan before anything is enqueued:

- `empty-path-rule` — a rule with no pattern.
- `path-missing-qualifier` — no `Alias:` prefix. A bare path would be ambiguous
  in a registered workspace and silently wrong in exactly the case that matters.
- `path-unknown-alias` — a qualifier the active plan does not declare.
- `absolute-path-in-rule` — an absolute pattern. Guards are workspace-relative;
  an absolute path would name something the snapshot may not even cover.
- `backslash-in-path-rule` — a `\` separator or escape.
- `parent-traversal-in-path-rule` — a `..` segment.
- `empty-segment-in-path-rule` — a `//` or a leading/trailing separator.
- `unsupported-path-pattern` — syntax beyond `*`, `?`, and `**`, including a
  `**` sharing a segment with other characters.
- `protected-path-evidence-unavailable` — the run has an active turn but no
  turn-start snapshot to compare against, so the guard is refused rather than
  armed blind. Between turns the guard is accepted and binds the *next* turn's
  baseline.

Enforcement is a **comparison against the turn-start workspace snapshot** the
runner already captures — there is deliberately no activation-time baseline. The
consequence is the intended reading of "this file must not change": a guard
added mid-turn convicts a change the turn had already made before the guard
existed, because an operator who sees a file they care about and freezes it is
asking about its state now, not about the moment their command was drained. The
symmetric consequence is that a path changed and then restored to its exact
turn-start content passes, because the comparison is of state, not of activity.
Changes an operator made before the turn began are part of the baseline and are
never convicted.

The check runs at finalization, after the general workspace write guard and
before every result and commit branch, so no violating turn is ever published or
committed. On a violation the run records the guard IDs, the matched qualified
paths, the write guard's own before/after evidence for the repositories holding
them, and each guard's enforcement coverage; emits a turn-rejected event;
returns the session to `pending`; and pauses with
`guard-protected-path-changed`. When the turn produced no comparison the guard
can read, it pauses with `guard-protected-path-unavailable` instead — a guard
that cannot see is not a guard that saw nothing. Both are inspect-and-repair
pauses rather than mailbox-reply pauses: there is no answer a worker could give
that would resolve them, so resume after restoring the path or removing the
guard.

A turn can breach more than one guard at once, and it then reports more than
one. When another finalization guard has already paused the turn — a write
scope violation, an unavailable write guard, a review mutation — the
protected-path step still runs, against the same turn-start comparison the
earlier guard captured, and still records its verdict. The earlier pause is the
one left standing: its reason and remediation are what the operator acts on,
and the session is not withdrawn twice. The protected-path verdict rides beside
it, marked `adjudicated_only: true` with `deferred_to_pause_reason` naming the
pause that won, in `result.json`, in the pause details, and in its own
turn-rejected event. An operator who repairs only the scope violation therefore
still learns the frozen path moved, instead of discovering it on the rerun.

The same holds when the harness raises. The exception, not a guard, ends that
turn, and it is re-raised unchanged — but the adjudication runs during cleanup,
while the run's guards are still alive, so a crash is not a way for a protected
path to move unremarked. A cleanup that cannot read a comparison at all records
the `guard-protected-path-unavailable` verdict rather than a clearance.

That holds for every turn shape, including a legacy generated review. A review
turn on an unregistered plan captures no write-guard snapshot, so its turn-start
comparison is the review fingerprint the mutation guard would have used. The
crash path recaptures that fingerprint and adjudicates against it: an unchanged
worktree clears every path rule at once, a changed one names the paths it moved,
and a recapture that fails or returns unusable evidence records
`guard-protected-path-unavailable`. The exception is still re-raised unchanged
and the filesystem changes are still retained.

**Filesystem changes are always retained.** A violation rejects the turn's
*result*; it never rolls back, reverts, aborts, or restarts anything. The run
cannot know whether the change was the valuable part of the turn, and destroying
an agent's work to satisfy a guard would cost more than it protects.

Retention answers where the work is *not* going, not where it is kept, and
under an active commit mode a *recoverable* pause now writes it down. The four
recoverable reasons — `verify-failed`, `transport-failed`, `timeout`, and
`no-status-change` — settle the paused turn's retained changes into a
`session-paused` commit before the process exits, so the relaunch meets a clean
worktree at its startup commit gate and repairs the session without a human.
A guard refusal is the opposite case and keeps the opposite treatment: a
write-scope violation or a protected-path change is not recoverable, the dirt
it retained is the evidence the gate exists to preserve, and it is left
uncommitted for the inspection that pause is already waiting on. Nothing rolls
back on either path, so the invariant itself is unchanged — the settlement only
says what a recoverable pause does with the bytes it kept.

Guards belong to one run and never outlive it. `add` and `remove` require an
active run and fail with `reason: no-active-run` otherwise, before appending
anything — a guard queued for a run that does not exist would either enforce
nothing or, worse, be inherited by an unrelated later run. Each queued mutation
also carries the run ID the operator saw, and the runner refuses it with
`run-changed` if the run turned over before the drain. A `remove` naming a
guard the run does not hold fails with `unknown-guard`. A rule whose executable
does not resolve on `PATH` fails with `executable-not-found`, since a shim for
it would shadow nothing while breaking the worker's own resolution, and a gate
the runner cannot materialize fails with `activation-failed`. A malformed
queue line — one carrying no `expected_run_id`, no rule, or no guard ID —
is rejected by the queue itself with `missing-expected-run-id`,
`missing-guard-rule`, or `missing-guard-id`; `invalid-guard-id` and
`invalid-reason` cover the wrong types.

#### The command gate

A command rule is enforced by a **per-run `PATH` shim**. Before every worker turn the runner
creates an empty `guard-bin` directory under the run's state directory and
prepends it to the turn's `PATH`, whichever harness the turn uses. Applying a
guard resolves the real executable *outside* `guard-bin` once, writes the
derived policy atomically, installs a small dispatcher into `guard-bin` under
the command's name, and only then records the guard and appends
`guard-applied`. A shim that is reached with a non-matching argv `execv`s the
real executable it recorded at activation; one reached with a matching argv
prints two stable lines to stderr and exits **`126`**:

```
asl: command denied by run-scoped guard g-4f2a91c07be3
asl: matched argv prefix: git push origin main
```

`remove` rewrites the policy atomically before reporting success, and the run's
termination expires every guard by deleting the policy. Shims are deliberately
left behind in both cases: with no policy they are plain delegators, so they
are inert evidence of what the run enforced rather than something that has to
be cleaned up correctly for the next turn to work.

`--wait` on `add` and `remove` waits for the guard's own outcome —
`guard-applied`, `guard-removed`, or `guard-rejected` carrying the enqueued
`command_id` — not merely for the queue observation, because the observation
says the runner read the command and nothing about whether the command is being
enforced. A `guard-rejected` outcome exits 1 and reports the runner's reason.

#### Enforcement lanes and coverage

Guards are enforced in up to three lanes, and every guard reports its own status
in each so an operator can tell an enforced instruction from a hopeful one:

- `command-gate` — the `PATH` shim above. Applies to command rules only; a path
  rule reports `unsupported` with `not-a-command-rule`.
- `pretool` — the harness's own pre-tool hook, which denies the call *before* it
  runs and returns the guard's reason to the model, so the worker can do
  something else instead of finishing a turn that is then thrown away.
- `postflight` — the turn-start snapshot comparison above. Applies to path rules
  only; a command rule reports `unsupported` with
  `command-execution-leaves-no-postflight-evidence`, because running a command
  leaves no trace a workspace comparison could convict.

Each lane reports `active`, `degraded`, or `unsupported` with a stable reason:

- `path-shim-armed` / `path-shim-not-armed` — whether the command gate
  materialized for this guard.
- `guard-overlay-not-armed` — the overlay could not be armed for the turn in
  flight, so the worker was spawned with an untouched `PATH` and no policy path
  in its environment. Preparing the overlay fails open — an unwritable run
  directory must not cost the turn — but every lane that reads it is blind
  until a later turn is prepared successfully, so the `command-gate` lane and
  any `pretool` lane that would otherwise be armed report this instead. The
  guard stays in the active set and is re-armed by the next turn.
- `hook-denies-before-execution` — the pretool hook is armed and covers this
  rule.
- `harness-exposes-no-pre-tool-hook` — the harness has no such lane at all.
- `codex-hook-trust-requires-user-config-mutation` — Codex hooks require a
  trusted `CODEX_HOME` configuration that `codex app-server` will not accept
  from an ephemeral overlay, and ASL does not mutate an operator's own config to
  obtain it. Codex therefore has no `pretool` lane.
- `copilot-deny-tool-fixed-at-process-start` — Copilot's only pre-execution lane
  is its command line, so the rules are compiled once into `--deny-tool` when
  the process starts. `degraded`, not `active`: a guard added later in the same
  turn cannot reach it.
- `guard-added-after-process-start` — exactly that case: the guard did not exist
  when Copilot was launched, so it is `postflight`-only for this turn.
- `copilot-deny-tool-cannot-express-paths` — Copilot's `--deny-tool` states shell
  commands, not paths, so a path rule has no Copilot pretool lane.
- `copilot-deny-tool-cannot-express-rule` — a command rule containing whitespace
  or parentheses that `--deny-tool` has no syntax for. It compiles to nothing
  rather than to something narrower than it reads.
- `turn-start-snapshot-comparison` — the postflight lane for a path rule.
- `not-a-command-rule` / `not-a-path-rule` — the lane does not apply to this kind
  of guard.

The pretool hook is armed through the ASL-owned hook overlay for the turn, and
is a *narrowing* of enforcement rather than a replacement for it. It denies a
write tool call naming a protected path, and a shell tool call whose command is
one unambiguous simple invocation matching a command rule — including
`/usr/bin/git push`, the absolute-path bypass the `PATH` shim cannot see,
because the basename is right there in the tool call. It deliberately does **not**
claim to predict what an arbitrary shell script will do: anything containing a
shell operator is passed through untouched and left to the shim and the snapshot
comparison. Reads of a protected path are never denied — a guard freezes
content, not attention. Every failure inside the hook allows the call, because
the guarantees live in the other two lanes and a hook that blocked the worker
whenever it failed to read a file would convert its own bugs into stalled runs.

`asl control guard list` and `asl status` both report the active set and the
per-guard lanes; status prints a `Run guards:` block naming each rule and its
armed lanes, and its JSON carries `command_guards` with `active`, `count`,
`coverage`, and `protected_path_coverage`. A run that has finished reports no
guards, because it is enforcing none.

Every turn a path guard was active for records a `protected_path_guard` block in
its `result.json`, on the clearing path as well as the convicting one: `passed`,
the `session_id`, the active `guards`, the `observed_paths` the turn changed, and
the per-guard `coverage`. A conviction adds `reason`, `guard_ids`,
`matched_paths`, the before/after `evidence`, `retained: true`,
`reverted_to_pending`, and a `detail` naming why the evidence was unusable when
that is the reason. A conviction reached beside an outcome that had already
ended the turn adds `adjudicated_only: true`, and `deferred_to_pause_reason`
when that outcome was a pause. A turn that passed a guard is therefore
evidenced, not merely unremarked.

The guarantee is **operational, not adversarial** — a safeguard against a
worker that would otherwise run the command or change the file, not a sandbox
against one determined to evade it, and not a security boundary against hostile
code. Denying a command does not prevent an equivalent operation performed by
another executable, by a library call, or by a raw network request. Nor does a
guard cancel anything: a turn that cannot have a guard activated is not aborted,
it is reported. Both halves of that boundary are stated here in the same words
`status` and `guard list` report them.

Covered by the command gate:

- a bare executable name run directly by the harness
- a bare name run through `env NAME=VALUE cmd ...`
- a bare name run through `sh -c` or any other shell
- a bare name a child process resolves through PATH, including Python's subprocess

Not covered — these are bypasses, by construction:

- an absolute or relative path to the real executable
- an equivalent library or network call that never spawns the command
- a process that clears or rewrites PATH before spawning
- a command that was already executing when the guard was applied

The first of those is additionally caught by the `pretool` lane on a harness
that has one, for a tool call the hook can read unambiguously. That narrows the
bypass on one harness; it does not close it, so it stays listed here.

Covered by a protected-path guard:

- any change to a matched path present at finalization, however it was made
- a change made earlier in the turn than the guard itself
- a change made by a process the harness never saw

Not covered:

- a matched path changed and restored to its exact turn-start content
- a change made after the run terminates
- predicting what an arbitrary shell script will write before it runs

`status` reads durable plan files and prints session counts, the next pending
session, content hashes, state paths, lock state, and any active harness turn.
Use `--json` for scripts. Its positional argument, `PLAN_DIR_OR_ID`, accepts
either an existing plan directory or the exact compact plan label printed after
`plan` by `asl sessions`. This compact plan label identifies a whole plan; it
is distinct from the dotted session identifier printed after `session` (for
example `session 2.1`), which names one session inside a plan and is not
accepted here. Existing absolute or relative directories always take
precedence, and only a bare, nonexistent argument (no directory component)
triggers a state-root lookup honoring `--state-dir`. Matching is exact and
case-sensitive against existing plan directories: zero matches fail with
`reason: plan-not-found`, while multiple matches fail with `reason:
ambiguous-plan`. Bare-label resolution is available to `status`, `asl control`,
and all eight mailbox commands. They are `asl inbox guidance`,
`asl inbox reply`, `asl inbox resolve`, `asl inbox read`, `asl ask`,
`asl notify`, `asl outbox wait`, and `asl outbox supervise`, plus
`asl inbox acknowledge` — filed under `asl inbox` but the worker's receipt in
the redirect journal rather than a mailbox command. `run`, `tail`, `unlock`,
and other plan-taking commands remain path-only. Successful
text and JSON output continue to report the canonical resolved `plan_dir`.

`tail` prints recent activity for the latest run and follows `events.jsonl`
plus live harness and commit stdout/stderr logs until the observed run
finishes. Recent activity places each log's lines among the run's events,
just before the event that finished the turn or commit that wrote it, so the
last `--lines` lines end on the run's latest events rather than on harness
output. Use `--no-follow` to print recent activity and exit. `tail` remains
the full event-and-log viewer; the `t` keyboard control embeds a logs-only
variant of the same follower inside a text TTY `run` so runner progress is the
single source for heartbeats and event lines.

`sessions` shows ASL-managed activity across every plan state directory under
the resolved state root. It scans each per-plan-hash directory for live
`lock.json` and `state.json` (running rows) and walks every
`runs/<run_id>/turn-*/result.json` artifact (recent rows). Output has two
sections: `Running sessions` lists currently active turns (or `loop`-kind
rows when a runner is alive between turns) with started time, elapsed, idle,
PID, harness, session ID, turn index, and plan; `Recent sessions` lists the
most recently finished turns with finished time, duration, status result,
harness, session ID, run ID, and plan. `--running` and `--recent` are
independent filters; passing neither (or both) shows both sections. `--limit`
must be a positive integer, defaults to `20`, and truncates recent rows only. Text
output uses compact plan-directory basenames; `--verbose` opts into full
canonical paths; JSON always includes full paths. The compact plan basename
shown after `plan` can be passed directly to `asl status` as its
`PLAN_DIR_OR_ID` argument; the dotted value after `session` remains a
session identifier within a plan and is not a `status` argument. A missing state root returns
success with empty arrays — only a catastrophic state-root error yields a
nonzero exit with `reason: state-error`. Recent rows are sourced from immutable turn artifacts,
so repeated runs for the same plan are not collapsed by the latest
`state.json`. Stale locks (dead PIDs, mismatched plan dirs, or invalid
shapes) and unreadable artifacts surface in dedicated `Stale locks` and
`Skipped artifacts` footer sections without aborting the scan.

`doctor` performs a non-mutating harness and path check. For registered plans
it resolves `--cwd` as the common workspace root and reports the ordered child
repositories in text and JSON. Use `--json` for scriptable prerequisite checks.

`unlock` removes a stale lock. It refuses to remove an active lock unless
`--force` is supplied. Use `--json` when automation needs the removed lock
state or an active-lock failure payload.

`compact-state` reclaims duplicated operational JSON by converting a plan's
older v1 `state.json`, `events.jsonl`, and per-turn `result.json` records to the
compact schema-v2 projection (the same shape new runs already persist). It
strips per-turn plan snapshots and raw verification output from state and
events, and for result records it extracts complete failed-check output into
recoverable gzip artifacts (readable through the bounded log API) while
discarding successful-check output after keeping a bounded preview. The default
is a dry-run that reports projected bytes reclaimed, files rewritten, and lossy
pass-output removal without writing; pass `--apply` to rewrite the records.
Reclaim figures are net of the gzip bytes those retained failure artifacts add
(reported separately as `artifact_bytes_added`), so a compaction that keeps a
poorly compressible failure artifact never reports more saving than is actually
reclaimed on disk.
An `events.jsonl` line that does not parse (a record torn by a crash
mid-append) is written back byte-for-byte rather than failing the plan, and
counted as `unparseable_event_lines`.
Every rewrite is atomic (temp-file + rename), so an interrupted compaction never
leaves a partially written JSON file, and re-running is idempotent. `--json`
emits the full `CompactionReport` payload.
