### Recovery card payload

Recovery cards are deterministic. It recomputes a diagnosis from
plan files, runner state, lock state, commit artifacts, verification
payloads, and preflight repair hints on every refresh; no harness/model
is called to summarize failures, and no recovery state is persisted.

`recovery_card_models.RECOVERY_KINDS` is the source-of-truth vocabulary:
`none`, `review-continuation`, `reply-needed`, `not-loop-ready`, `verify-failed`,
`no-contract`, `malformed-contract`, `stale-lock`, `stale-state`,
`commit-preflight`, `commit-failed`, `conversation-recovery`, and
`generic`. V1 maps common structured blockers to concrete cards and falls
back to `generic` for unknown failures.

`conversation-recovery` covers both pauses that hand a decision to a
person rather than to a retry — `conversation-recovery-required` and
`redirect-successor-blocked` — and it is keyed on the decision
`intervention.recovery_decision` returns rather than on the pause reason,
so a later pause mapping to an existing decision needs no new card. It is
a read-only card by construction: everything it offers is evidence and
copyable commands. That is not a gap. `fresh-conversation`'s authorization is one-use and
discards the agent's accumulated context, and
`predecessor-not-stopped` has no authorization at all, so anything that
performed either would be the wrong shape for the decision. The two
reasons previously fell back to `generic`, whose advice — inspect, then
relaunch — is actively wrong here: relaunching without the one-use
authorization re-derives the same pause, and relaunching beside an
unstopped predecessor is the thing the second pause exists to prevent.

A full card, `RecoveryCard.to_payload()`, contains:

- `plan_dir`, `kind`, `title`, `summary`, `severity`, and `recoverable`.
- `review_resume`, identical to status/run JSON. A `review-continuation` card
  uses it to show historical materializations, the minimum required cap,
  artifact validation errors, or the ready `materialize-repairs`,
  `run-repairs`, and `rereview` action.
- `run_id`, `session_id`, `turn_index`, and `commit_index` when a
  selected turn or failed commit can be identified.
- `evidence` rows (`label`, `value`, `detail`, `severity`) for pause
  reasons, readiness gates, failed checks, lock PIDs, stale-state
  reasons, commit statuses, harness config paths, and warnings.
- `commands` (`label`, `command`, `description`) for copy-only shell or
  harness repairs such as `asl preflight <plan-dir> --mark-loop-ready`,
  `$plan-preflight-iterate <plan-dir>/`, `asl verify <plan-dir>
  --session <id> --json`, and `asl preflight <plan-dir> --json`.
- `artifacts` (`label`, `path`, `kind`, `source`) for existing
  `state.json`, `events.jsonl`, `progress.md`, stdout/stderr,
  `result.json`, failed commit files, related config files, and—when review
  continuation is relevant—`final-review.md` plus `final-review.json`.
- `warnings` legacy strings plus `warning_details` structured entries
  (`code`, `message`, `severity`). State-read warning codes are
  `state-missing-race`, `state-unreadable`, `state-malformed-json`, and
  `state-invalid-json-shape`.
- A nested `compact` object matching the additive `recovery` summary
  each `asl plans --json` entry carries. Compact payloads intentionally
  expose only `warning_count`, not the full `warning_details` list.

`evidence`, `commands`, and `artifacts` are uncapped, expected-small,
best-effort arrays. ASL does not impose a runtime item limit on
them; clients should render whatever the diagnosis returns and tolerate
empty lists. Artifact paths are best-effort pointers into state or
artifact directories and can be missing by the time an operator inspects
the card.

Commands in `commands` are copy-only repair guidance. ASL never
executes those commands on a card's behalf.

### Recovery payload examples

These minimized examples show the stable shape custom clients can branch
on. Real cards include the full field set described
above, plus nullable IDs such as `run_id`, `session_id`, `turn_index`, or
`commit_index` when they are available.

`review-continuation` with an insufficient absolute cap:

```json
{
  "kind": "review-continuation",
  "severity": "warning",
  "recoverable": true,
  "review_resume": {
    "action": "materialize-repairs",
    "blocker": "higher-cap-required",
    "historical_materializations": 2,
    "minimum_required_cap": 3
  },
  "artifacts": [
    { "label": "Final review", "path": ".../final-review.md" },
    { "label": "Structured review", "path": ".../final-review.json" }
  ]
}
```

`none`:

```json
{
  "kind": "none",
  "severity": "none",
  "recoverable": false,
  "evidence": [],
  "commands": [],
  "artifacts": [],
  "warnings": [],
  "warning_details": []
}
```

`reply-needed`:

```json
{
  "kind": "reply-needed",
  "severity": "warning",
  "recoverable": true,
  "session_id": "7",
  "commands": []
}
```

`not-loop-ready`:

```json
{
  "kind": "not-loop-ready",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "Readiness", "value": "not-loop-ready" }
  ],
  "commands": [
    {
      "label": "Repair readiness",
      "command": "asl preflight <plan-dir> --mark-loop-ready"
    }
  ]
}
```

`verify-failed`:

```json
{
  "kind": "verify-failed",
  "severity": "error",
  "recoverable": true,
  "session_id": "7",
  "evidence": [
    { "label": "Failed check", "value": "docs-regression" }
  ],
  "commands": [
    {
      "label": "Rerun verification",
      "command": "asl verify <plan-dir> --session 7 --json"
    }
  ],
  "artifacts": [
    { "label": "Result JSON", "path": ".../result.json", "kind": "json" }
  ]
}
```

`no-contract`:

```json
{
  "kind": "no-contract",
  "severity": "error",
  "recoverable": true,
  "session_id": "3",
  "commands": [
    {
      "label": "Check preflight",
      "command": "asl preflight <plan-dir> --json"
    }
  ]
}
```

`malformed-contract`:

```json
{
  "kind": "malformed-contract",
  "severity": "error",
  "recoverable": true,
  "session_id": "3",
  "artifacts": [
    { "label": "Session file", "path": ".../session-3.md", "kind": "markdown" }
  ]
}
```

`stale-lock`:

```json
{
  "kind": "stale-lock",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "Lock PID", "value": "12345" }
  ]
}
```

`stale-state`:

```json
{
  "kind": "stale-state",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "State", "value": "stale unfinished run" }
  ],
  "warnings": ["state.json disappeared during recovery diagnosis"],
  "warning_details": [
    {
      "code": "state-missing-race",
      "message": "state.json disappeared during recovery diagnosis",
      "severity": "warning"
    }
  ]
}
```

`commit-preflight`:

```json
{
  "kind": "commit-preflight",
  "severity": "warning",
  "recoverable": true,
  "commands": [
    { "label": "Inspect readiness", "command": "asl preflight <plan-dir> --json" }
  ]
}
```

`commit-failed`:

```json
{
  "kind": "commit-failed",
  "severity": "error",
  "recoverable": true,
  "commit_index": 1,
  "artifacts": [
    { "label": "Commit stderr", "path": ".../commit-stderr.log", "kind": "log" }
  ]
}
```

`conversation-recovery`, on `conversation-recovery-required`. Note that
`recoverable` is `true` although the card performs nothing: the recovery
exists, and a person performs it.

```json
{
  "kind": "conversation-recovery",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    {
      "label": "Decision",
      "value": "fresh-conversation",
      "detail": "authorized by operator"
    },
    {
      "label": "Automatic resume",
      "value": "no",
      "detail": "the run loop cannot clear this pause on its own"
    },
    { "label": "Authorization", "value": "one-use" }
  ],
  "commands": [
    {
      "label": "Inspect recovery",
      "command": "asl control recover-conversation <plan-dir> --json"
    },
    {
      "label": "Authorize fresh conversation",
      "command": "asl run <plan-dir> --resume --fresh-conversation"
    }
  ]
}
```

The same kind on `redirect-successor-blocked` reports
`"value": "none exists"` for its `Authorization` row, `severity: "error"`,
and a single `redirect-status` command — there is no second command,
because there is no authorization.

`generic`:

```json
{
  "kind": "generic",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "Reason", "value": "nonzero-exit" }
  ]
}
```

Representative mappings:

- `reply-needed` points at the reply workflow (`asl questions`, then
  `asl run --resume --reply` or the bridge's `question.reply`).
- `not-loop-ready` shows readiness evidence and displays copy-only
  preflight repair commands.
- `verify-failed`, `no-contract`, and `malformed-contract` identify the
  failed session when available, link failed-turn artifacts, and display
  copy-only `asl verify`/`asl preflight` commands.
- `stale-lock` and `stale-state` point at lock/state evidence; `asl unlock`
  clears them.
- `commit-preflight` and `commit-failed` surface commit evidence and
  failed commit artifacts when present.
