## Troubleshooting

Check what the runner sees:

```bash
asl status ~/.local/share/agent-plans/example-plan/ --json
```

Preview the next command without invoking the harness:

```bash
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --harness-model gpt-5 --review-harness claude --review-harness-model claude-opus-4 --dry-run --effort high
```

Inspect the most recent artifacts named in `state.json` or the JSON output:

```bash
jq '.turns[-1] | {stdout_path, stderr_path, last_message_path, result_path}' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json
jq -r '.turns[-1].artifact_dir' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json
jq -r '.turns[-1].result_path' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json | xargs jq '{session, session_id, returncode, status_changes, runner_pause, plan_error}'
```

Follow the active or latest run:

```bash
asl tail ~/.local/share/agent-plans/example-plan/
asl tail ~/.local/share/agent-plans/example-plan/ --lines 80 --no-follow
```

For text runs started from a real terminal, press `h` for the current keyboard
controls, press `s` for a compact status snapshot, press `t` to toggle inline
tailing, press `1`/`2`/`3` to select the future harness, and press `p` to
request or cancel stop-after-current. Use `tail` from another terminal to
inspect durable `run_control` events.

Remove a stale lock:

```bash
asl unlock ~/.local/share/agent-plans/example-plan/ --json
```

Remove an active lock only after verifying the owning process is not a real
runner you still need:

```bash
asl unlock ~/.local/share/agent-plans/example-plan/ --force --json
```

A lock whose recorded `hostname` is not this host reports
`lock was recorded on host <name>` and is the one stale lock `asl run` will not
take over: the local process table holds no entry for a remote pid, so liveness
there is unknowable rather than dead. Acquisition fails with a lock-contention
error naming that host; clear it with `asl unlock` (no `--force` — the lock is
stale) once you have confirmed the other runner is gone. A dead pid recorded on
*this* host needs no command at all: the next read of that lock reclaims it (see
[Stale unfinished `state.json` with no
lock](#stale-unfinished-statejson-with-no-lock)).

If a plan has no pending sessions but is not complete, inspect every final
`session-<id>.md` handoff and normalize statuses to exactly `pending` or
`complete`. Ignore older status templates preserved above the final handoff.

### Five-second plan transaction lock timeout

Atomic plan publication and recovery are serialized by a marker-scoped plan
transaction lock. Plan reads wait up to five seconds for the publication to
finish and then fail instead of hanging indefinitely. This timeout can surface
from plan reads behind `asl status`, `asl tail`, `asl prepare`, and the
Workbench API.

The diagnostic names the lock path and the pid, hostname, and thread of the
process that held the lock when it was acquired. Those holder fields identify
the publishing process and its host and thread for investigation; the kernel
lock remains authoritative. Retry the operation once if plan publication was
still in progress. If the timeout repeats, inspect the named holder before
retrying or restarting that process. Do not delete the transaction marker or
confuse this publication lock with the separately managed runner lock.

### Long verification after `session N/N finished`

The runner emits `session N/N finished` as soon as a harness turn writes a
`- **Status**: complete` handoff and the per-turn artifacts are flushed.
The independent post-turn verification gate then runs the session's
`## Completion Contract` `### Verify` recipe, which can take longer than the
harness turn itself (full unit suites, integration commands, large
`regex_in_file` scans, etc.). During that window the runner is **not** idle:
`state.json.active_verification` is populated with the in-flight check, and
`events.jsonl` carries `verification_started`, `verification_check_started`,
`verification_heartbeat` (every 30 seconds), `verification_check_finished`, and
finally `verification_finished` records. `asl status`, `asl tail`, and
`asl sessions --running` all surface this phase — `asl sessions --running` reports
the row with `kind="verification"` and a `check k/n NAME` suffix, and `asl tail`
prints the same heartbeats inline. Independent verification remains mandatory:
the harness handoff is only the implementation claim, the verify recipe is the
durable gate. Per-check stdout, stderr, and `failure_reason` enums remain
captured in the turn `result.json` under the `verification` key and in any
`asl verify --json` invocation.

### Stale unfinished `state.json` with no lock

When a runner process is killed (SIGKILL, host reboot, container OOM) without
running its cleanup path, `state.json` can be left without
`run_finished_at` while no `lock.json` is held. `asl status` flags this in two
places:

```bash
asl status ~/.local/share/agent-plans/example-plan/ --json \
  | jq '.runner_state | {stale_unfinished, stale_reason, run_id, run_started_at}'
```

Text output prints a second `Run state: stale/orphaned (<reason>)` line under
the regular `Run state:` line. The orphan is informational: durable session
completion still lives in the `## Handoff` blocks, and the next `asl run` will
acquire a fresh lock, recompute readiness, and resume normally. If the orphan
appears alongside a `Lock: stale (...)` entry, clear the lock with
`asl unlock ... --force --json` only after confirming no other runner is
genuinely active.

Left alone, such a run never gets a terminal record, so a following `asl tail` (it follows by default) and
anything else waiting on `run_finished` waits forever. Both `asl status` and a
stale-lock takeover inside `asl run` therefore reconstruct that ending once
death is *proven*: the lock's pid is gone and its recorded `hostname` matches
this host (or predates the field), or no lock has existed for the 120s
`ABANDONED_NO_LOCK_GRACE_SECONDS` window past `run_started_at`. Marking rewrites
`state.json` with `ok: false`, `reason: abandoned`, a `run_finished_at`, null
`active_turn`/`active_verification`, and an `abandoned` block (`detected_at`,
`trigger`, `lock_reason`, `detected_by_pid`), then appends one synthetic
`run_finished` event carrying `synthetic: true` and `outcome: abandoned`. The
recheck and both writes run under an exclusive `flock` on an `abandon.lock`
sidecar in the plan's state directory, so concurrent `asl status` probes and a
takeover racing them leave exactly one terminal record: the caller that cannot
take that claim declines rather than waiting. It is idempotent, and never fires
on an active lock, a lock recorded on another host,
a fresh no-lock start, or a stale lock with no usable pid. Taking a stale lock
over is not itself proof: the takeover path proves the *previous* owner's death
from the lock it removed, so reclaiming an unparseable or pid-less lock clears
the lock and leaves `state.json` and `events.jsonl` untouched. Any
`waiting_for_reply` record survives as evidence of what the run died holding.

Reading such a lock also removes it, so a dead runner stops blocking the plan
without an operator. The one case that qualifies is a pid missing from the local
process table on a lock whose recorded `hostname` is this host; the removal is
reported once on stderr as `reclaimed stale lock at <path>`. The call that
reclaims still reports the lock it found, so that `asl status` prints
`Lock: stale (pid N is not running)` and the next one finds no lock at all.
Nothing else is reclaimed by a read: a lock recorded on another host, a legacy
lock with no `hostname` key (its pid names no process table), an unparseable
lock, and any live pid all survive. `asl gc` is the one command exempt from all
of this: it has promised to write nothing in dry-run and to touch only run
directories under `--apply`, so it inspects locks without reclaiming them in
either mode. `asl unlock` remains the only route out for
a cross-host lock; the rest are taken over by the next `asl run`. The Workbench
read endpoints are exempt too, and for the same reason `asl gc` is: they build
every summary with `reclaim=False`, so refreshing the queue or opening a plan
observes a stale lock and reports it without removing it. Reclaim on the
Workbench stays where an operator asked for it — the Recovery Center flow and
`POST /api/unlock`. Every
mutation of `lock.json` — a read-side reclaim, a stale takeover, publishing a
new lock, `asl unlock`, and the release at the end of a run — runs under an
exclusive `flock` on a `lock.mutex` sidecar in the plan's state directory. The
lock file cannot order its own replacement because it is created and unlinked,
so two callers holding that path can be looking at different files; the sidecar
is never removed and is therefore a stable identity to queue behind. The proof
that a lock is dead is re-established under that claim and the file is unlinked
before it is released, so a lock a new runner published between the proof and
the removal is left to its new owner rather than deleted. No mutator ever
unlinks without holding the claim, and each degrades in its own way when the
wait expires: a reclaim skips, leaving the lock for the next reader; acquiring
and `asl unlock` fail loudly; and the release at the end of a run leaves its
lock in place and reports that on stderr, because the file sitting there after
a contended wait may already be a replacement owner's live lock. That abandoned
lock names a pid on this host, so it is reclaimed by the next read once the
process exits.

### Missing progress journal entry for the final session after a killed runner

The runner appends to `progress.md` (the runner-managed cross-session digest)
**after** the post-turn verification gate succeeds. If a runner is killed
between writing the `- **Status**: complete` handoff and finishing
verification, the session's handoff is durable but the journal entry will be
missing. The fix is to rerun the loop — verification reruns, and the
post-verification append is idempotent against `(session_id, completed_at)`.
If the operator already finished the run by hand and only wants to backfill
the journal, regenerate it from the handoffs:

```bash
asl backfill-progress ~/.local/share/agent-plans/example-plan/
asl backfill-progress --force ~/.local/share/agent-plans/example-plan/
```

`backfill-progress` reconstructs the journal from completed handoffs; `--force`
replaces an existing journal atomically. Never edit `progress.md` by hand from
a session — the schema-versioned `<!-- agent-session-loop progress-journal v1 -->`
header is what the runner uses to refuse stale or malformed journals.

To inspect the journal without opening the file, use
`asl progress PLAN_DIR [--tail] [--json]`: it prints the runner-managed
journal, follows it live with `--tail`, and emits parsed entries with
`--json`.

### Accepting remote-ref updates after a paused review

For a paused review whose retained changes consist only of existing remote-ref
updates, `asl accept-ref-updates PLAN_DIR --session SESSION_ID --json` previews
the evidence. After explicit operator approval, repeat it with `--accept DIGEST
--reason 'Operator approved these ref updates'` using the preview's exact
`evidence_digest`. Acceptance requires unchanged reviewed evidence, no active
runner, complete registered repository snapshots, clean current worktrees, and
refs still at the recorded values. It records the operator decision without
changing session scope or completing the review. Resume with the original
`asl run` command; ordinary readiness and write guards still apply. Source,
local branch, tag, topology, and unavailable-evidence failures are ineligible.
See [the recovery contract](../../agent_session_loop/README.md#accepting-remote-ref-updates-after-a-paused-review).

### Interactive Q&A resume

When a harness pauses with `RUNNER_PAUSE: blocked - ...` because it is waiting
on operator answers, the runner records the structured pause and surfaces an
`interactive_help` block. The recommended workflow is:

```bash
# 1. Run pauses with reason: blocked and prints interactive_help.
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --json

# 2. Render the resolver prompt for a human or a wrapping agent.
asl questions ~/.local/share/agent-plans/example-plan/

# 3. After collecting answers, resume the same paused session with the
#    operator's verbatim reply. The reply text is never persisted.
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --reply "1a, 2b, 3a"
```

The same `interactive_help.questions_command` and `interactive_help.reply_command`
strings are the canonical paste-ready invocations a coordinator should use:
they preserve the active `--state-dir` so the resume targets exactly the
paused turn. Use `asl run --dry-run --reply TEXT` to inspect the resolver
prompt and the in-harness reply prompt side-by-side without invoking the
harness, consuming the paused state, or writing the preview payload to
`state.json`/`events.jsonl`.

## Workbench

`asl workbench [serve]` runs a stdlib-only HTTP control plane that
surfaces every plan ASL can see in one local browser UI. It is
intentionally loopback, single-user software: there is no remote-access
mode, no multi-tenant auth, and no scheduling logic beyond a thin
renderer over the runner, registry, cleanup/archive storage, control
queue, launch, and unlock helpers.

```bash
asl workbench
asl workbench serve
asl workbench serve --port 0 --open
asl workbench --plan-root ~/work/plans --plan-root ~/.local/share/agent-plans
asl workbench serve --state-dir /tmp/asl-state --json
```

The bare `asl workbench` invocation defaults to `serve`; the same
flags (`--host`, `--port`, `--state-dir`, `--plan-root`, `--open`,
`--json`) are accepted on both the parent and the subcommand so either
position works.

The server binds `127.0.0.1:8765` by default. `--port 0` selects an
ephemeral port (useful for tests or for running multiple workbenches side
by side). `--state-dir DIR` mirrors the same flag on `asl run`; `--plan-root
DIR` may be passed multiple times to add extra directories to plan
discovery. `--open` best-effort launches the default browser at the
tokenized URL; the URL and token are also printed regardless, so the
operator can paste them by hand if the browser cannot be opened. `--json`
prints a startup payload (`reason: serving`, `host`, `port`, `url`,
`token`) instead of human text and the server still blocks until exit.

CLI registry maintenance commands let operators manage pins without
opening the UI:

```bash
asl workbench pin /path/to/plan-dir
asl workbench pin /path/to/plan-dir --cwd /path/to/repo --label "Refactor"
asl workbench unpin /path/to/plan-dir
asl workbench registry --json
```

`pin` upserts a registry entry for the canonical `plan_dir`; `--cwd`
sets the optional legacy repository root and `--label` sets the optional
human-readable name. Registered pins additionally project their prepared
`workspace_root`, registry digest, and complete ordered repositories. Their
launches always use that binding; a conflicting saved root is rejected with
`asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE --rebind-workspace`
guidance. Other launch-profile fields stay `null` (runner
default) unless edited through the UI. `unpin` removes the matching
pin. `registry` prints the loaded registry (pins, `plan_roots`, and
any loader warnings); pass `--json` for the
`responses.success_payload`-shaped envelope used elsewhere in the CLI.

Queue/detail summaries, launch results, state, Recovery Center, and final-review
responses retain `cwd` for compatibility and add `workspace_root`,
`registry_digest`, and ordered `repositories`. Recovery evidence identifies
repository-qualified failed checks, retained scope violations, unavailable Git
evidence, unresolved commit groups, and partial per-repository commit SHAs.
`GET /api/final-review` also returns the bounded Markdown view plus parsed
`final-review.json` when available, preserving finding repository attribution
and repair-group scopes. Browser and terminal summaries show a bounded prefix
for very large registries; machine-readable vectors remain complete. The
browser's `formatRepositoryAliases` formatter is the authoritative source for
its 12-repository preview limit.

The console output for a normal start is:

```text
Workbench serving on http://127.0.0.1:8765
Authorization token: <opaque secrets.token_urlsafe(32) value>
```

Open `http://127.0.0.1:8765/?token=<token>` (or use `--open`). The
frontend strips `?token=...` from the URL after first load and persists
it in `sessionStorage`; refreshing the tab keeps the token. The token is
regenerated on every server start, so existing tabs need a fresh URL
after a restart.

### Security model

- The default bind is `127.0.0.1`; the server never reaches outside the
  local host without an SSH tunnel or other operator-initiated forward.
- Every mutating endpoint (`POST /api/registry/pin`,
  `POST /api/registry/unpin`, `POST /api/plan/archive`,
  `POST /api/plans/archive`, `POST /api/plan/delete`,
  `POST /api/archive/restore`, `POST /api/archive/delete`,
  `POST /api/archive/settings`, `POST /api/launch`,
  `POST /api/reply`, `POST /api/control/...`, `POST /api/unlock`)
  requires
  an `Authorization: Bearer <token>` header. The token is generated with
  `secrets.token_urlsafe(32)` at startup and compared with
  `secrets.compare_digest`.
- Read endpoints (`GET /api/queue`, `/api/plan`, `/api/recovery`,
  `/api/state`,
  `/api/events`, `/api/logs`, `/api/last-message`, `/api/result`,
  `/api/verification`, `/api/commits`, `/api/progress`,
  `/api/final-review`, `/api/questions`, `/api/registry`,
  `/api/archive`, `/api/artifacts`, `/api/redirect-status`,
  `/api/conversation-recovery`) are **not** authenticated. They are
  loopback-only by construction. If
  the Workbench is ever exposed beyond the local machine (e.g., SSH port
  forwarding, a reverse proxy), plan inventory and resolver prompts —
  including paused-Q&A `last-message` text — will leak. Treat the URL the
  same way as any other unauthenticated local devtool.
- Reply text only ever travels through the child process stdin via
  `asl run --reply-stdin`. `POST /api/reply` accepts the operator answer
  in the JSON body and forwards it to `workbench_launch.resume_plan_with_reply`,
  which writes it to the child stdin and discards the byte buffer. The
  reply text is never persisted in `state.json`, `events.jsonl`, argv,
  or turn artifacts; only the character count surfaces (as
  `reply_length`) on the launch result and `REPLY_PROVIDED_EVENT`.
- Stop controls are checkpoint-only and never terminate an active
  harness subprocess. They are equivalent to typing `p`, `1`/`2`/`3`,
  etc. at a text TTY runner.
- Every `python -m agent_session_loop run` child the Workbench spawns —
  including the intervention and `--resume` launches — carries
  `ASL_INVOCATION_ORIGIN=workbench`, so `asl metrics` attributes those
  runs to the Workbench rather than to a supervisor at a terminal. That
  marker and — on an `isolate_locks=True` launch — `ASL_LOCK_ISOLATION=1`
  are the only entries `launch_plan` sets; a caller-supplied `env`
  mapping is otherwise passed through unchanged, and a launch with no
  `env` materializes the inherited environment so the child is still
  attributed. See [CLI invocation trace](14-safety-modes.md#cli-invocation-trace).
- `ASL_LOCK_ISOLATION` narrows a spawned child's lock authorization to
  the plan it was launched for: the child still takes that plan's lock
  and still refuses an overlapping claim, but it reclaims no other
  plan's proven-dead lock. It is set only by launchers authorized one
  plan at a time — today, the overseer's environmental-recovery resume
  through `intervention.resume_recovered_run`. An operator-driven
  launch, including `asl run` and the Workbench's own reply resume, sets
  nothing and keeps the ordinary corpus-wide hygiene. The variable is
  internal, presence-checked rather than value-checked, and never
  documented as an operator knob: setting it by hand only turns off
  stale-lock cleanup for that one run.
- The Recovery Center is a read-only diagnosis layer. It may display
  copyable repair commands, but the Workbench never executes those
  commands. Action cards only route to existing affordances and
  endpoints (`/api/reply`, `/api/launch`, `/api/unlock`, or local
  detail-panel focus targets).
- `--unsafe-bypass` is still the runner default. The Workbench launch
  form surfaces the choice and the UI confirms the launch before
  spawning a child with bypass mode set, but flipping the toggle does
  not change global runner defaults.
- The supervision bridge can host a Workbench listener of its own.
  `workbench.ensure` lazily starts one server on `127.0.0.1` with an
  ephemeral port and a freshly generated token, hands back a
  `/?token=...#plan?dir=...&focus=...` deep link, reuses that same server
  for every later request, and stops it when the bridge process exits.
  The security properties above apply unchanged: the token guards the
  same mutating endpoints, the read endpoints stay unauthenticated, and
  the listener is loopback-only. What differs is lifetime and
  disclosure — the URL carries a token in its query string, so treat it
  as a credential rather than a bookmark, and the listener dies with the
  bridge rather than outliving the client that asked for it.
- The bridge decides what a supervision client is even able to record. A
  snapshot carries identifiers, counts, states, and timestamps; question
  and reply prose reaches a client only through a deliberate
  `question.get` for the one question it is rendering, and comes back with
  no durable place to land. That is the property a client's own diagnostics
  inherit: Agent Mux keeps the availability, version, protocol, restart
  count, snapshot age, and action-outcome counters in `amx doctor` and its
  debug bundles precisely because the bridge never handed it anything else
  to keep. A bridge-authored refusal message is not published either — the
  client reduces it to the stable error code before it reaches a shared
  artifact, because an ASL message is free to quote the plan it just failed
  to find.

### Registry, pins, launch profiles

The Workbench registry is a JSON file stored at
`$XDG_CONFIG_HOME/agent-session-loop/workbench.json` (default
`~/.config/agent-session-loop/workbench.json`). Schema version `2`. The
file is hand-editable; malformed pin or root entries become recoverable
warnings on the loaded registry rather than failing the server.

Pins record:

- `plan_dir` — canonical absolute path (deduplicated).
- `label` — optional human-readable name shown in the UI.
- `profile` — optional `LaunchProfile` defaulting all fields to `null`.
  Recognized fields mirror `asl run` flags: `cwd`, `harness`,
  `harness_model`, `effort`, `timeout`, `commit_mode` (`per-session` or
  `end`), `review_harness`, `review_harness_model`, `unsafe_bypass`,
  `allow_not_loop_ready`, and `note`. `null` means "no preference"; the
  workbench falls back to the runner default. `review_harness` must differ
  from `harness`, and `review_harness_model` requires `review_harness`.

Schema-v1 registries remain readable, but a legacy `profile.model` is ignored
and surfaced as a recoverable warning; it is never forwarded to `asl run`.
The next pin/profile save writes schema v2 with `harness_model` and
`review_harness_model` only, permanently dropping the legacy shared field.

  The nullable booleans (`unsafe_bypass`, `allow_not_loop_ready`) are
  surfaced in the UI as tri-state `<select>` controls with literal
  `(default)`, `enabled`, and `disabled` options that round-trip
  `null`/`true`/`false` through the registry without coercion. The
  blank/default state emits **no** `--unsafe-bypass` and **no**
  `--no-unsafe-bypass` argv — it leaves the runner default
  (`--unsafe-bypass` today) in place. `enabled` adds
  `--unsafe-bypass` / `--allow-not-loop-ready`; `disabled` adds
  `--no-unsafe-bypass` (`allow_not_loop_ready=false` intentionally has
  no negative flag because the runner has no opposite of
  `--allow-not-loop-ready`).

Concurrent pin/unpin writes (e.g., two browser tabs) are serialized
through an advisory `fcntl.flock` on a sidecar file
`workbench.json.lock` next to the registry. The lock file is created
lazily on first write and inherits the same per-user permissions as the
registry. The atomic write itself uses `tempfile.mkstemp` plus
`os.replace`.

`plan_roots` is a list of extra discovery directories the operator
wants surfaced. They are merged with any `--plan-root` CLI overrides
and the well-known defaults. Registry roots are scanned alongside
CLI-supplied roots and both flow through `/api/queue` under the
canonical `plan-root` discovery source label, so the queue surfaces
plans from registry `plan_roots` even when the operator did not pass
`--plan-root` on the command line.

The registry also stores `archive_retention_days`, default 30 days.
Invalid or missing values load as the default with a recoverable warning,
and `POST /api/archive/settings` is the supported way for the UI to
update the retention window.

### Plan discovery

Discovery sources, in stable canonical order, are:

- `state` — every per-plan directory under the resolved ASL state root
  (e.g., `~/.local/state/agent-session-loop/<plan-hash>/`).
- `registry` — `plan_dir` for every pin in `workbench.json`.
- `plan-root` — the union of registry `plan_roots`, CLI
  `--plan-root DIR` values, and `$AGENT_PLANS_DIR` when set.
- `agent-plans-dir` — `$AGENT_PLANS_DIR` itself when present.
- `tmp-default` — `$TMPDIR/agent-plans` if it exists.
- `share-default` — `$XDG_DATA_HOME/agent-plans` (or
  `~/.local/share/agent-plans`) if it exists.

A plan-root directory is scanned for immediate children that contain
both `manifest.md` and `context.md`; nothing deeper is walked. Each
discovered plan dedup-merges across sources and preserves the full list
of sources that surfaced it.

Archived entries live under `<state-root>/archive/<archive_id>/`.
Archive storage is outside normal plan-root discovery, so archived plans
do not reappear in the queue until they are restored.
