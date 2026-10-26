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

### Changed

- Documentation: a first-run path in the README, `asl prepare` as the front
  door in the setup guide, an inspection and maintenance summary in the
  operator guide, and an accurate description of each CI lane.
- CI: the macOS suite is split across four parallel jobs, runtimes are bounded,
  a Python 3.13 job runs the portable Workbench tests, the Workbench browser
  workflow is pinned to a named runner image and triggers on the code its
  fixture server imports, and Dependabot covers GitHub Actions, the browser
  harness's npm pins and the `dev` Python dependency group.

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
