# ASL Autopilot: helper internals

On-demand reference for the `asl` skill (`../SKILL.md`): forwarded run flags,
registered workspace roots, and how the bundled helper watches the mailbox and
holds its supervisor lease.

## Spark

`--spark` and `--no-spark` are ordinary forwarded run flags. Spark is an
implementation-only runner policy: eligible prepared Codex sessions may use
`gpt-5.3-codex-spark`, while review/audit turns, generated review repairs,
live model overrides, non-Codex harness switches, and ineligible sessions use
the resolved baseline. Do not reinterpret or remove either flag. The runner
owns classified quota/unavailable-model fallback, preserves the failed Spark
turn, and immediately retries the still-pending session on the baseline; the
Autopilot helper should supervise that continuation normally rather than add a
second retry of its own. Surface the `spark` and `spark_route` status metadata
when it explains a cooldown, baseline route, or fallback.

## Registered multi-repository workspaces

For a registered multi-repository plan, `--cwd` is the common workspace root,
not one child checkout. Prefer the authoritative `workspace_root`,
`registry_digest`, and ordered `repositories` projection from `asl status
--json`; an omitted root may be filled from that binding, but a conflicting
root is `user_required`. Autopilot never infers a parent from one repository or
grants itself `--rebind-workspace` authority. A deliberate move is a separate
operator action:

```text
asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE --rebind-workspace
```

## `asl outbox supervise` details

- The wait returns on a five-second internal cadence, so a question surfaces
  within seconds rather than at the next poll boundary.
- A returned question that is still open also refreshes the body-free
  supervisor lease projection and makes a best-effort
  `AgentSessionLoopMailboxQuestion` handoff to AgentMux. This applies to the
  explicit supervisor command, not the helper's internal watcher; notification
  failure never changes the result.
- `--timeout` defaults to 550 seconds, which sits below the supervising
  harness's own tool-call ceiling (safe for a 600-second cap). Loop rather than
  spending the 1800-second maximum in a single call.
- Running this against a live helper heartbeats its lease; it does not take over
  supervision.

## Progress heartbeats

`--json` emits a single machine-readable result — into the `result.json` at the
handshake's `result_path` under `--detach`, on stdout otherwise; progress
heartbeats go to stderr (the handshake's `stderr_path` under `--detach`). Each heartbeat carries live progress — the running session label,
`(n/total)`, and `idle <n>s` since the child last emitted output — and escalates
to a `WARN possibly stalled` line once idle time crosses the stall threshold.
The WARN is soft: supervision keeps waiting. Above it sit two hard hang bars
that end supervision with `reason: worker_hung` while leaving the run alive —
see `quota-and-hangs.md`. To override the runner's cycle cap at launch, include
`--max-review-fix-cycles N` inside the `--command` string (`0` still reviews but creates no repair
sessions). The same launch command may also set `--max-repair-group-attempts N`
or `--max-review-fix-cycles-ceiling N` when the operator deliberately wants
different review-loop limits for that run.

## Watcher and lease

With mailbox supervision enabled, the helper writes a fresh fixed-size lease
before starting the child, polls questions and notices every five seconds, and
renews the lease every 30 seconds using the existing child/attach wait loops—no
watcher thread is added. Each message appears on stderr as one bounded,
JSON-escaped `QUESTION id=... preview=...` or `NOTICE id=... preview=...`
record; its complete body is retained in the corresponding `questions` or
`notices` array of the final structured `outbox`. The watcher keeps one
physical-sequence cursor across attach and self-launch transitions. On attach
it replays notices attributed to the still-live run, suppressing duplicates for
the lifetime of that supervisor process while allowing another supervisor
process to replay them at least once. Notices never enter the open-question
projection or lease payload. The watcher stops renewing and lets the lease
expire when supervision returns.
Lifecycle progress is body-free and bounded: `WAIT_ENDED ...` identifies a
terminated wait and `CLOSURE_CONSUMED ...` confirms that the worker or runner
actually observed an accepted closure.

One-shot supervisor commands do not displace a live helper. When the on-disk
lease is fresh and carries a question projection, `asl inbox guidance`,
`asl inbox reply`, `asl outbox wait`, and `asl outbox supervise` heartbeat it —
the helper's supervisor identity and projection survive with only the lease timestamp refreshed during
that initial heartbeat. A wait that returns an actionable open set then
refreshes that same identity with its exact latest projection. Only a missing,
stale, or unprojected lease is taken over with a fresh supervisor identity, so a
human stepping into an unsupervised plan still registers as its supervisor.
