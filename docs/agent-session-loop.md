# Agent Session Loop

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
See [the recovery contract](../agent_session_loop/README.md#accepting-remote-ref-updates-after-a-paused-review).

`agent-session-loop` repeatedly invokes an agent harness against a split
session plan directory. The default short executable alias is `asl`. Each
harness turn runs one `$session-run` or `/session-run` invocation, then the
runner re-reads the plan files before deciding whether to continue.

```bash
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --max-sessions 1
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --commit-per-session
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --commit-at-end
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --note "operator reference"
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --review-harness claude
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --dry-run
bin/.local/bin/asl status ~/.local/share/agent-plans/example-plan/
bin/.local/bin/asl tail ~/.local/share/agent-plans/example-plan/
bin/.local/bin/asl sessions
bin/.local/bin/asl sessions --running
bin/.local/bin/asl sessions --recent --limit 5
bin/.local/bin/asl sessions --json
```

The package is stdlib-only and lives in `agent_session_loop/`. The executable
wrapper at `bin/.local/bin/agent-session-loop` adds this repository to
`sys.path` and dispatches to the package CLI. `bin/.local/bin/asl` is a
relative symlink to that wrapper. The long executable remains available.

## Reference parts

The full reference is split into the files below, in reading order. Section anchors resolve within each file.

- [Setup](agent-session-loop/01-setup.md)
- [Commands](agent-session-loop/02-commands.md)
- [Live Mailbox and Future-Turn Model Control](agent-session-loop/03-live-mailbox-and-future-turn-model-contr.md)
- [Live reply wait and fallback](agent-session-loop/04-live-reply-wait-and-fallback.md)
- [Command Behavior (continued)](agent-session-loop/05-command-behavior-continued.md)
- [Terminal success semantics](agent-session-loop/06-terminal-success-semantics.md)
- [Terminal success semantics (continued)](agent-session-loop/07-terminal-success-semantics-continued.md)
- [Terminal success semantics (continued)](agent-session-loop/08-terminal-success-semantics-continued.md)
- [Run-scoped guards](agent-session-loop/09-run-scoped-guards.md)
- [Repositories](agent-session-loop/10-repositories.md)
- [Session 01](agent-session-loop/11-session-01.md)
- [Assurance in metrics, retention, and consumers](agent-session-loop/12-assurance-in-metrics-retention-and-consu.md)
- [Harness Commands](agent-session-loop/13-harness-commands.md)
- [Safety Modes](agent-session-loop/14-safety-modes.md)
- [Conversation continuity](agent-session-loop/15-conversation-continuity.md)
- [JSON Output](agent-session-loop/16-json-output.md)
- [JSON Output (continued)](agent-session-loop/17-json-output-continued.md)
- [Pause Semantics](agent-session-loop/18-pause-semantics.md)
- [Autopilot Skill (`$asl`)](agent-session-loop/19-autopilot-skill-asl.md)
- [Troubleshooting](agent-session-loop/20-troubleshooting.md)
- [Plan catalog](agent-session-loop/21-plan-catalog.md)
- [HTTP API surface](agent-session-loop/22-http-api-surface.md)
- [Recovery Center payload](agent-session-loop/23-recovery-center-payload.md)
- [Frontend behavior](agent-session-loop/24-frontend-behavior.md)
- [Supervision Bridge](agent-session-loop/25-supervision-bridge.md)
- [Observational and mutation guarantees](agent-session-loop/26-observational-and-mutation-guarantees.md)
- [Redirect recovery before restarting](agent-session-loop/27-redirect-recovery-before-restarting.md)
