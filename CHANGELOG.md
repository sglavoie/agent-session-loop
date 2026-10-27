# Changelog

Notable changes to Agent Session Loop (`asl`), its portable Workbench
(`asl_workbench`) and the ASL skills. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version is
`__version__` in `agent_session_loop/__init__.py`. Releases are cut with Agent
Mux's paired release gate, which tags both repositories (see "Releasing the
pair" in [docs/asl-guide.md](docs/asl-guide.md)); when cutting one, bump the
version, move the Unreleased entries under it, and date it.

## [Unreleased]

A polish and hardening round after the public release.

### Fixed

- Workbench: replaying an older request, reusing its id for a conflicting action,
  or submitting an unrelated action no longer supersedes uncertain operations.
  Those records remain open to late confirmation; only a new operation in the
  same scope can replace them.
- Quota retry hints preserve fractional timestamps and their timezone offsets,
  and dotted `a.m.`/`p.m.` times remain parseable, so cooldowns and supervisor
  waits use the provider's reset time. Local clock hints use the UTC offset
  at the reset time when a daylight-saving transition intervenes.
- Workbench: concurrent operation reports and recovery sweeps no longer turn
  late reports into upload failures; stale refusal receipts cannot overwrite
  acceptance, and expired queued copies cannot expire dispatched operations.
- Workbench: a remote correction refused before persistence is recorded as
  rejected, so retries no longer report acceptance and later corrections remain
  possible; remote admission preserves the actual conflict or uncertainty
  reason; deeply nested JSON lines no longer block operation-journal reads
  and writes.
- Run loop: closed lifecycle, transport and durability gaps; a refused run or a
  stale teardown no longer breaks a live run; plan identity, locks, durable
  writes, timestamps and git path parsing are hardened.
- Workbench: retries, refreshes and errors behave as described; the server
  refuses rebound `Host` headers and unread request bodies; reads are hardened.
- CLI consistency: mistakes are reported from the command that was run, a
  missing plan is reported the same way by every command, and non-finite
  numeric input is refused.
- Install checks every destination before linking and writes context files
  atomically.
- Run loop: SIGTERM and SIGHUP tear a run down like Ctrl-C; the host-rename
  rule applies to every foreign-host check; a run whose lock pid was reused is
  recorded as abandoned; a failed harness spawn answers a pending abort; turn
  idle time uses the monotonic clock.
- Readers tolerate bytes that are not UTF-8 and pathologically nested JSON:
  verification checks, last-message files, control, metrics, recovery and
  queue-drain readers; the control queue drain now fullsyncs its truncate.
- Workbench: one outstanding mutation per plan holds under concurrent
  requests; a reply or correction stays with the plan it was sent for; the
  connector lock is published atomically; malformed, stalled, short or
  over-deep request bodies, bad `Content-Length` values and out-of-range
  reads get 4xx instead of 500; unsupported standard methods get 405;
  offset-less times read as UTC in the browser; keyboard focus and refresh
  announcements are kept where they belong.
- CLI: `asl workbench` group flags no longer vanish before a non-serve leaf;
  `split` reports a missing source as a JSON `input-error`; refusals read as
  sentences, numeric refusals share one wording and refused flag values report
  `input-error`; help placeholders name the kind of value; `progress` accepts
  `--state-dir`.
- Supervision cursors are written with mode `0600` like the other mailbox
  files, and `install.py --home` names the renderer for that home.
- Plan authoring: a code fence closes only on a run at least as long as its
  opener, so a quoted example no longer adds phantom sessions; a leading BOM no
  longer hides Session 1; sessions out of ascending order, and contracts for
  sessions the plan lacks, are refused; non-UTF-8 input is an `input-error`
  rather than a traceback; the plan title skips fenced comments; a `|` in a
  title keeps the manifest table intact; `## Repositories` tolerates trailing
  whitespace; a heading such as `### Session cookies` is content; re-splitting
  or re-preparing reports `already-split`; and `prepare` authors the split
  exactly as `split` does, harness skill prefix included.
- `asl commit`: the staged diff is captured the same way whatever the user's
  diff configuration, and quoted, non-ASCII and renamed file names parse; a
  submodule pointer bump commits despite dirty content inside the submodule;
  `snapshot-drifted` lists paths verbatim.
- Storage: `compact-state --apply` gives each failed check's output its own
  artifact instead of overwriting one with another, and keeps a torn
  `events.jsonl` line rather than failing; dry runs of `maintenance run` and
  `migrate-state` write nothing, and leave a dead-owner lock in place.
- Reporting: `status`, `tail`, `review-verdict` and `progress` tolerate
  non-UTF-8 bytes; `status` text says how the recorded run ended; `tail`
  says a run stopped when it neither finished nor paused, keeps the run's
  outcome in its recent window, follows truncated events and logs, prints
  whole log lines, and refuses an events file it cannot open; `progress
  --tail` follows a rewritten journal and refuses `--json`; `review-verdict`
  text prints every finding in full; counts of one read in the singular; tail
  and sessions times carry their UTC offset; `doctor` shows its required
  arguments and refuses a bad harness as JSON; `sessions --limit 0` is refused.
- Overseer: an undecodable or over-deep state, lock, ledger, claims file or
  Agent Mux answer is reported or refused instead of crashing the fleet; a
  plan resolves to the record that proves its identity, not a stray copy;
  context and ledger refusals are exit-2 envelopes; a bounded `watch` keeps
  its leases so `act` can use them, and `ownership_lost` asks for a re-watch
  rather than a human; `decide` refuses when Agent Mux withholds the context
  grant; expansions keep their 4000-character ceiling; an unknown `--plan`,
  another plan's incident and an unschedulable duration are refused; partial
  watch sweeps print their warnings.
- Supervision: `outbox supervise` wakes only for the current run's
  still-open questions; an over-deep mailbox or liveness line and an
  undecodable `questions` input are refused rather than crashing; a LOST
  report says why the runner state is not fresh; mailbox commands name the
  sessions-label lookup for an unknown plan; reply and resolve report
  `waited` and `timed_out`, and a reply no run can consume skips its wait;
  the supervisor guide lists every placeholder and refuses a bad harness as
  JSON; `inbox acknowledge` refuses a wrong attempt token.
- Bridge: `workbench.ensure` needs a contract-matched handshake; a reused
  operation id that names a different request is refused; one unreadable or
  mistyped plan state no longer empties the projection; over-deep frames,
  handler failures and over-long request ids are answered instead of ending
  the bridge; a closed stdout exits 0. Redirect text names its refusal
  reason and `--ttl` is checked first; over-deep redirect journal lines and
  torn event lines no longer block redirect status or scope recovery.
- Contracts and readiness: a non-string check type, a huge timeout and
  non-UTF-8 session files or regex targets are refused instead of crashing;
  a missing check cwd or `--cwd` root is named rather than blamed on the
  command, and contract-check and preflight diagnose it; `verify` reports an
  absent session as `invalid-session`; preflight calls a registered plan's
  JSON typo malformed and points at the file line; `baseline --json` and
  `expand-ingest` report bad input as `input-error` and `plan-not-found`;
  tilde and longer fences around Verify JSON are read, CRLF files keep their
  newlines, a non-integer validator version is refused, and command details
  are shell-quoted.
- Housekeeping: `harness-compat` survives non-UTF-8 output, finishes when the
  CLI exits rather than when its pipe closes, and checks each harness once;
  `metrics` decides first-pass from the whole turn history, ignores
  non-finite durations and clamps an oversized window like `gc`; only
  digest-named state dirs are scanned, so `archive/` is no longer counted as
  a plan; `gc` counts read in the singular; `diagnostics export` names
  itself and removes its temporary file on failure; `plans` shows a
  never-run plan's activity and reports a bad `--readiness` as
  `input-error`; an empty PLAN_DIR is refused.
- Review loop: refreshing a pending review's contract finds its headings only
  as whole lines, so a finding that quotes one no longer duplicates the verify
  recipe; over-deep `final-review.json`, repair journals and state, huge
  `Status:` counts and a float artifact version are refused instead of
  crashing; a dirty path holding a backtick reads back intact; an
  `ASL_REVIEW_GUARD_EXCLUDE` refusal names the variable; one finding reads in
  the singular.
- Assurance: damaged pending-audit records, ledger lines, index caches,
  manifests and state are skipped or refused rather than crashing every
  command; `assurance plan` reports an unreadable ledger as such; an unknown
  `--plan` is `plan-not-found` and a mistyped `--audit-harness` is refused
  before any run is marked unusable; Ctrl-D at a confirmation cancels; lone
  surrogates and over-long issue ids are refused; counts read in the singular.
- Redirect and repositories: `register-repositories` refuses non-UTF-8
  manifests and stdin, a missing `--cwd` and an empty PLAN_DIR, and gives
  checkouts that differ only in case distinct aliases; the repository scanners
  use the plan parser's fence rule; NUL-bearing paths, non-UTF-8 or over-deep
  event, last-message and redirect evidence files no longer crash; a redirect
  refused before anything was persisted no longer says it was settled.
- Turns and conversations: a lone surrogate in harness output is logged as
  U+FFFD instead of losing the turn; output still counts as activity when its
  live log append fails; a failed launch drops its interactive surfaces even
  if its teardown write fails; a post-result hang after a CPU-progress
  clearance can be recovered again; a repair child whose commit targets could
  not be read records `failed`; ledger capture and relocation failures are
  recorded and reported, and a NUL-bearing ledger path is an invalid record.
- Workbench: a remote submission discarded before it left, or refused by the
  connector with a receipt, is settled instead of blocking the plan's later
  mutations forever; a failed migration backup leaves no wedged
  `migration.json` or staging directory; a malformed legacy project list no
  longer rolls back the store upgrade; an overflowing history time or
  duration is left unplaced instead of ending the import.
- Follow-ups: the Claude, Codex and Copilot readers share one JSON-line
  decoder that turns lone surrogates into U+FFFD and treats over-deep lines as
  plain text; `status`, `preflight`, `baseline`, `contract-check`, `verify`
  and `doctor` refuse an empty PLAN_DIR; `split --inline-stdin` refuses
  non-UTF-8 input like `--plan-file`; Ctrl-D declines `asl commit`'s prompt;
  oversized transport numbers and counts are refused fields rather than 500s;
  a repository path holding `|` is refused; a CRLF review session keeps its
  line endings when re-scoped.
- Run loop: a turn that times out or is interrupted takes back a completion
  claim nothing verified, as a nonzero exit already did, so a relaunch no
  longer skips that session; a check whose location escapes its root during
  verification reverts the session and pauses instead of ending the run with
  the claim intact; a bare 502/503/504 in a turn's output (a line number, a
  port) no longer makes an unknown failure retryable.
- Supervision and bridge: `outbox supervise` drops a carried question whose
  wait ended during the carry delay instead of reporting an unanswerable
  ACTION; the bridge emits `runs.changed` to a client that never asked for a
  snapshot; a CRLF frame of exactly the ceiling is accepted and an
  unterminated last frame over it is refused; `outbox wait --since` above
  the mailbox's integer ceiling is an `invalid-since` input error.
- Plans and evidence: the scope-field scanner and the assurance draft's
  heading check close fences by the plan parser's rule; single-repository
  baseline evidence ignores a plan directory inside the repository, so it is
  not stale the moment it is written; commit-message diff counts include hunk
  lines that start with `---` or `+++`.
- Workbench: the remote queue sorts before it pages; the label sort orders
  rows by the label they show; a `#entry=` link opens the history pane and
  leaving it clears the missing-entry note; `limit=0` is refused.
- CLI: `harness-compat --strict --json` reports `ok: false` when it exits
  nonzero; `maintenance` flag values are refused as JSON `input-error`s like
  `gc`; help shows every flag default as `(default: X)` and wraps group
  descriptions; `--allow-not-loop-ready` help says what it bypasses; missing
  files are reported without raw `[Errno 2]` text; text output says yes/no
  rather than `True`/`False`; more refusals echo the refused value.
- Documentation agrees with the code on the mailbox schema version and files,
  the Claude command line, redirect refusal codes, the Workbench
  `question-reply` and `history` routes, the full event list, and symbols
  that moved to sibling modules.
- Guards: a protected path the worker changed and then committed is convicted,
  and HEAD moving without a readable path list leaves the guard unavailable
  rather than passing; the legacy `.` qualifier names the repository top level
  in both the pre-tool hook and postflight, so a run from a subdirectory no
  longer splits them; a retry merge lets the newer attempt's view of an
  untracked path win, so a file the retry deleted is not staged.
- Repository evidence: changed tracked paths are recorded verbatim rather than
  C-quoted; an unresolved merge conflict, an untracked embedded repository and
  a prunable linked worktree are fingerprinted or skipped instead of leaving
  every turn unavailable; edits inside an already dirty submodule move the
  fingerprint; scope recovery refuses to checkpoint an embedded repository.
- Harnesses: the Claude stream splits records on newlines only, so U+2028,
  U+2029 and U+0085 inside a record no longer tear it; the Claude prompt is
  written from a thread so the turn timeout and abort bound it; an ACP request
  with a malformed option kind, or one whose handling fails, is still
  answered; the Codex app-server decoder flushes a final unterminated line;
  failure classification ignores Claude's model narration.
- Workbench: a stranded pending or dispatched remote operation is settled at
  startup and on each sweep, and a new submission supersedes an `unconfirmed`
  one once the plan is idle, so one lost acknowledgement cannot lock a plan;
  connector freshness no longer counts the corpus walk twice; a new plan's
  first run that fails between observation cycles alerts; future-dated
  observations are refused as `future-observation`; a history entry must name
  its project.
- Operations: a maintenance dry run judges the log-body budget after
  compression, as `--apply` does; `tail` no longer splits a line still being
  written; a timestamp the local zone cannot hold renders at its stored
  offset; a context file that is not UTF-8 is reported as unreadable by
  `startup-context` and install verification; `asl gc --json` reports
  `ok: false` when a removal fails; a baseline check shared by sessions is
  budgeted once; `asl metrics` leaves its own trace record out of
  `records_read`; the paused resolver route is given as
  `asl run --resume --reply` throughout.
- Continuation: each run gets its own handoff-correction budget instead of
  inheriting attempts earlier runs spent; a repair group refused as exhausted
  no longer counts its never-created child as one more attempt.
- Mailbox: `asl ask --wait` ends the wait it opened when it is interrupted,
  fails mid-wait, or is sent SIGTERM or SIGHUP by a harness timeout, so a reply
  is no longer accepted for a question nobody is listening to.
- Assurance and verification: a malformed or non-UTF-8 baseline cache reads as
  empty instead of crashing `preflight`, `baseline` and `prepare`; a stray
  byte in `events.jsonl` spoils only its own line for the verification and
  terminal readers; catch-up dates an abandoned run by its start, not by when
  its death was noticed, so a run that predates the newest resolution is
  skipped as `superseded-generation`.
- State: a fresh run checks the default review-fix cycle cap against
  `--max-review-fix-cycles-ceiling`, as `--resume` already did; an over-nested
  result or state file no longer crashes `gc`, `compact-state` or `status`; a
  failed write during `migrate-state --apply` is reported, with `relocated`,
  instead of a traceback.
- Plan preparation: a cached validator correction whose replacement extends its
  original text is not re-applied on every `prepare --resume`; a workspace
  rebind keeps baseline evidence current for a plan inside a registered
  repository and no longer prints `/private/private/...` paths; status sync,
  manifest row edits and the readiness hash skip fenced example tables (a plan
  marked loop-ready with such a table reads stale once and needs
  `asl preflight --mark-loop-ready` again).
- Overseer: `verify` and `resume --session-id` find the session a paused run
  stopped on from its last turn, since a paused run has no turn in flight, and
  the progress token now changes when a run pauses on a later turn; a ledger
  write checks its grant and stamps its policy epoch inside the write lock, so
  a concurrent policy change can no longer leave old evidence unsealed under
  the new epoch.
- Supervisor audit: a list or object where an id or status belongs no longer
  stops evidence collection; review-verdict retries are weighed against each
  review cycle's own budget, as the runner spends them; camelCase secret keys
  such as `accessToken` and URL passwords holding an `@` are redacted; a kept
  `new` finding is no longer attached to the issue a duplicate of it chose.
- Run loop: `asl control redirect` and a Workbench correction retry refuse text
  that is not valid UTF-8 instead of raising; a repair group whose name holds
  a backtick or a line break keeps its attempt count (line breaks collapse to
  spaces); a live pause that follows a no-op
  `RUNNER_PAUSE: none` line is seen mid-turn.
- Plan files: readiness parsing and rewrites skip fenced examples (a plan
  marked loop-ready with a fenced readiness example reads stale once and needs
  `asl preflight --mark-loop-ready` again); form feeds and Unicode line
  separators survive readiness rewrites and expansion references and no
  longer start a readiness line the readiness hash skips; a
  non-integer plan-metadata version, and a non-UTF-8 plan metadata file,
  transaction journal or prepare validation file, are refused instead of
  crashing `status`, `preflight` and `prepare --resume`.
- Reporting: `asl metrics` tolerates a list or object turn index or phase;
  `asl diagnostics export` survives a non-string projection timestamp, drops
  NaN and Infinity so it stays strict JSON, and caps tallied event names;
  `asl install verify` reports undecodable `amx doctor` output as an advisory.
- Assurance: a reconcile manifest that both merges into an issue and rules on
  it keeps its ruling and re-applies cleanly; a finding located by a line range
  such as `file.py:120-140` matches other sightings of its file.
- Agent Mux contract: a boolean no longer satisfies a `const` or `enum` that
  names a number, nor `0` a `false`.
- Commit messages: a conventional message whose body ends in an error-shaped
  JSON example is no longer mistaken for an API error.
- Workbench: the archive retention window is capped at 36500 days instead of
  crashing archive and review, a non-UTF-8 `archive.json` is skipped with a
  warning, and a plan whose state cannot be archived is put back rather than
  orphaned; `asl workbench connect` refuses an origin with an unusable port; a
  launch reply that cannot be encoded is refused before spawning as
  `invalid-reply`, and a failed
  reply write always stops the spawned run; transport frames and history lines
  nested too deeply are malformed (400) rather than a crash or a stalled
  import; identifiers, keys and pause reasons ending in a newline are refused;
  an incomplete catalog generation publishes without a false refusal warning.
- Repair budgets: an exhausted review-verdict repair needs inspection (pause
  policy v6) instead of being relaunched to burn another review turn; the
  verification repair budget and the repair packet's attempt number count only
  failures that graded a check, not host refusals or review-verdict
  exhaustion pauses.
- Run controls: an abort whose turn a turn timeout or an interrupt ended is
  recorded as `abort-turn-preempted`, and `asl control abort --wait` exits 1,
  instead of crediting the worker with compliance; the bridge snapshot reports
  a current-format wait's ceiling deadline and span instead of null.
- Workbench: the Pi refuses a second launch or reply while the first is still
  pending behind a mutation in another scope; a catalog plan one page could not
  publish is no longer retired when a later page closes the same generation.
- `asl commit --message-file` keeps the operator's own title and opening
  paragraphs instead of cutting everything above a later title-shaped line such
  as `note:`; prepare corrections stay blocked inside the Handoff section when
  form feeds or Unicode line separators sit above it.

### Changed

- Documentation: a first-run path in the README, `asl prepare` as the front
  door in the setup guide, an inspection and maintenance summary in the
  operator guide, and an accurate description of each CI lane.
- CI: the macOS suite is split across four parallel jobs, runtimes are bounded,
  a Python 3.13 job runs the portable Workbench tests, the Workbench browser
  workflow is pinned to a named runner image and triggers on the code its
  fixture server imports, and Dependabot covers GitHub Actions, the browser
  harness's npm pins and the `dev` Python dependency group.
- Documentation agrees with the code on mailbox counts, Autopilot's
  `worker_hung` stop, `--strict` exit codes, the harness config path and the
  bridge's interventions; CI checkouts drop their persisted token, Playwright
  browsers are cached, and the harness-compatibility job triggers on every
  module its verdict depends on.
- Documentation agrees with the parser on command synopses, `supervisor-guide`
  JSON keys and leaf-only help groups; the bridge release check requests the
  retired protocol, and the mental-model citations point at current lines.

## [0.2.0] - 2026-10-23

The release published as a standalone repository.

### Added

- Built-in `asl commit`, replacing the external commit tool, with a bounded
  message turn and scoped commits bound to the snapshot their message describes.
- Evidence-based planning guardrails for `asl prepare` and the planning skills.
- Conversation continuity across turns for Codex (including the app-server
  transport), Claude and Copilot (over the Agent Client Protocol), and live
  delivery of supervisor guidance into a running turn.
- Operator redirects of a running turn: admission, forced stop of a worker that
  ignores one, crash recovery, and a CLI to submit and reconcile them.
- Run-scoped command and protected-path guards, and guarded recovery for
  external workspace changes.
- `asl overseer` and the `asl-overseer` skill for fleet supervision, with an
  evidence ledger, watch leases and guarded actions.
- Settlement of work a recoverable pause retained, including commits for it.
- The portable `asl_workbench` package: an installable service with a remote
  observation store, an outbound connector, versioned transport with role-bound
  credentials, and overview and history panes in the shell.
- The ASL-owned Agent Mux compatibility contract, the protocol 2 bridge
  handshake, scheduled storage maintenance, diagnostics export, execution
  timing metrics and Spark routing for eligible sessions.

### Changed

- Oversized modules and test suites were split into focused modules, and the
  ASL reference into per-topic part files.
- The project moved into its own repository with CI on Linux and macOS.
