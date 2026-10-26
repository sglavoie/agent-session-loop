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
