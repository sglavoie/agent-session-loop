## Autopilot Skill (`$asl`)

The shared `asl` skill (`skills/asl/`, invoked as `$asl` under Codex or `/asl`
under Claude/Copilot) supervises a single `asl run` invocation from start to
finish without a human babysitting each turn. The operator hands the skill the
exact `asl run ...` command they would otherwise type; the bundled helper
(`skills/asl/scripts/asl-autopilot.py`) starts or attaches to the run, polls
`asl status --json`, streams heartbeats to stderr, auto-retries recoverable
pauses, and stops only when a run needs a human answer or has finished. Unless
the command carries `--no-mailbox`, it also establishes a supervisor lease
before starting a child, discovers live worker questions, and keeps supervising
while the runner waits for a reply.

### `asl supervisor-guide`

`asl supervisor-guide [--harness auto|codex|claude|copilot] [--json]` prints
that whole workflow as one canonical model, so an agent that never opened this
document still gets the exact commands. Text and JSON render from the same
model and therefore carry equivalent commands; the JSON payload is `ok`,
`command`, `selected_harness`, `skills`, `helper`, `operations`, and
`leaf_help`. It covers the `$asl` / `/asl` skill invocation carrying the user's
exact `asl run` command, the detached
`<skill-dir>/scripts/asl-autopilot.py --command ... --supervision-id ... --json --detach`
launch and its `--reply-stdin` resume, the `asl outbox supervise`,
`asl inbox reply`, `asl inbox resolve`, `asl inbox guidance`,
`asl control guard add`, `asl control redirect`, `asl control redirect-status`,
`asl control recover-conversation`, and `asl status`
operations, and reading the helper's verdict from the handshake's
`result_path`. Skill prefixes come from the harness adapters themselves rather
than a second table. `--harness auto` uses the existing invoking-harness
detection: a single detected harness (or an explicit `--harness NAME`) is
reported in `selected_harness` and shown alone, while detection that is absent
or ambiguous leaves `selected_harness` null and offers every supported harness
candidate rather than failing. The command is registered through the lazy
`handler_ref` mechanism, so importing or building `cli.py` never loads it.

Every nested command group — `bridge`, `workbench`, `control`, `inbox`,
`outbox`, and `assurance` — carries an epilogue, because a leaf-only group's
help lists its leaves and none of their flags, which reads to an agent like the
flags do not exist. Those groups say that they list leaves only and name the
leaf help to read instead, such as `asl outbox supervise --help`; the
supervision groups also point at `asl supervisor-guide`.

`asl workbench` is the exception: it installs the serve flags on the parent
parser and the bare `asl workbench` form runs `asl workbench serve` with them,
so its help legitimately shows `--host`, `--port`, `--state-dir`,
`--plan-root`, `--open`, and `--json` alongside the leaves. Its epilogue says
so — bare `asl workbench` runs `asl workbench serve` with those flags, and
every other leaf has its own — instead of claiming the group lists leaves only.

### Install and sync

The skill ships in the repository's top-level `skills/asl/` and `install.py`
links it into every harness skill directory alongside the other ASL skills. Run
`just sync-skills` (or `just all`) to refresh those links, then
`just verify-asl-install` to confirm the active `asl` wrapper and the installed
skill links resolve to this checkout. See [Setup](../../README.md#setup).

`just sync-skills` also renders the always-loaded ASL startup card, which is
how a fresh Codex, Claude or Copilot session learns the supervisor route without loading
the whole skill. `agent_session_loop/startup_context.py` builds the card from
`supervisor_guide.guide_payload`, so the card cannot drift from
`asl supervisor-guide`, and maintains it as one marked block in the context file of every harness root
that exists (`~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md`, the `CLAUDE.md`
of any extra profile named by `CLAUDE_CONFIG_DIR` or `ASL_CLAUDE_CONFIG_DIRS`,
and `~/.copilot/copilot-instructions.md`, created when it does not exist yet),
and in the generated skill reference
`skills/asl/references/supervisor-context.md`. Everything outside the markers
is preserved byte for byte. The start marker carries a digest of the block
body: `python3 -m agent_session_loop.startup_context --check` (also
`just check-startup-context`) reports stale blocks without rewriting them
(listing a context file that does not exist yet as `missing:`), and
`just verify-asl-install` fails when an installed context file is missing or
carries a marker other than the current one, naming `just sync-skills` as the
remedy, when the installed ASL skill has lost its
reference-loading instruction, or when its bundled helper does not resolve to
this checkout.

### Safety model

The helper is deterministic supervision only; all inferential reasoning stays
with the harness agent driving the skill. It:

- Parses the command itself and refuses shell operators, pipes, redirects,
  `--dry-run`, user-supplied `--reply` / `--reply-stdin`, and
  `--fresh-conversation`. It never runs the command through a shell — argv is
  built directly, tilde paths are expanded, and `--json` is guaranteed.
- Leaves ASL authoritative for implementation, verification, session status,
  progress journals, and review session execution. The skill does not patch
  repository code directly.
- Prefers the complete ordered workspace projection from `asl status --json`.
  When a registered command omits `--cwd`, the child receives the bound
  workspace root. A conflicting `--cwd` stops as `user_required` with atomic
  rebind guidance; Autopilot never grants itself rebind authority.
- Never persists reply text: resumes flow through `asl run --reply-stdin`, so
  the reply appears only on the child's stdin, never in argv or helper output.
- Attributes every `asl` child it spawns to itself. One central child
  environment feeds the `asl run`, `asl status`, and `asl questions`
  subprocesses, and it stamps `ASL_INVOCATION_ORIGIN=autopilot` after the
  supervised command's own `NAME=VALUE` assignments are overlaid, so a
  registered command cannot relabel helper automation as a direct operator
  call. `asl metrics` reports those calls under the `autopilot` origin; see
  [CLI invocation trace](14-safety-modes.md#cli-invocation-trace).
- Uses the existing child/attach wait loops for mailbox polling and lease
  renewal; it does not create a mailbox watcher thread. Full question bodies
  are retained only in the structured result's `outbox`, while stderr receives
  a bounded, single-line, JSON-escaped `QUESTION id=... preview=...` record.

### Supervisor policy: retries, recovery, and urgent intervention

Supervision retries what a machine can retry and hands back what it cannot.
The split is `pause_policy`'s, not the helper's: `RECOVERABLE_PAUSE_REASONS`
(`verify-failed`, `transport-failed`, `timeout`, `no-status-change`) are
relaunched up to `DEFAULT_MAX_RETRIES` (2) times per `(session, reason)`, and
only `transport-failed` backs off, on the fixed
`TRANSPORT_RETRY_DELAYS_SECONDS` schedule of 5s then 30s. A redirect or
conversation-recovery pause is not in that set, so it consumes no retry budget
at all — a plan that hits one twice is in exactly the same position the second
time as the first.

Two pauses are recovery decisions rather than failures, and both stop with
`user_required`:

- **`conversation-recovery-required`.** The recorded conversation cannot be
  resumed: `conversation_resume.PROCEEDING_SELECTION_STATES` is written as the
  two states that may proceed (`resumable`, `absent`), so every other selection
  state — including a state added later — lands in
  `RECOVERY_REQUIRED_SELECTION_STATES` by default rather than falling through
  to a fresh conversation nobody authorized.
- **`redirect-successor-blocked`.** A redirected turn's predecessor could not
  be shown stopped, so a successor would run beside a worker that may still be
  alive.

A `user_required` stop on either carries a `recovery` block in the structured
result naming `decision` (`fresh-conversation` or `predecessor-not-stopped`),
an explicit `auto_resume: false`, the `inspect_command` that reads the
evidence (`asl control recover-conversation` or `asl control redirect-status`),
and an `authorization_command`. The block is derived from the pause the runner
already wrote plus the supervised command's own plan/state paths: it reads no
file, opens no journal, and settles nothing, so producing it cannot change what
it describes.

`--fresh-conversation` is a one-use, never-persisted operator authorization,
and it cannot be supervised. Two independent layers enforce that: the helper
refuses a supervised command carrying it with `unsupported-mode` at parse time,
and `_CHILD_RUN_ARG_SKIP_DESTS` drops the flag even from a namespace built
without going through `parse_command`. The refusal is the point — a helper that
accepted the command and then stripped the flag would either replay a fresh
start on every retry or discard an explicit decision without saying so. So for
`fresh-conversation` the `authorization_command` is the exact relaunch to run,
rendered by `redirect_cli.fresh_conversation_command` (the single renderer, so
the helper and `asl control recover-conversation` cannot disagree). For
`predecessor-not-stopped` it is `null` and the absence is stated rather than
left as a missing key: no text an operator sends makes a second worker safe to
start beside a live one.

Uncertain evidence stays uncertain everywhere. A correction a worker was
launched with and never receipted comes back as `redirect_recovery.STATUS_UNCERTAIN`
and is excluded from `recoverable_operation_ids`, so no later attempt carries
it automatically; re-submitting is the operator's move, with its own words
chosen knowing what happened. A stop stays stopped for the same reason:
`stop-after-current` and `abort` end supervision cleanly with `stop_kind`
rather than being relaunched into the work the operator just stopped.

Urgent intervention (`asl control redirect`) is documented in
`asl supervisor-guide` for a supervisor the user has **explicitly authorized**
for that correction. Unlike guidance it interrupts the turn in flight, so it is
never self-authorized from a supervision signal, a failing check, or a
suspicion that the worker is wrong. It is deliberately not a fleet authority:
`overseer_decisions.OPERATIONS` stays `reply`, `guidance`, `verify`, `commit`,
`resume`, because every one of those either answers a question the run asked or
acts between turns, while a redirect replaces what a worker was told mid-turn.

### Attach behavior

Each supervision cycle reads `asl status --json` first. A stale lock is treated
as safe to start over (ASL clears stale locks before acquiring). An active lock
for the same plan and state means **attach**: the helper polls the foreign run
to completion instead of starting a second one. An active lock for a different
plan directory, or an observably different harness than the operator requested,
stops with `user_required` for a human to resolve. `--max-sessions N` is a
global session budget: the supervisor stops cleanly with `max-sessions` once `N`
implementation sessions have completed across the run (there is no per-chunk
restarting — an uncapped run drives every pending session in one process).
Self-relaunches (pause retries and runner-owned review continuations) and attach
polls share one bounded budget so a wedged plan can never loop forever.

Mailbox supervision uses one status-derived watcher across attach polls and
later self-launches. It validates that the advertised mailbox and lease files
belong to the advertised per-plan state directory, starts from the physical
`last_seq`, polls questions and notices every five seconds, and renews its lease
every 30 seconds. Advancing through every physical line prevents cursor replay
after guidance, acknowledgements, filtered records, or malformed records.
Questions and notices use independent accumulators and identity deduplication:
notices from the exact live run are emitted as bounded escaped `NOTICE`
progress and replayed at least once on attach within each supervisor process,
even when the status-derived cursor is already past them. The final structured
outbox declares `include: all` and preserves complete separate `questions` and
`notices` arrays; notices never enter unanswered-question projections or lease
counts. When supervision returns, it stops future watcher activity and lets the
last fixed-size lease expire.
`--no-mailbox` skips watcher construction and lease writes in both launch and
attach modes.

### Reply and resolution behavior

While the helper is running, a `QUESTION id=...` progress record is
nonterminal. Answer it from a separate shell with:

```text
asl inbox reply <plan-dir> --to <question-id> --text '<answer>' [--state-dir DIR] --json
```

Use `--text-stdin` when the answer should not appear in argv. The first
concurrent reply or resolution wins. Use `asl inbox resolve ... --action
continue` only for a plainly nonblocking message that needs no factual answer;
use `--action pause` when the live wait should become ordinary paused-run
recovery. A reply and continue resolution are each consumed once and continue
the same session through their separately typed prompts; a pause resolution
ends the live wait without fabricating an answer. The helper keeps complete
question and notice records in the separate arrays of its eventual `outbox`
payload and treats `waiting_for_reply` as healthy bounded activity, including
the pending-outbox count, rather than a stalled or paused run.

If nobody answers before the runner's five-minute, lease-extendable,
30-minute-absolute wait expires—or mailbox I/O/fullness forces fallback—the
runner records its ordinary terminal pause. The helper then uses the existing
pause behavior below; no separate mailbox-only terminal status is introduced.

The helper auto-retries recoverable same-session pauses (`verify-failed`,
`transport-failed`, `timeout`, `no-status-change`) up to twice per session and
reason before escalating. Only `transport-failed` backs off before relaunching:
5 seconds, then 30 seconds; the other recoverable pauses retry immediately.
An unclassified `nonzero-exit` pause is never retried. Which pauses are worth
escalating comes from the persisted `pause_policy` block: `requires_user` is true
for every pause a machine cannot retry, and `user_action` names what the human is
supposed to do about it — `reply` when an operator answer can clear the pause,
`inspect` when no answer can and the environment has to change instead, and
`null` for the recoverable pauses that need no human at all. The helper
escalates `inspect` pauses immediately and routes `reply` pauses through the
mailbox wait. Three kinds of pause are `inspect`: the non-retryable harness
failures that carry no question context (`quota-exhausted`, `auth-failed`,
`invalid-model`); the pauses blocked on a workspace or environment
precondition (`commit-preflight`, `commit-failed`,
`repository-registry-required`, `workspace-repository-contention`,
`not-loop-ready`, `required-checks-incomplete`, `review-not-clear`,
`guard-protected-path-changed`, `guard-protected-path-unavailable`,
`conversation-recovery-required`, `redirect-delivery-unrecordable`); and the
verdicts a post-turn guard reached along with the turn outcomes nobody asked a
question about (`workspace-write-scope-violation`,
`workspace-write-guard-unavailable`, `review-guard-unavailable`,
`review-mutated-worktree`, `plan-artifact-tampered`, `unexpected-status-change`,
`multiple-status-changes`, `nonzero-exit`, `malformed-plan`,
`malformed-review-repair`, `interrupted`, `redirect-successor-blocked`,
`redirect-submission-unresolved`). The
dividing principle is whether a
mailbox answer can make the run proceed: a dirty worktree stays dirty however
the operator answers, so waiting for a reply would only burn the mailbox lease
before escalating anyway. That third group is the same principle read from the
other side — the mailbox wait is entered only from the runner-pause
disposition, so a guard verdict advertising `reply` was advertising an answer it
had no channel to receive. A reason joins the set on its own clearing path,
never on symmetry with a neighbour. The block also carries `policy_version`,
which lets a consumer diffing a persisted block against a fresh classification
tell a schema change from a real contradiction; `policy_version: 3` is the
revision that moved the blocked-precondition reasons off `reply`, and
`policy_version: 4` the one that moved the guard verdicts and unclassifiable
turn outcomes off it too.

`policy_version: 5` also makes `verify-failed`
require inspection when `verification_repair_exhausted` is true; an exhausted
repair budget is never automatically retried.

Every pause with `requires_user` also populates the state payload's
`action_required` block (`source: pause`), echoing the pause reason, message,
and `user_action` alongside a `remediation` list. Each remediation entry is a
preflight repair hint (`check_id`, `category`, `fix_category`, `message`) with
its plan-directory and repository-root placeholders already substituted, so a
readiness pause names the exact command that clears it — for example
`asl baseline <plan-dir> --cwd <repo-root>` for missing baseline evidence. A
runner-side refusal carries findings rather than hints in its details, so the
stable pause reason resolves one from `preflight.RUNTIME_REPAIR_HINTS` instead —
that fallback is what keeps an `inspect` pause from telling an operator to look
without saying where. `redirect-successor-blocked` is the reason that needs it
most, because it is the one pause no reply clears and no flag overrides: its hint
reads `details.termination` back in operator terms — `remaining_pids` are
descendants still answering under the identity they were sampled with,
`unverified_pids` are ones whose identity could not be read at all,
`ownership_unknown` means a probe was refused, `predecessor-uncollected` means the
stopped worker was never reaped — and names the `ps -p <pid> -o
pid,ppid,lstart,command` that tells a survivor from a reused pid. It also covers
the one reason that names a setting instead of a process: under
`process-sweep-opt-out` the operator's own `ASL_PROCESS_SWEEP_DISABLED` withheld
the descendant evidence, so there is no pid to inspect and unsetting it is the
whole repair. A pause whose
reason has no hint either still gets the
block, with an empty `remediation` list; a live intervention signal the turn
already raised is the more specific record of the same ask and is left in place.

That last rule holds only while the pause *is* the ask. A pause the runner
declared — `blocked`, `unsafe`, `malformed-contract` — is the runner's own
verdict and is strictly more specific than the harness's generic
`runner-status-paused` or `runner-pause`, and the run stops on it, so the
generic ask can no longer be answered. Writing that pause therefore retires the
live signal and keeps the displaced record under
`action_required_superseded`, naming the pause reason that displaced it. Nothing
else would: `clear_live_action_required` fires only when another turn starts or
the run finishes unpaused, and a paused run reaches neither, so the stale signal
used to outlive every pause that superseded it and reach the audit as a run
still asking for something it had already moved past. The signal names the same
session as the pause, so a same-session comparison would not have caught it
either. A signal the runner itself declared, review findings above all, is not a
live signal and still stands.

When a pause genuinely needs an answer, the result carries `needs_agent_reply`
with a `questions` block (resolver prompt, the paused agent's last message, pause
metadata, artifact paths). The driving agent either infers a clearly defensible reply
from the plan/repo/artifacts or relays the question to the operator, then
resumes by streaming the reply on stdin:

```text
printf '%s' '<reply text>' | skills/asl/scripts/asl-autopilot.py --command '<same exact command>' --json --reply-stdin
```

For live steering, use a separate shell while the helper owns the foreground:

```text
asl inbox guidance <plan-dir> --text '<scoped guidance>' --wait-delivered [--state-dir DIR] --json
asl control set-model <plan-dir> [--harness codex|claude|copilot] --harness-model MODEL [--effort EFFORT] [--state-dir DIR] --json
```

`--wait-delivered` is what turns the command from a report of what it wrote
into a report of what happened: it prints "Guidance delivered live to
<harness>" once the delivery ledger confirms the running turn received it, or
"Guidance queued but not delivered" with the reason when it did not — a
copilot turn declining a mid-turn steer, no turn in flight, or an attempt still
awaiting confirmation. Undelivered guidance stays pending for the next turn's
initial prompt or for the worker's next `asl inbox read`. Model/effort controls
are polled during active turns but affect only the next implementation
invocation in the current runner process; they do not alter the active
subprocess or generated review turns.

### Review-fix loop behavior

When the supervised command used `--review-harness`, a clean completion still
carries the final-review verdict. An all-clear review returns `complete` with a
`review` block whose `outcome` is `all-clear`. A review that reports findings
returns `review_findings` with the `final-review.md` path, status line,
severity counts, a bounded summary excerpt, and the freshest `review_resume`
guidance. Repair creation, child execution, and rereview are runner-owned on
the same plan. The supervisor attaches to an active matching lock before
considering zero-pending completion, and when unlocked it relaunches only for
`initial-review`, `materialize-repairs`, or `rereview` actions permitted by the
command's review harness. Only `materialize-repairs` is gated by the effective
absolute cap — 4 unique source-review materializations unless the supervised
command passed `--max-review-fix-cycles N`; eligible `rereview` launches are
cap-free because they consume no materialization budget. The runner also
persists the per-group reopen cap (`--max-repair-group-attempts`, default 2)
and the absolute cap ceiling (`--max-review-fix-cycles-ceiling`, default 8),
so resumed supervision reuses the same repair-loop bounds unless the operator
explicitly overrides them. A corrected malformed artifact or a higher cap can
therefore resume the same command later without a second plan. Insufficient
caps and terminal all-clear/repeated/unstructured findings do not relaunch, and
`repair-group-exhausted` or `cycle-cap-exhausted` hand control back to the
operator instead of silently raising caps, so every supervision loop has a
deterministic stop. The skill never authors, splits, or expands a separate
repair plan.

### Final helper statuses

The helper emits a single JSON result — the `result.json` at the launch
handshake's `result_path` under `--detach`, stdout otherwise — whose `reason`
field is the terminal verdict (`ok` is `true` only for a clean completion):

- `complete` (`ok: true`): every session finished. With `--review-harness`, an
  all-clear review is attached under `review.outcome: all-clear`.
- `max-sessions` (`ok: true`): the `--max-sessions N` global budget was reached
  with sessions still pending — an intentional clean early stop, not a pause or a
  failure. `completed` and `pending` counts are attached; re-running the same
  command continues from the remaining sessions.
- `stopped` (`ok: true`): an out-of-band control request was honored with work
  still to do — the structural twin of `max-sessions`, not a pause or a failure.
  `stop_kind` names which request it was: `stop-after-current` for the `p` key or
  `asl control stop-after-current`, honored at the session boundary, and `abort`
  for `asl control abort`, which ended the turn in flight and returned its
  session to `pending`, so that session is *not* in `completed`. An abort the
  worker satisfied inside its grace window never reaches this verdict: it
  surfaces as the worker's own pause, or as a run that simply kept going.
  `completed` and
  `pending` counts are attached and the helper does not relaunch; re-running the
  same command continues from the remaining sessions, or runs the pending
  review when every session is already complete (`review_skipped_reason:
  stopped`).
- `needs_agent_reply` (`ok: false`): the run paused on a decision the helper
  could not auto-resolve; a `questions` block carries the resolver context and
  the driving agent must supply a reply.
- `review_findings` (`ok: false`): the run completed but the final review
  reported findings and no automatic continuation supported by the current
  command remains; `review` carries the verdict and `review_resume` explains a
  higher-cap, artifact-correction, or terminal circuit-breaker result.
- `user_required` (`ok: false`): an active `asl run` lock holds this plan/state
  for a different run; a human must resolve the conflict before supervising.
- `blocked` (`ok: false`): a chunk stopped without completing and without a
  recoverable pause (the child's non-paused failure reason is attached).
- `capped` (`ok: false`): a safety budget was exhausted before the plan settled
  — `max_chunks` self-launched pause-retry/review relaunches, or
  `attach_max_seconds` (default 12h) of wall clock within a single attach
  episode. Both are kwargs-only, with no CLI flag.
- `status-error` / `run-error` (`ok: false`): `asl status --json` or `asl run`
  itself failed to produce a usable payload.
- `empty-reply` (`ok: false`): `--reply-stdin` was requested but stdin carried
  no non-whitespace reply text.
- A parse-stage failure (`ok: false`, `stage: parse`) with a specific reject
  reason when the requested command is unsupported or malformed.
