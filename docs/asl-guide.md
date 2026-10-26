# ASL operator guide

The operator-facing overview of `agent-session-loop` (`asl`): what each command
family does, how live supervision and assurance work, and how ASL is released
alongside Agent Mux. The exhaustive reference — plan schema, completion
contracts, every command's flags, state files, and JSON output — is
`docs/agent-session-loop.md`; the package internals are in
`agent_session_loop/README.md`.

`agent-session-loop` / `asl` runs split agent session plans one session at a
time. Both names are wrappers in `bin/.local/bin/`.

## Command summary

### Running plans

Run split agent session plans one session at a time.
`asl run` self-heals stale/missing runner-managed `progress.md` and recomputable manifest readiness metadata before pausing, `asl preflight` clears non-derived loop-ready findings, `asl verify` executes a session's completion contract, and `asl sessions` shows running and recent ASL-managed activity across every plan state directory.
Pass `asl run ... --review-harness codex|claude|copilot` to append a final pure-critique session under a second harness that writes `PLAN_DIR/final-review.md`.
Review repair materializations use an absolute cross-run `--max-review-fix-cycles` cap: a higher cap can reopen budget-stopped findings, and a corrected structured artifact can retry a prior `malformed-review-repair`; the additive `review_resume` JSON block and Workbench Recovery Center explain the next same-plan action.
When a session pauses with `reason: blocked` because it needs operator answers, use `asl questions PLAN_DIR` to render the resolver prompt and `asl run PLAN_DIR --reply TEXT` to resume the paused turn with the operator's verbatim reply.

### Workbench

`asl workbench [serve]` launches a stdlib-only loopback HTTP control plane (token-protected mutating endpoints, browser UI at the printed `?token=` URL) that surfaces every discoverable plan — including any directories listed under registry `plan_roots` — supports pin/unpin with tri-state nullable launch profiles (blank means "leave the runner default alone"), renders dotted session IDs verbatim from `/api/state.sessions`, enqueues stop-after-current and harness-selection through the runner control queue, streams logs/events/progress, offers cleanup/archive/retention views for old plans with restore and typed-count purge, shows a deterministic Recovery Center with `/api/recovery` diagnostics and copy-only repair commands, and submits paused-Q&A replies without persisting reply text.
The bare `asl workbench` form defaults to `serve`; companion commands `asl workbench pin PLAN_DIR [--cwd ROOT] [--label LABEL]`, `asl workbench unpin PLAN_DIR`, and `asl workbench registry [--json]` manage the registry from the CLI.
`asl workbench connect --target-id ID --origin https://HOST --token-file PATH` is the outbound half of the same Workbench: it reads the shared catalog, run, and history services in process — no bridge subprocess — and publishes bounded, path-free heartbeat, snapshot, catalog-page, and history-batch observations to a remote service over HTTPS, single-instance per state directory, with deterministic 1s–60s backoff and a history checkpoint that advances only after every frame of a pass was accepted, so a reconnect repeats rather than duplicates and observation never modifies the corpus it reads.

### Run guards

Mid-run enforcement, as distinct from advice, is `asl control guard add PLAN_DIR_OR_ID [--deny-command "CMD ARG ..."] [--protect-path ALIAS:PATH]`: a command rule is matched as an exact argv prefix and enforced by a per-run `PATH` shim, while a protected path is compared against the turn-start workspace snapshot at finalization, so a change made before the guard was added still counts and one undone exactly does not.
A violation rejects the turn, retains every filesystem change, returns the session to pending, and pauses with `guard-protected-path-changed`; it never rolls back, aborts, or restarts anything.
`asl control guard list` and `asl status` report each guard's per-lane coverage (`command-gate`, `pretool`, `postflight`) as active, degraded, or unsupported with a stable reason, because a harness that cannot enforce a rule should say so rather than imply it did.

### Run control without the Workbench

The Workbench's run controls are also available without it. `asl control stop-after-current PLAN_DIR_OR_ID [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]` and `asl control select-harness PLAN_DIR_OR_ID --harness codex|claude|copilot [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]` are the programmatic equivalents of the interactive `p` and `1`/`2`/`3` run-control keys, which are disabled for `run --json`, `run --dry-run`, and non-TTY runs; each validates the plan, enqueues onto the durable per-plan control queue, and reports `enqueued`, `command_id`, `run_active`, and a `note` (plus `pending_after_current` for stop-after-current and `harness` for select-harness).
The rest of `asl control` — `abort`, `set-model`, `guard`, and the `redirect`, `redirect-status`, `resolve-redirect` and `recover-conversation` leaves for correcting a live turn — is listed with its flags in the [command reference](agent-session-loop/02-commands.md); every receipt wait there takes `--wait` with `--timeout` (also spelled `--wait-timeout`).
A honored stop lets the current session finish and, when any session is still pending, exits the run successfully with `reason: stopped` so the next run resumes from the remainder (a stop requested during the last session is a no-op that still ends `all-complete`).

### Autopilot skill

The shared `asl` skill (invoked `$asl <exact asl run command>` under Codex or `/asl ...` under Claude/Copilot) supervises one `asl run` end to end: it starts or attaches to the run, polls structured status, auto-retries recoverable pauses, streams the reply back through `--reply-stdin` (never persisting reply text), and relaunches runner-owned same-plan repair/rereview continuations when the command's review harness and absolute cap permit them.
It stops with a terminal status (`complete`, `max-sessions`, `stopped`, `needs_agent_reply`, `review_findings`, `user_required`, `blocked`, `capped`, `status-error`, `run-error`) so the driving agent knows what to do next; `--max-sessions N` is a global session budget that stops cleanly (`max-sessions`, `ok: true`) once `N` sessions have completed, `stopped` (`ok: true`) is the twin clean early stop after an honored `asl control stop-after-current` left sessions pending, and `review_findings` means no automatic continuation supported by that command remains.

### Preparation and inspection

`asl prepare PLAN.md --cwd ROOT` runs the whole preparation pipeline (split, validate, expand, baseline, mark loop-ready) in one deterministic command and prints the exact `asl run` command on success, `asl split --check --plan-file PLAN.md` lints a source plan's session headings without writing anything and reports every malformed heading at once (each with a `suggested_line` when the repair is unambiguous) so `session-split` repairs a plan instead of dead-ending on `malformed-plan`, `asl baseline` records completion-contract baseline evidence for the preflight gate, `asl compact-state` reclaims duplicated operational JSON (dry-run by default, `--apply` to rewrite), `asl bridge serve --stdio --protocol 2` exposes the active/actionable run corpus and every operator intervention to an external supervision client over NDJSON on stdio (body-free snapshots plus `runs.changed`, an explicit `question.get`, and token-guarded `question.reply`, `question.resolve`, `run.resume`, `run.stop_after_current`, `run.abort`, and `workbench.ensure` that share one service layer with the CLI and Workbench, answer `accepted` with an operation ID, and report later outcomes as body-free `operation.updated` events), and `asl metrics` reports reliability, windowed review materializations and reopen attempts/results, and storage numbers over the recent state corpus (default activity window: last 7 days).

See `docs/agent-session-loop.md` for the split → expand → run workflow, the `$asl` Autopilot skill, and the Workbench security model, registry, cleanup/archive, Recovery Center, and smoke checklist.

## Plans and preparation

Prepared ASL plans carry a versioned `plan-metadata.json`. Legacy plans retain
version 1 with one canonical repository `cwd`; registered multi-repository
plans use version 2 with a canonical `workspace_root`, ordered registry digest,
and no duplicated repository registry; status and runner state add the ordered
resolved child projection. `--cwd` therefore means the repository root for a
legacy plan and the common workspace root for a registered plan. A run
validates an existing binding instead of silently rewriting it; only an
unbound legacy plan is backfilled. Moving a registered plan is an explicit,
atomic preparation operation:

```text
asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE --rebind-workspace
```

Prepared Codex runs can opt into selective Spark routing with `asl prepare
PLAN.md --spark` or `asl run PLAN_DIR --spark`. Preparation always records each
session's suitability; only eligible implementation turns use
`gpt-5.3-codex-spark`, while reviews, generated review repairs, live model
overrides, and ineligible work stay on the resolved baseline. A classified
Spark quota or unavailable-model failure immediately retries the still-pending
session on that baseline and records any shared reset cooldown in the ASL state
root. `asl status` reports the policy, current route, fallback, and cooldown.

Multi-repository source plans declare an ordered top-level `## Repositories`
table (`Alias`, `Path`, `Origin`), exact per-session `Reads repositories` and
`Writes repositories` scopes, repository-attributed completion checks, and
qualified `Alias:path` handoff paths such as `SVC:internal/foo.go`. The common
parent may be a non-Git directory because ASL validates and fingerprints each
registered child checkout. See `docs/agent-session-loop.md` for the full
schema, mutation guard, combined-review, partial-commit recovery, and
compatibility contracts.

## Operations

### Live ASL supervision

ASL can use a per-plan mailbox for scoped guidance and live question/reply
continuations. A direct run enables it when
`<state-root>/<plan-path-sha256>/mailbox.jsonl` exists or its separate
512-byte `mailbox-lease.json` supervisor lease is fresh; `$asl` Autopilot
establishes and renews that lease before supervising. `asl run --no-mailbox`
is the explicit override and preserves the historical terminal
pause/`asl run --reply` flow.

`asl supervisor-guide [--harness auto|codex|claude|copilot] [--json]` prints
the canonical supervisor workflow — the `$asl` / `/asl` skill invocation, the
detached `asl-autopilot.py --supervision-id ... --json --detach` launch and its
`--reply-stdin` resume, the `asl outbox supervise` / `asl inbox reply` /
`asl inbox resolve` / `asl status` operations, and reading the helper's verdict
from the handshake's `result_path` — with text and JSON carrying equivalent
commands. `--harness auto` reuses the invoking-harness detection and falls back
to every supported candidate when detection is absent or ambiguous. Because a
leaf-only group's help lists its leaves and none of their flags, `asl bridge`,
`asl control`, `asl inbox`, `asl outbox`, and `asl assurance` each say so and
name the leaf help to read instead, such as `asl outbox supervise --help`.
`asl workbench` instead carries the serve flags on the parent parser, so its
help shows them and its epilogue says the bare `asl workbench` form runs
`asl workbench serve` with those flags.

The eight mailbox commands are `asl inbox guidance`, `asl inbox reply`,
`asl inbox resolve`, `asl inbox read`, `asl ask`, `asl notify`,
`asl outbox wait`, and `asl outbox supervise`. (`asl inbox acknowledge` sits
under `asl inbox` too, but it is the worker's redirect receipt, not a mailbox
command.) `asl outbox supervise` is the
supervision loop: it wakes only on questions and terminal `supervision-end`
records, persists its own `--since` cursor per launch, absorbs non-actionable
wakes, and prints one of `IDLE`, `ACTION questions=<n>`,
`DONE outcome=<outcome> seq=<n>`, or `LOST reason=<reason>` at exit code 0, so a
healthy supervised run produces no terminal output. `LOST` reports a supervising
helper whose recorded `(pid, process birth time)` identity is gone
(`helper-exited-without-record`) or that never recorded one while no run holds
the plan (`helper-never-started`), with the lock, `run_id`, runner state, and
session counts that say whether the run itself is still alive; Autopilot is
launched with `--detach` so the supervision it forks outlives the shell that
started it. Notices let active worker turns publish nonblocking
information without opening a reply wait. Autopilot reports them as bounded
escaped `NOTICE` progress and keeps complete question and notice arrays
separate in its final structured outbox; attach replays notices from the live
run at least once per supervisor process and suppresses in-process duplicates.
The schema-v3 log is non-destructive, still reads v1/v2 records, uses physical
line numbers as durable cursors, and caps appends at 4 MiB. Nested capacity
reserves keep closure, wait termination, and consumption-receipt records
appendable. Wait-mode v3 questions accept a closure only while a recorded wait
is active; a late closure returns `question-not-waiting`. The first concurrent
reply or resolution logically closes a question, and all source, closure, and
lifecycle records remain append-only history because mailbox rotation and
compaction are intentionally absent.
Guidance may steer implementation choices inside the selected session, but
cannot override its scope, completion contract, safety constraints, or
runner-owned mutation rules. Guidance is advice; only a guard enforces.
`asl inbox guidance --wait-delivered` waits for the ledger to say what became
of it and reports either `Guidance delivered live to <harness>` or `Guidance
queued but not delivered (<reason>)`; without the flag nothing is observed and
the command says only that the record is durable. Delivery is never inferred
from a successful write. Codex is steered over its app-server, claude over a
bidirectional stream-JSON conversation whose delivery is confirmed by a
protocol acknowledgement or by an ASL-owned `PreToolUse` hook armed through a
turn-scoped settings overlay, and copilot declines a mid-turn steer outright
(`unsupported-active-steer`) and carries the guidance in the next turn's
prompt instead. An explicit `asl inbox read` is itself a delivery
(`delivered-by-read`). `asl status --json` projects the recent guidance
records with their state and latest attempt — channel, harness, turn, reason,
and receipt time — and never their bodies. A live answer wait lasts five
minutes initially,
then checks the supervisor lease. Lease freshness is checked only at an active
deadline: a fresh lease extends the wait by 60 seconds, while an absent or
stale lease ends it unless a detected system suspend leaves a 90-second awake
grace still covering it. The 30-minute ceiling bounds unsupervised time rather
than wall clock: it is measured from the last lease that proved a supervisor
alive, so a watched wait outlives it while an unwatched one still ends within
30 minutes of that proof. Reaching the ceiling, mailbox failure, or timeout
falls back to the ordinary pause. Mailbox closure polling runs every two
seconds, so a committed reply or resolution is normally observed within that
bound. Runner-generated worker asks use explicit 300-second waits; the general
`asl ask --wait` default remains 90 seconds. `asl inbox reply` and `asl inbox
resolve` report a closure as accepted until `--wait-consumed` observes its
receipt, and a consumption timeout remains a successful append with `consumed:
false`. A completed worker session with unanswered, unconsumed, or
pause-resolved blocking mailbox evidence is restored to pending and paused
instead of being accepted as complete.

`asl control set-model PLAN [--harness H] [--harness-model MODEL|--clear-harness-model] [--effort E|--clear-effort]`
changes only future implementation invocations in the current runner process.
It never changes an active subprocess or a generated review turn. The mailbox
adds two private (`0600`) state files, seven commands, and one `mailbox` event
name with body-free action variants; it adds no runner/supervisor thread, no
terminal status, and no Workbench mailbox-authoring, reply, resolution, notice,
or live model-control endpoint in v1.

### Harness version compatibility

`asl harness-compat [--harness codex|claude|copilot ...] [--strict] [--json]`
reports whether the installed harness CLIs fall inside the supported version
window pinned in `agent_session_loop/harness_compatibility.py`. It uses no
model: the only subprocess it builds is `<executable> --version`, which is what
makes it safe to schedule as a recurring CI smoke against every provider CLI.
Unknown releases warn and known-incompatible releases block — default exit is 0
clean and 1 on an incompatible release, while `--strict` exits 3 on anything
that is not `supported`. Probes memoize per executable, time out at 20 seconds,
and can be disabled with `ASL_HARNESS_VERSION_PROBE=0`; any probe failure
degrades to `unknown` rather than to `incompatible`.

`asl preflight` applies the same policy to the selected harness: an
incompatible release is a red blocking finding, an unrecognised one is a green
non-blocking warning. The warning is deliberately green rather than yellow,
because yellow preflight findings block, and a harness release newer than the
pinned window has to stay runnable.

### Supervisor assurance

ASL audits its own supervision after a persisted loop ends. Every eligible run
finishes with exactly one assurance state — `all-clear`, `findings`, `partial`,
`unavailable`, `skipped`, or `pending` — and the audit is advisory: it never
changes the run's exit code, pause reason, readiness, or artifacts. `partial` is
the honest middle: the audit ran, found nothing wrong, and could not judge every
rubric lane.

Six run flags decide whether and how a run is audited: `--supervisor-audit`
(the default) or `--no-supervisor-audit`, `--audit-harness codex|claude|copilot`,
`--audit-harness-model MODEL`, `--audit-effort LEVEL`, and
`--audit-timeout SECONDS` (a per-attempt budget, not one shared across attempts;
at most two unusable answers, plus four delivery attempts retried after 15, 60,
and 120 seconds when a launch never reaches the agent).
The audit harness must differ from `--harness` and may equal
`--review-harness`; with no explicit value the role is inherited from the
effective review harness. All six persist in `run_config`, so `--resume`
restores the audit settings the run started with unless a flag is passed again.
`--no-supervisor-audit` is the only way to run no checks at all: the run records
`skipped` and creates no issue.

Ordering inside one run is fixed. The loop and any final review finish, the
audit runs and writes `supervisor-audit-input.json`, `supervisor-audit.json`,
and `supervisor-audit.md` under the run directory, its ledger records are
appended, and only then is `run_finished` written — so the terminal event and
the state projection carry the same assurance verdict.

Findings become issues in one append-only ledger, `assurance-inbox.jsonl`,
which sits directly under the state root and is shared by every plan. Issues
are referenced as `ASI-42`, and the bare `42` is accepted everywhere, so any id
ASL prints pastes straight back in. Ten commands cover the whole inbox:

```text
asl assurance list [--limit ROWS | --all] [--state STATE] [--severity LEVEL] [--json]
asl assurance show ISSUE [--json]
asl assurance resolve ISSUE [--note TEXT]
asl assurance dismiss ISSUE [--note TEXT]
asl assurance accept-risk ISSUE [--note TEXT]
asl assurance merge ISSUE TARGET [--yes] [--note TEXT]
asl assurance reconcile [--apply MANIFEST [--yes]] [--json]
asl assurance closeout RUN_ID --outcome accepted|reworked|escaped [--issue ISSUE] [--replace]
asl assurance process-pending [--limit RUNS] [--run-id RUN_ID]
asl assurance plan ISSUE --harness NAME --output PATH
```

`list` and `show` never write; `list` defaults to the 50 most recently observed
open issues. The three dispositions differ only in what a recurrence does:
after `resolve` the finding coming back reopens the issue, while `dismiss` and
`accept-risk` keep it closed and still record the new sighting. `merge` is the
one irreversible command and prompts on a TTY unless `--yes` is given.
`reconcile` repairs an inbox written before identity keys existed: run without
`--apply` it writes nothing and prints a manifest of the safe bindings it can
add plus the pairs of issues that may be one defect, and `--apply` writes a
manifest a human has reviewed as one append, refusing anything still marked
`review` and anything reviewed against a catalog that has since moved.
`closeout` records what one audited run's findings were worth; a second
closeout for the same run is refused without `--replace`, which appends a
superseding record instead of rewriting the first.

A run whose own process was killed never reaches its audit, so the debt is
recorded rather than lost: `assurance-pending.json` is written into the dead
run's directory and the reconstructed terminal state points at it. Nothing
relaunches by itself — `asl status` detects the abandonment and may not start a
harness. `asl assurance process-pending` pays those debts on demand, claims
each run with a nonblocking lock so concurrent passes divide the backlog, and
is idempotent. Runs that predate assurance enumerate as `predates-assurance`
rather than as a backlog.

`asl assurance plan ISSUE --harness NAME --output PATH` is the only path from a
finding to an improvement proposal, and it stops at exactly one Markdown file
at the path the operator names. It creates no plan, no session, no readiness,
no commit, and no repository change; the drafting module cannot reach
splitting, preparation, or `asl run` at all. A human reads the document and
decides what, if anything, becomes real work.

The per-plan mailbox and the assurance ledger are separate systems that share
no file, lock, or lifetime. `mailbox.jsonl` lives in one plan's state
directory, carries live guidance and question/reply traffic for a run that is
still going, and is enabled per run. `assurance-inbox.jsonl` lives at the state
root, is corpus-wide and permanent, records audit history after runs end, and
is written under its own `assurance-inbox.lock`. Neither one reads, writes, or
locks the other.

### Agent Mux integration

When `amx` is on `PATH`, `agent-session-loop` emits Agent Mux lifecycle hooks
for run, turn, verification, wait, pause, and finish boundaries. The hook
payload includes the loop `cwd`, `harness`, and `state_path`; turn notifications
report the active turn harness, and finish notifications report the last
completed turn harness when present. A successful `asl inbox reply` also emits
a text-free `AgentSessionLoopMailboxReply` marker when a fresh supervisor lease
and the post-reply mailbox prove that no question remains open for the
projected run. The marker carries only run/question identity and mailbox
sequences plus additive accepted/consumed delivery state; acceptance never
claims an unconsumed closure was delivered. `asl inbox resolve` emits the
parallel text-free `AgentSessionLoopMailboxResolution` marker, including its
`continue` or `pause` action, under the same fresh-evidence and post-closure
no-open-question gate. Raw worker mailbox waits remain
bare 🔁; Agent Mux shows ❓🔁 only after the interactive supervisor has handed
the projected question to the user. If `amx` is unavailable or rejects the
marker, the mailbox closure still succeeds and normal lease polling remains
the fallback. Neither marker carries question, reply, resolution-note, or
notice bodies.

Hooks are one direction; `asl bridge serve --stdio --protocol 2` is the other.
Agent Mux's daemon owns one bridge subprocess and uses it to render the active
and actionable run corpus, and to reply, resolve, resume, stop-after-current,
abort, and open a Workbench deep link — the same interventions the CLI and the
Workbench expose, through the same service layer. It does not start, schedule,
or tail runs. Install or upgrade ASL *before* the client: a missing or
protocol-incompatible bridge is reported as unavailable and leaves the client
read-only rather than driving anything. The wire shape is pinned in
`tests/fixtures/agent_mux_contract/`, which Agent Mux vendors byte-identical and
validates in Go, so a one-sided protocol edit fails a test rather than an
installed pair. Compatibility is one exact contract-set ID rather than a
negotiation: a client that does not implement it keeps its read-only view and
loses mutations with a stable, non-retryable reason. See
`docs/agent-session-loop.md` for the protocol, the observational and mutation
guarantees, and the fixture change procedure.

### Releasing the pair

The two halves are released together, and the order is part of the contract:
**ASL first, then Agent Mux.** ASL is the older, more widely installed half, so
upgrading it first means the new client finds a bridge it can talk to on the
first start. The reverse order is safe but inert — the client reports the bridge
unavailable and stays read-only until ASL catches up.

For an ordinary reinstall of the same revisions, that is the whole procedure:

```bash
just all                  # link the wrappers and skills, render the startup card
just verify-asl-install   # active wrappers and skills resolve to this checkout
```

Cutting an actual release runs Agent Mux's gate, which drives both repositories.
`paired-release-gate` is a recipe in the
[Agent Mux](https://github.com/sglavoie/agentmux) checkout, not this one; run it
there:

```bash
# in the Agent Mux checkout
just paired-release-gate asl_root=/path/to/agent-session-loop signing_key=/path/to/signing_key
```

It requires both working trees to be clean, hashes ASL's wrapper and a digest
over `agent_session_loop/`'s sources, runs `just test-all`, `just lint`,
`just reliability-stress`, and `just verify-asl-install` on this side, compares
the canonical corpus in `tests/fixtures/agent_mux_contract/` against the vendored
copy byte for byte, installs both halves in the order above, restarts the real
daemon, and drives a real Kitty smoke. It then writes a deterministic JSON
evidence record — both revisions, the artifact hashes, the contract set ID, the
manifest SHA-256, the OS/Kitty/harness versions, and every command with its
result and duration — signed with `ssh-keygen -Y sign` under the namespace
`agentmux-asl-release`, and tags both repositories with the same pair ID.

Verify a record with the namespace named explicitly; a signature made under a
different namespace must not verify as a release approval:

```bash
ssh-keygen -Y verify -f allowed_signers -I release@example.com \
  -n agentmux-asl-release -s <record>.json.sig < <record>.json
```

The record's `"result"` field is what says the pair passed — a failed run is
written and signed too, because a failed gate's record is the most useful thing
it produces. Agent Mux's `docs/operations.md` holds the full procedure,
rollback, and maintenance-installation steps.

## Testing

```bash
just test-fast
just test-portable
just test-all
just reliability-stress
```

`just test-fast` runs the unit/schema/parser test modules; `just test-all` runs
the complete suite (including helper-script tests under `skills/`) as isolated
parallel shards. Both use stdlib `unittest` only — see the Verification section
of `docs/agent-session-loop.md`.

`just test-portable` runs the subset that spawns no external process — no `git`,
no shell script, no launchd, no harness executable — so it can run on a machine
(such as Linux) without those tools. `just test-all` runs against the real
tools, so nothing is reported as passing on a machine that does not have them.
The workflows in `.github/workflows/` are active: `check.yml` runs on every push
to `main` and every pull request against it -- a Linux lane (lint plus
`just test-portable`, on Python 3.14 and 3.15) and a macOS lane (lint plus
`just test-all`, sliced across parallel jobs) -- and `harness-compatibility.yml`
is a weekly, no-model-use `asl harness-compat` smoke. A failed test job uploads
`.cache/run_suite_logs/`, the full output of every failed shard. The same
recipes run locally, and CI installs its linters from the `dev` dependency
group in `pyproject.toml` (`python3 -m pip install --group dev`), so install
that group to lint with the versions CI gates on.

`just reliability-stress [reps] [shards]` re-runs the load- and calendar-sensitive
scenarios in isolation, then across a spread of timezone offsets, then as
concurrent shards while the full suite saturates the machine. The scenarios
synchronize on run state and pin their own clock, so neither contention nor the
passage of the calendar may change their outcome. The full suite runs there only
as load and does not gate the recipe — its outcome is reported, but `just
test-all` is what judges it, unloaded.

End-to-end tests bound every CLI call with a generous hang ceiling so a wedged
subprocess still fails rather than hanging forever. The per-call time budgets are
asserted separately, only when `ASL_E2E_TIME_BUDGETS=1` is set, so a slow or busy
machine reports a slow machine instead of a spurious correctness failure. The
final phase of `just reliability-stress` sets that variable once the rest of the
run has finished and the machine is quiet.
