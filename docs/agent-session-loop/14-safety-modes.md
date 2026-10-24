## Safety Modes

By default, `run` and `doctor` use harness-specific bypass modes for every
harness. For Codex, Claude, and Copilot this means:

```text
codex app-server  (thread/start sandbox: danger-full-access)
claude --print --verbose --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode bypassPermissions <prompt>
copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools --allow-all-paths --no-ask-user --silent --acp  (session/prompt carries <prompt>)
```

`--unsafe-bypass` is accepted as an explicit form of the default. Pass
`--no-unsafe-bypass` to use the harness default sandbox and permission mode
where supported by that harness:

```text
codex app-server  (thread/start sandbox: workspace-write, writable root <plan-dir>)
claude --print --verbose --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode dontAsk <prompt>
copilot -C <cwd> --add-dir <plan-dir> --no-ask-user --silent --acp  (session/prompt carries <prompt>)
```

For Copilot, `--no-unsafe-bypass` does not re-enable interactive approval prompts; it keeps `--no-ask-user --silent` and only removes `--allow-all-tools --allow-all-paths`. Over ACP it also decides the one thing the permission responder may answer: with the bypass flags a `session/request_permission` is answered `allow_once`, and without them it is answered `reject_once`.
Because bypass mode is the default, run only trusted plans against trusted
repositories, or pass `--no-unsafe-bypass` when the harness should stay in its
normal workspace sandbox.

The runner itself never marks sessions complete. Durable completion is only the
`- **Status**: complete` line inside the final `## Handoff` block of each
`session-<id>.md` file. Operational state can be deleted and rebuilt from the
plan files. After a successful turn, the runner may update recognized status
tables in `manifest.md` and `context.md`, but those tables are derived
bookkeeping; handoff statuses remain authoritative.

### Process sweep

Every harness turn and every verification command check runs in a session of
its own (`start_new_session=True`), so each owns a process group. However that
turn or check ends, the runner then sweeps it: one read of the process table,
then `SIGKILL` for every process still in the group and for every process
recorded in a snapshot of the root's descendants taken while the root was still
alive, provided it is still running under an identical command string *and* an
identical start time. That re-verification is what makes a snapshot safe to act
on after the fact: a pid reused by an unrelated process no longer matches on
command, and one reused by a process running the same command is rejected on
its start time (reported as `snapshot_pid_reused`). The start time comes from
`ps -o lstart=`; where a platform's `ps` does not report one, entries carry no
start time and command re-verification stands alone. Pid reuse also decides the
other route: the caller reaps the root before sweeping, so a root pid that is
still in the table now belongs to something else and the group under that number
is not the turn's — enumeration is skipped (`pgid_skipped: root_pid_reused`)
rather than aimed at a stranger's children. The root pid, the
runner itself, and pids <= 1 are never signalled. The sweep is best-effort
cleanup of something already finished, so an unreadable process table or a kill
that raced a reap degrades to a report rather than an error and can never fail
a turn or a check. It exists for two observed failures: a leftover dev server
from an earlier check failing a later one, and a descendant that called `setsid`
while holding the captured pipes open, which stalled a run for over two hours.

The sweep is **on by default**. Set `ASL_PROCESS_SWEEP_DISABLED=1` (or `true`,
`yes`, `on`) to turn it off; any other value, including a typo, leaves it
enabled, so a mistyped opt-out never silently disables cleanup. With the sweep
off, no process table is read and no report is produced.

The three identification routes cover different escapes, and the paths differ in
which routes they have:

- **Process group members.** Positive identification, since a group is only
  entered by inheritance from the root. This is the only route available after
  a turn or check that exited on its own, because there is no kill to snapshot
  before.
- **A pre-kill descendant snapshot.** A group kill reparents a `setsid`
  descendant to pid 1, after which no descendant walk can reach it, so the
  snapshot only works if it was taken while the root was still alive. The
  verification timeout, cancellation, and interrupt paths (the last covering a
  Ctrl-C that lands on the thread running a sequential recipe) and the harness
  turn timeout each take one; for a silent harness turn, the process tree the
  hang sentinel samples while diagnosing it serves as the same evidence, and
  that turn's sweep consumes it so a later turn handed the same root pid cannot
  reuse a sample of a tree that is gone. The auto-commit subprocess backstop
  takes one too, and there the snapshot is not a corner case but the main
  route: the nested `asl commit` starts its message harness in a session of its
  own, so the group kill aimed at the subprocess reaches the interpreter and
  nothing under it.
- **Live descendants of the above** (reported as `late_descendants`). A
  snapshot names only what was running at the instant it was taken, so a child
  forked afterwards appears in no snapshot — and if it too called `setsid`, in
  no process group either. Its parent link survives until that parent is
  signalled, and the walk runs against the same pre-kill table read, so the
  link is still there to follow. Nothing is re-verified here because nothing
  can be forged: ancestry in the live table *is* the identification, and the
  walk is only ever rooted at a process already identified by one of the two
  routes above, so a reused pid never becomes a root to walk from. The walk
  stops at the root pid and at the runner itself rather than descending
  through either.

The honest limit: a descendant that left the process group **and** whose parent
chain was already dead when the table was read is identified by none of the three
routes and survives the sweep, as does one forked after that read. In practice
that is a `setsid` descendant of a turn or check that exited normally while
nothing had sampled its tree. It can no longer stall
the run — the pipes it holds are no longer waited on, under the reader-join
bounds above — but it is not killed. Work that must not outlive the turn or
check that started it therefore must not be detached into a session of its own.

A sweep that killed something, or failed to, appends one `process_sweep` event
per turn and per swept check. Its payload carries `phase` (`turn` or
`verification`), the run/turn/session identity, the `check` name for a
verification sweep, and the report: `killed`, `kill_failed`, `survivors_after`,
and an `enumeration` block with `expected_pgid`, `table_size`, `pgid_members`,
`snapshot_size`, `snapshot_pid_reused`, `snapshot_verified`, `late_descendants`,
`already_gone`, and
`resampled`. A sweep
that found nothing is the normal case and is not recorded, so a `process_sweep`
event always means real cleanup happened. The auto-commit backstop's sweep is
the exception to the event: it is not a turn or a check, so its report is
carried on the commit result payload under the same `process_sweep` key —
present whenever the outer bound fired and the sweep was enabled, whether or
not it found anything — rather than appended to the event stream.

### CLI invocation trace

Every real `asl` process — the installed wrapper and `python -m
agent_session_loop` alike — appends two records to
`cli-invocations-YYYY-MM-DD.jsonl` at the effective state root: a `start` event
once the command line has been recognized, and an `end` event on whatever
happens next (normal exit, nonzero exit, `Ctrl-C`, or an uncaught exception).
Only `cli.main` is instrumented, so importing `agent_session_loop` as a library
and calling a handler directly records nothing.

A record carries only vocabulary the parser already owns plus process facts:
`schema_version`, `event`, `invocation_id`, `timestamp`, `pid`, `command_path`
(recognized subcommand names), `options` (recognized option names), `help_depth`,
`harness`, `origin`, and — on the `end` record — `exit_code`, `outcome`,
`duration_ms`, and `exception_type`. Raw argv, option values, positional values,
unknown tokens, paths, environment values, message bodies, captured output, and
exception messages are never assembled into a record, so a trace file cannot
leak content. An option value is counted and skipped rather than inspected,
which is why `--state-dir metrics` cannot promote `metrics` into the command
path.

`outcome` is one of `ok`, `failed`, `help`, `usage`, `interrupted`, or
`exception`. `help_depth` is `root`, `group`, or `leaf` on a `--help` exit and
null otherwise. `origin` comes from the internal `ASL_INVOCATION_ORIGIN`
allowlist (`direct`, `autopilot`, `workbench`, `install-verify`); any other
value is recorded as `other`, and an unset variable means `direct`.

Every launcher that spawns an `asl` child marks it, so a supervisor reading
`asl metrics` can tell their own calls apart from internal automation. The
autopilot helper's central child environment marks its `asl run`, `asl status`,
and `asl questions` subprocesses as `autopilot`; `workbench_launch.launch_plan` marks
its `python -m agent_session_loop run` child as `workbench`; and the install
verifier marks its `asl status --json` smoke call as `install-verify`. Anything
left is `direct`. The marker is always applied **after** the launcher's other
environment work — after the autopilot helper overlays the supervised command's
own `NAME=VALUE` assignments, and after a Workbench caller's `env` mapping —
because attribution belongs to the process doing the launching. A supervised
command that sets `ASL_INVOCATION_ORIGIN=direct` therefore cannot disguise
automation as an operator call, and every other caller-provided entry is
preserved untouched. A Workbench launch that passes no `env` at all still
materializes the inherited environment so the marker reaches the child.

A successful parse traces under its effective `--state-dir`. A `--help` or
argparse usage exit happens before a namespace exists, so its pair lands under
the default/XDG state root instead.

Each record is one locked, mode-`0600`, append-only write; files are daily and
pruned opportunistically once they are more than 30 days old. Tracing is
fail-open in every direction — an unwritable state root, a full disk, or a lock
that cannot be taken produces no record rather than a failed command, and the
reader skips malformed lines instead of rejecting the file, counting them so
that corruption is reported rather than hidden. A killed process leaves an
unmatched `start`, which readers tolerate.

`asl metrics` is the consumer: its `cli_invocations` section pairs the two
records by `invocation_id` and reports the lifecycle. See
[the `metrics` command](04-live-reply-wait-and-fallback.md#command-behavior) and
[`metrics --json`](16-json-output.md#json-output).

Tracing is **on by default**. Set `ASL_CLI_TRACE=0` (or `false`, `no`, `off`) to
turn it off; any other value leaves it enabled. The test suite disables it
package-wide so the suite never writes into a developer's live state root.

## State Files

By default, runtime metadata is stored under:

```text
~/.local/state/agent-session-loop/<plan-path-hash>/
```

`--state-dir DIR` changes the state root; the plan hash subdirectory is still
created below that root. The hash is based on the canonical plan directory path,
so logs and locks remain stable as session files change. `asl sessions` scans
every per-plan-hash subdirectory under the resolved state root, classifying
`lock.json` and `state.json` for running rows and walking every
`runs/<run_id>/turn-*/result.json` artifact for recent rows. Recent rows come
from the immutable artifact tree, so the same plan's repeated runs are visible
rather than collapsed by whichever `state.json` was overwritten last; running
rows come from the live lock plus the latest `state.json` `active_turn`.

`state.json` carries schema version 3: a top-level `version: 3` plus a nested
`agent_mux` projection. Turn `result.json` files and `events.jsonl` envelopes
stay at `version: 2`, and readers treat a missing `version` key as v1 and keep
accepting those records. The persisted `state.json` is a compact projection —
per-turn `before`/`after` session lists and raw verification check
stdout/stderr are dropped (bounded previews, byte counts, sha256 digests, and
gzip artifact paths remain) — while each turn's `result.json` keeps full
per-turn detail. No state or event record embeds raw verification output; full
failed-check output lives in the referenced `verification/*.gz` artifacts. A
record that carries no projection is stamped `version: 2` rather than 3, which
is what `asl compact-state` writes when it rewrites a legacy file: the version names
what the record actually contains.

The `agent_mux` block is the durable half of the AgentMux integration — the
same canonical projection the lifecycle hook sends, written into the one atomic
`state.json` replacement that every ASL state transition already performs, so a
reader that has the state file has the projection at that exact transition. It
carries its own `schema_version`, the run and plan identity, the derived phase
(`starting`, `turn`, `verification`, `between`, `waiting`, `paused`, or
`finished`), turn index, session ID, harness, the awaited question's *ID*, and a
`process` birth identity (`pid` plus the kernel's start token, which survives
pid reuse) that is present for live phases and `null` for `paused` and
`finished`. It carries no question body, no prompt, no verification output, and
no raw bridge diagnostics: the projection says where a run is, and the artifacts
it names say what happened.

Two counters advance independently and neither is derived from the other.
`agent_mux_projection_generation` counts durable state transitions and is
allocated inside the same lock that serializes the payload mutation and the
write, so concurrent writers produce a gap-free monotonic sequence.
`agent_mux_sequence` is the lifecycle *notification* source sequence and
advances per hook emission, which is a different event than a state write.

`cli-invocations-YYYY-MM-DD.jsonl` sits at the state root itself, beside the
per-plan-hash directories, because a CLI invocation is not scoped to a plan. It
is retained for 30 days and pruned opportunistically; `asl metrics` reports that
root and retention as `cli_invocations.trace_root` and
`cli_invocations.retention_days` so an empty section is distinguishable from an
absent trace. `asl gc` never touches these files — its sweep only removes
`runs/<run-id>/` directories under a plan hash. See
[CLI invocation trace](#cli-invocation-trace).

The remaining runtime files live under a plan's hash directory:

- `state.json`: latest run payload.
- `events.jsonl`: append-only run, pause, turn, and commit events.
- `lock.json`: active per-plan runner lock.
- `redirect-journal.jsonl`: optional 4 MiB-capped redirect request journal;
  physical lines are durable sequences, the last 512 KiB are reserved for
  settlement records, and the file is never truncated in v1.
- `mailbox.jsonl`: optional 4 MiB-capped mailbox log; physical lines are durable
  sequences and the file is never truncated in v1.
- `mailbox-lease.json`: optional fixed-size supervisor activity lease, fresh for
  90 seconds.
- `mailbox-supervision-<supervision-id>.json`: optional resumable supervision
  cursor, one per supervising launch, pruned after seven days.
- `conversations.jsonl`: optional 4 MiB-capped conversation ledger; physical
  lines are durable sequences and the file is never truncated.
- `conversations.lock`: the exclusive-append lock for that ledger; writers never
  lock the data file itself.
- `runs/<run_id>/`: immutable artifacts for one runner invocation.
- `runs/<run_id>/turn-0001-session-<id>/stdout.log`: stdout from the harness
  turn.
- `runs/<run_id>/turn-0001-session-<id>/stderr.log`: stderr from the harness
  turn.
- `runs/<run_id>/turn-0001-session-<id>/last-message.txt`: final harness
  message when available.
- `runs/<run_id>/turn-0001-session-<id>/result.json`: diagnostic result for the
  turn, including argv, status changes, runner signals, snapshots, and artifact
  paths.
- `runs/<run_id>/commit-0001-session-<id>/stdout.log`: stdout from an automatic
  per-session `asl commit --yes --no-verify` invocation.
- `runs/<run_id>/commit-0001-session-<id>/stderr.log`: stderr from that commit.
- `runs/<run_id>/commit-0001-session-<id>/result.json`: diagnostic commit
  result, including argv, cwd, return code, HEAD before/after, status result,
  commit SHA when one was created, and artifact paths.
- `runs/<run_id>/supervisor-audit-input.json`: the bounded operational evidence
  bundle the audit read, including the run's slice of the plan's `mailbox.jsonl`
  with every message body replaced by its digest.
- `runs/<run_id>/supervisor-audit.json`: the validated audit record.
- `runs/<run_id>/supervisor-audit.md`: the operator-readable audit report.
- `runs/<run_id>/supervisor-audit-attempt-N/`: per-attempt prompt, returned
  message, and outcome.
- `runs/<run_id>/supervisor-audit-staging/`: where both reports are written
  before they are promoted to the two names above. It is removed once the
  publication settles either way, and anything an interrupted attempt left in it
  is cleared by the next one, since no durable record ever names a path inside
  it.
- `runs/<run_id>/assurance-pending.json`: the audit debt an abandoned run left
  for `asl assurance process-pending`.
- `runs/<run_id>/commit-0001-end/...`: equivalent artifacts for
  `--commit-at-end`.

Three assurance files sit one level up, directly under the state root rather
than inside any plan's directory, because the inbox spans the whole corpus:

- `assurance-inbox.jsonl`: the append-only assurance ledger. New records are
  written at schema v3, which adds the keep-distinct ruling to the v2 that added
  the identity binding and changes nothing else, so every older line already on
  disk still means exactly what it meant when it was written. All three versions
  are read; a line claiming a version this build does not know, or a kind its own
  version predates, is preserved in place as rejected history rather than guessed
  at.
- `assurance-inbox.lock`: the exclusive-append lock; writers never lock the
  data file itself.
- `assurance-index.json`: a disposable cache, rebuilt whenever it is missing,
  stale, malformed, or of an unknown version.
