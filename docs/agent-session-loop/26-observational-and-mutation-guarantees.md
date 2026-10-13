### Observational and mutation guarantees

Every read the bridge performs — Workbench summary, Q&A context, lock
state, waiting-for-reply status, and every action-validation read —
passes `reclaim=False`. A dead local lock is classified `stale` and left
on disk rather than reclaimed, so a snapshot is a function of the corpus
and not of the bridge's own read order, and validating an action can
never be the thing that changes the corpus being validated. A plan that
disappears mid-scan projects as `invalid` alongside its surviving
neighbours instead of ending the scan.

A refused mutation performs zero writes and never spawns a process: the
token check runs before the mailbox lease, the control-queue append, and
the resume spawn alike. Reply text is passed straight to ASL's durable
records and appears in no response, event, operation receipt, snapshot,
or stderr line the bridge can emit.

### Cross-language wire fixtures

The protocol has two implementations in two languages, so the wire shape
is pinned in a checked-in fixture corpus rather than described twice.
Protocol 2 retired the bridge's own fixture directory into the contract
corpus, because two corpora describing the same wire is exactly the drift
a corpus exists to prevent.
`tests/fixtures/agent_mux_contract/fixtures/bridge-protocol-v2/` holds one
canonical document per message — hello, handshake accepted, handshake
read-only, snapshot, `runs.changed`, question body, operation receipt,
`operation.updated`, error response, Workbench link, redirect receipt,
redirect status, recovery options — beside documents the
schema must reject for a named reason at a named JSON pointer. The
corpus's `manifest.json` pins the protocol vocabulary (methods, the
mutating subset, events, error codes), a SHA-256 per file, and the corpus
digest the contract-set ID is derived from.

This repository is the source of truth. Agent Mux vendors a byte-identical
copy at `internal/aslcontract/corpus/` and validates the same documents in
Go with unknown fields rejected. Both sides recompute the corpus digest, so
a one-sided edit fails on whichever side drifted instead of surfacing as a
runtime mismatch against an installed pair.
`tests/test_asl_bridge_fixtures.py` additionally validates what
the `agent_session_loop.bridge_*` modules actually produce — its hello, its snapshot, its
handshake, and both new refusals — against those schemas, which is what
keeps the corpus from becoming a self-consistent fiction. The change
procedure is in the corpus's own `README.md`.

The documents are illustrative wire shapes rather than derived records:
`plan_dir`, `row_id`, and `action_token` are fixed fixture strings, not
values recomputed from a live corpus. They also cannot name the live
contract set — a digest computed over corpus bytes cannot appear in those
bytes — so they carry the placeholder `asl-amx-1.0000000000000000` and the
test substitutes the real identifier when it compares a served frame
against a checked-in one. A fixture containing the real identifier is
itself a failure.

### Client rollout order

Ship the bridge before the client. ASL is the older and more widely
installed half, so upgrading it first means a new supervision client finds
a bridge it can talk to on its first start. The reverse order is safe but
inert: a client that finds no `asl`, or one that predates the bridge
subcommand, reports the integration as unavailable and stays read-only
until ASL is upgraded. The bridge itself has no client-facing migration —
it adds a subcommand and changes nothing about `asl run`, `asl workbench`,
the mailbox, or the control queue.

Capabilities are additive and ordered, and a client feature-detects by
membership rather than by position, so a newer bridge serving more methods
than a client knows about is not a version conflict. A client asking for a
protocol other than 2 is: the server exits 2 with a stderr diagnostic and
serves nothing rather than degrading into a guess.

## Agent Mux Pairing

`asl agent-mux pairing [--state-dir DIR] [--migrate] [--json]` is the
surface `amx config asl-sync` reads before it is allowed to rewrite Agent
Mux's configuration. Pairing is a promise about a whole installation —
one executable, one contract set, one state root — and every other ASL
command that knows those things is keyed by a single plan directory
(`asl contract-check --agent-mux`, `asl migrate-state`,
`asl compact-state`). This one takes no plan argument and answers for the
root.

It reports the absolute path of the entry point that is running, the
contract-set ID this build implements, the bridge protocol it speaks, the
canonical state root, the current state schema version, and a
classification of every plan state directory under that root:

| Classification | Meaning |
| --- | --- |
| `compatible` | already at the current state schema |
| `migratable` | inactive, and upgradable in place |
| `active-incompatible` | locked by a live run whose state this build cannot pair with |
| `unprojectable` | too little run identity to build a projection from |
| `unreadable` | `state.json` is not a JSON object |
| `empty` | a state directory with no state record |

The executable is made absolute but deliberately **not** canonicalized.
The launcher on `PATH` is usually the stable name — `~/.local/bin/asl` —
pointing at a target an upgrade replaces, so following the link would
persist the path that churns rather than the one that does not. When the
entry point cannot be named at all (an embedded or `python -c`
invocation) the field is null rather than a bare `asl`, because a bare
name would put an interactive shell's `PATH` back in the loop, which is
exactly what pairing exists to remove.

### An active incompatible run refuses the whole pairing

`--migrate` upgrades every `migratable` plan in place. It is refused
outright — nothing is written anywhere under the root — while any plan is
classified `active-incompatible`, and the offending runs are named with
their plan directory, run ID, and PID.

The refusal is whole-root rather than per-plan because the promise is
whole-root: a live legacy run keeps publishing state and lifecycle hooks
that the configuration about to be written does not cover. Such a run is
never stopped and never migrated underneath. It holds its state payload
in memory and rewrites the whole file on its next transition, so an
upgrade beneath it is reverted at best; stopping someone else's run to
finish a configuration change is not a decision this command gets to
make. It names the run and leaves it to its operator.

Exit reasons are part of the contract, and each names a distinct operator
action: `ok`, `active-incompatible-runs` (stop the named runs),
`migration-failed` (a named plan could not be upgraded),
`state-root-error` (the root exists but cannot be scanned). Every outcome
presents the same keys, so a consumer branches on `reason` alone and
never on which fields are present.

## Diagnostics Export

`asl diagnostics export --agent-mux [--state-dir DIR] [--run-id ID]
[--plan-dir DIR] [--output PATH] [--json]` is the **only supported way for
another program to read ASL's internal state**. `state.json`,
`events.jsonl` and `mailbox.jsonl` are private formats: they carry
question bodies, reply text, control messages and whatever a harness wrote
into a reason string, and their shapes change whenever the loop changes.
`amx debug bundle --include-asl` invokes this command instead of parsing
any of them.

Three properties make the document safe to put in a shareable bundle.

**Bounded.** Every collection has a cap, every cap is published in the
document's own `limits` block, and every cap that bites is counted in
`omissions`. A run with a hundred thousand events contributes counts, not
records. The scan itself is bounded too, at `max_log_lines` and
`max_log_tail_bytes`, so an unrotated log cannot turn a debug capture into
a full-file read. That bound is a bound on the log's *tail*: an
append-only log is read backwards from its end, because the records that
explain a failure are the newest ones and a bounded prefix of a long log
would report on whatever the run was doing hours earlier. What was left
before the retained window is counted as a bounded omission, and
`scanned`, `rejected` and the malformed digests describe that window and
nothing outside it. The serialized document never exceeds
`max_export_bytes`; runs are dropped from the end until it fits, and each
drop is recorded.

**Deterministic.** The document carries no wall clock, no host entropy and
no iteration-order dependence — runs are ordered by the projection's
`updated_at` with the plan digest as tie-break. Over frozen inputs the
same bytes come out every time, which is what lets a bundle assert byte
equality across two captures. The `digest` field is a SHA-256 over the
rest of the document, so a consumer can prove it re-read the same
evidence.

**Control-plane only.** Field allowlists, not denylists, decide what is
copied, and a value that arrives as a container on an allowlisted key is
dropped rather than copied. Mailbox `text` and `note`, run-control
`message`, per-attempt delivery detail, prompts, model output, bridge
stderr and verification output have no path into the document at all.
What was withheld is still *counted*: a mailbox record reports
`text_chars` and `note_chars`, so a reader can tell "no reply" from "a
reply this export will not show you". The standing policy ships inside
the document as `redaction_policy`.

The export is read-only in the strict sense — no file under the state root
is opened for writing, no lock is taken, and no stale lock is reclaimed —
so a bundle can be captured while a run is live, and while it is wedged.

### What one run record correlates

| Field | Joins to |
| --- | --- |
| `plan_digest` | `sha256(plan_dir)`, the correlation key that survives a share-mode bundle's path aliasing |
| `projection` | the nested `agent_mux` block, copied through the projection contract's own field list |
| `projection_generation`, `lifecycle_source_sequence` | Agent Mux's monotonic acceptance gate |
| `delivery.records[].source_event_id`, `.request_id` | the daemon journal's `request_id` for the same lifecycle notification |
| `control.operation_ids` | bridge operation IDs, durable because a control command persists its operation ID as `command_id` |
| `receipts.closure_ids` | the closure IDs reply and resolve produce |
| `assurance` | the shared assurance ledger, by issue ID, without any finding body |
| `build.binary_sha256` | which bytes ran, next to the version string that says which release they claim to be |

Counted breakdowns — `delivery.by_status`, `control.by_action`,
`receipts.by_kind`, `events.by_name` — are **lists of labelled records**
(`{"status": "accepted", "count": 1}`), not maps keyed by the tallied
value. A consumer that redacts by key allowlist, as Agent Mux's bundle
redactor does, can vet two fixed keys once; a value-keyed map would
present every distinct status as an unknown key and lose the whole
breakdown. Each list is sorted by its label.

`--run-id` and `--plan-dir` narrow the document to one run. A projection
whose contract set this build does not implement is reported as
`foreign-contract-set` and **not** copied: this build cannot vouch for the
field meanings of a contract it does not implement.

Every unavailable input degrades to a named reason rather than to a
failure. A missing state root exports an empty but complete document, an
unreadable `state.json` still exports that plan's logs, and a state root
that cannot be scanned at all exits nonzero with
`reason: state-root-error` — still a well-formed, digested document.

Without `--output`, the command writes the document itself to stdout, byte
for byte, because that is what a consuming bundle stores; `--json` wraps
it in the standard response envelope instead. `--agent-mux` names the
consumer profile and is required: a caller that forgets to name one is
refused rather than silently handed Agent Mux's.

## Verification

Run the regression suite and compile check from this repository:

```bash
just test-fast
just test-portable
just test-all
just lint
python3 /Users/dev/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/session-resplit
just sync-skills
python3 -m py_compile agent_session_loop/*.py bin/.local/bin/agent-session-loop bin/.local/bin/asl
just build-ccl
just all
just verify-asl-install
```

The test suite is organized as subsystem modules (`tests/test_asl_*.py`,
stdlib `unittest` only) sharing fixtures through `tests/support.py`.
`just test-fast` runs the unit/schema/parser modules; `just test-all` runs
everything — including the helper-script suites under `skills/` — as isolated
parallel shards via `tests/run_suite.py`, which also fails if a test module is
missing from its shard lists. Individual modules run directly, for example
`python3 -m unittest tests.test_asl_verification_run_and_observers -q`.

`just test-portable` is a third lane cut for CI rather than for a developer. It
runs `PORTABLE_MODULES`, the subset of `FAST_MODULES` that spawns no external
process at all — no `git`, no shell script, no launchd domain, no harness
executable, no Kitty. Membership is that mechanical rule rather than a judgement
call, and the list is hand-curated, so `tests/test_asl_docs_*.py` pins it as a
strict subset of `FAST_MODULES`: a module renamed or dropped from the fast lane
cannot keep the portable lane green by running a name `unittest` would refuse to
import. This is the lane the Linux CI job gates on. Everything that needs the
real tools — the Git-backed repository evidence, the shell helpers, the launchd
maintenance definitions, the harness adapters — is left to the macOS job, which
runs `just test-all` on the platform the pair is actually released against, so
the portable lane never reports a pass it did not earn.

The Workbench shell is checked from two directions, because the claim being made
is that one front end serves two deployments and half of that claim is only
observable in a browser. `tests/test_workbench_shell.py` runs in the integration
lane: it seeds one plan corpus, binds both providers to real loopback sockets
over it, and asserts that the two serve byte-identical assets and answer the
same questions about the same plans. `tests/workbench_browser/shell.spec.js`
drives that shell with Playwright at desktop and phone widths against both
deployments in turn — the queue, the responsive collapse, every detail pane,
keyboard navigation, and both historic link formats. Two of its claims are about
the refresh poll rather than the layout, and they are there because neither is
visible from a request: a poll that redraws the same answer performs no page
scroll at all, and a poll that shortens the page still puts the operator back.
The unconditional restore that came before ate taps on a phone, where WebKit
reads a document scroll between `touchstart` and `touchend` as a pan and throws
the tap away. `overview.spec.js` and `interactions.spec.js` sit beside it over
the same fixture: the overview's sections, facets, history pane and the banners
for what a page could not see, and the controls bar driven up to the edge of
every mutation and no further. All three run as
`npm --prefix tests/workbench_browser test`, against a fixture server the
Playwright config starts itself (`fixture_server.py`, which binds the two
deployments on the fixed ports recorded in `scenarios.json` so the config and
the fixture cannot disagree about where they are).

That harness is development-only and deliberately sealed off from what ships:
`tests/workbench_browser/package.json` declares an empty `dependencies` and
carries `@playwright/test` as a dev dependency alone, which
`tests/test_workbench_shell.py` asserts rather than trusts. Neither deployment
needs Node — the shell is native ES modules served by Python — and the Pi
especially must not acquire a JavaScript toolchain because a test harness drifted
into the runtime. `npm --prefix tests/workbench_browser run test:install` fetches
the browser engines on a machine that has none; nothing else in the repository
depends on them being there, so a checkout without Playwright still runs every
Python lane.

`just reliability-stress` is a fourth lane, and it judges what the other three
cannot: whether a result still holds when the machine is hostile. It keeps three
lists rather than one. The correctness scenarios are the ones observed failing
under load or as the calendar moved, and they synchronize on run state and pin
their own clock, so neither contention nor the calendar may change their outcome.
The date-sensitive subset of those is swept again across UTC, a whole-hour offset,
a three-quarter-hour offset, and a southern-hemisphere DST zone, because a path
that reached for a naive local clock reads differently under each. The third list
is the crash, race, and cancellation scenarios — the fault groups of
`tests/test_asl_redirect_adversarial.py` alongside the escalation-timing, blocked
transport, live termination, and serialization groups of
`tests/test_asl_redirect_control.py` and `tests/test_asl_redirect_continuation.py`.

That third list is separate because contention is the only condition under which
most of its claims can fail at all: a kill that has to land on a deadline, a latch
two threads reach at once, a journal several producers append to, an expiry and an
acceptance racing to answer one request. It repeats in isolation and again as
concurrent shards while the full suite saturates the machine, and it is
deliberately excluded from the final phase, which measures end-to-end CLI latency
on a quiet machine. A wall-clock budget has nothing to say about a deadline a test
pins against its own injected clock; reading those scenarios with a stopwatch
would measure the test's own patience and report it as a regression.

The adversarial module is organised by fault rather than by the module under
test, because each fault crosses several of them: slow storage, a blocked pipe, a
completion race, concurrent feedback, a stale process identity, partial
multi-repository work, a review repair, a stopped supervisor, missing history, and
an expired submission. Three of its groups drive real child processes and are
POSIX-only, which is why it is registered in `FAST_MODULES` and not in
`PORTABLE_MODULES`. Two of its claims are worth naming here because each reads
like a defect until the ordering is spelled out. An acceptance whose journal
append stalls past the escalation deadline must force-stop nothing and report its
decision window skipped, then force-stop the worker once the append lands — the
promise is made durable before it is kept, never the other way round. And an
answer that arrives after the turn has already settled records no refusal at all:
the drain returns as soon as it observes the settled turn, so the request stays
`submitted` for `redirect-status` to report rather than being rewritten by a turn
that no longer exists.

The bridge's cross-language contract is checked from both sides.
`tests/test_asl_bridge_fixtures.py` runs in `just test-fast` and verifies that
`tests/fixtures/agent_mux_contract/` still describes what
the `agent_session_loop.bridge_*` modules produce and that the manifest digests match the
files beside them. The other half runs in the client repository: after copying
the corpus across unchanged, `just asl-integration` in Agent Mux launches the
installed `asl bridge serve --stdio --protocol 2` against a throwaway plan and
state root and checks the hello, the contract-set ID against the vendored
corpus, the method and mutation lists against the manifest, the handshake and
its mutation gate, snapshot decoding, body-free action and receipt shapes, the
Workbench URL shape, and a clean exit. That recipe is opt-in there and fails
rather than skips when ASL is missing or incompatible, so an unrun check is
never mistaken for a passing one.

Releases of the pair are gated from the client side by
`just paired-release-gate asl_root=… signing_key=…`, which runs this
repository's `just test-all`, `just lint`, `just reliability-stress`,
`just all`, and `just verify-asl-install` alongside Agent Mux's own gates,
compares `tests/fixtures/agent_mux_contract/` against the vendored copy byte
for byte, and installs the two halves in the only safe order — ASL first,
because the client's install restarts the daemon and a daemon restarted against
a stale ASL says nothing about the pair. The result is a deterministic JSON
evidence record: both source revisions, a SHA-256 of `bin/.local/bin/agent-session-loop`
and an ordered digest over `agent_session_loop/`'s sources, the contract-set ID,
a SHA-256 of `tests/fixtures/agent_mux_contract/manifest.json`, the
OS/Kitty/harness versions, and every command with its exit code and duration.
It is signed with `ssh-keygen -Y sign` under the namespace
`agentmux-asl-release` and both repositories are tagged with the same pair ID.

The manifest hash is recorded explicitly because `manifest.json` is excluded
from every corpus digest: a manifest that drifted while the schemas held still
is invisible to every digest check and visible only to the byte comparison and
to this hash. The tree digest over `agent_session_loop/` exists for the
symmetric reason — ASL ships as Python, so there is no compiled artifact whose
hash pins the code that will actually run, and hashing the wrapper alone would
pin a twenty-line launcher.

Use a synthetic plan directory for manual smoke tests so generated state, locks,
and logs do not touch an active project plan.

Manual smoke checklist:

- Create a synthetic split plan, run with a fake harness that fails one turn,
  confirm the run pauses, then rerun successfully and confirm the failed and
  successful turns are preserved under different `runs/<run_id>/` directories.
- Run a multi-session synthetic plan from a real terminal, press `h`, press `p`
  while a session is active, and confirm the run exits successfully after the
  current session with `reason: stopped`, a consumed `control` payload, and
  `run_control` events visible through `tail`.
- Repeat the text TTY run with `s`, `t`, and `1`/`2`/`3`; confirm `s` only
  prints stderr status lines, `t` first shows recent active stdout/stderr
  context and then streams new log lines while enabled (without re-emitting
  heartbeats, session start/finish, control, status, or commit lines), and
  later turns use the selected harness.
- Repeat the text TTY run with `--commit-per-session` and confirm the
  per-session commit runs before the stop-after-current request is consumed.
- Queue `asl control stop-after-current` while the last session of a
  `--review-harness` run executes, and confirm the run exits `reason: stopped`
  with `review_skipped_reason: stopped`, no generated review session file, and
  `asl status` reporting `review_resume.action: initial-review`.
- Run a text TTY plan with `--commit-at-end`, stop before all sessions are
  complete, and confirm no final `commit-0001-end` artifact is created.
- Run `run --json` and confirm stdout is the final JSON payload, stderr is
  quiet, and keyboard controls are not enabled.
- Make a required plan file unreadable or remove `context.md`, then confirm
  `run --json`, `status --json`, and `doctor --json` emit JSON failures without
  Python tracebacks.
- Run `doctor --json` against a valid synthetic plan and confirm it reports the
  resolved harness executable, prompt, and invocation argv.
- Create a synthetic plan with sessions `1`, `2`, and `3`; run
  `$session-resplit <plan-dir>/ --session 2 --parts 2`, rerun
  `$session-expand <plan-dir>/`, then confirm `asl status` and
  `asl run --dry-run --json` report `2.1` as the next pending session.
- Create a synthetic active lock, confirm `unlock --json` reports
  `reason: active-lock`, then run `unlock --force --json` and confirm the lock
  is removed.
- In terminal A, start an Autopilot-supervised synthetic two-session plan. In
  terminal B, send multiline `asl inbox guidance`, then confirm the next worker
  prompt receives JSON-escaped guidance and `asl inbox read` acknowledges only
  that snapshot.
- While the first turn is active, queue `asl control set-model`; within the
  five-second control-poll cadence confirm the runner records
  `implementation-model-updated`, the active subprocess is unchanged, and the
  second implementation invocation receives the override. Start a new runner
  process and confirm it returns to the original flag/native default.
- Have the worker emit a question and confirm the supervisor discovers one
  `QUESTION` record. Race two `asl inbox reply` commands, confirm exactly one
  succeeds, and verify the answer continues the same session without a
  `run_paused` event or pause-metric increment.
- Repeat without answering: confirm the bounded wait becomes one ordinary
  terminal pause after timeout. Repeat with `--no-mailbox`: confirm no lease is
  written, no live protocol is added to the prompt, and the historical
  pause/`asl run --reply` path remains unchanged.

Supervision bridge:

- Pipe a hand-written NDJSON session into `asl bridge serve --stdio --protocol
  1` against a synthetic state root: confirm `hello` arrives before any input
  is read, that a `runs.snapshot` of a paused plan carries the question ID and
  unanswered count but no question text, and that `question.get` is the only
  way to obtain the body.
- In the same session, send a malformed line, an unknown method, and a frame
  past 1 MiB. Confirm each is answered with its own code and the server keeps
  serving; only end-of-stream should end it.
- Take an `action_token` from a snapshot, mutate the plan (answer the question
  elsewhere, or let the run finish), then submit the stale token. Confirm the
  refusal names the specific race, and that no mailbox record, control-queue
  entry, or spawned process resulted.
- Request `--protocol 2` and confirm exit code 2 with a stderr diagnostic and
  nothing on stdout.

Workbench browser workflow:
