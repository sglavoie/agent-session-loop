<!-- Continuation of the previous part. -->

- Start `asl workbench serve --open` and confirm the queue lists
  state-backed plans, registry pins, and any `--plan-root` children.
  Pin one extra plan from the UI and confirm `workbench.json` gains the
  entry.
- Edit a pin's launch profile (implementation harness/model, review
  harness/model, commit mode), launch the
  plan from the UI under a fake harness, watch the row transition from
  `ready` → `running` → `complete`, and confirm the spawned child is a
  detached `asl run` process (not a child of the server). Confirm the captured
  argv uses `--harness-model` and `--review-harness-model` and contains no
  legacy shared ASL model flag.
- Load a schema-v1 registry pin containing `profile.model`, confirm the global
  warnings area reports that the legacy value was ignored, then save the
  profile and confirm `workbench.json` is schema v2 with only `harness_model`
  and `review_harness_model` model-routing fields.
- Start `asl run <plan-dir>` from a terminal while the Workbench is
  running. Confirm the Workbench attaches to that lock and surfaces the
  same active turn — both terminal and Workbench should observe the
  same `state.json` without lock contention.
- Pause a run with a blocked question (`RUNNER_PAUSE: blocked - ...`),
  open the detail view, submit a reply through the reply panel, and
  confirm `state.json`, `events.jsonl`, and the captured `argv` never
  contain the reply text (only `reply_length` and the redacted argv
  placeholder).
- Click "stop after current" from the Workbench while a fake-harness
  turn is in flight; confirm the turn is not killed and the next
  lifecycle checkpoint records `run_control: stop-after-current-...`
  events sourced from the control queue rather than the keyboard
  listener.
- Create a stale lock (e.g., kill the runner with SIGKILL), open the
  detail view, confirm the "stale lock" CTA is offered, and unlock with
  confirmation. Attempting to unlock an active lock should require the
  force toggle.
- From the detail view, open logs (`stdout` and `stderr`), the last
  message, the active turn's `result.json`, the progress journal, and
  `final-review.md` (when present); confirm every viewer respects the
  bounded read window without 500-ing if the artifact file is missing
  or rotated mid-poll.
- Hard-refresh the page and confirm in browser devtools that
  `/static/app.js` and `/static/styles.css` hit `304 Not Modified` on
  the second load while `GET /` keeps returning `Cache-Control:
  no-store`.
- Open the Workbench in two tabs and pin/unpin different plans
  concurrently; confirm `workbench.json` retains both edits (the
  advisory `workbench.json.lock` sidecar serializes the writers).
- Send SIGTERM to the Workbench process and confirm the listening
  socket is released immediately (re-binding to the same port should
  succeed without `EADDRINUSE`).
- Open the queue against a discovery root that contains a mix of
  paused, failed, running, ready, and complete plans. Confirm the
  default view sorts by `urgency_rank` (paused-for-reply rows surface
  first, complete rows are hidden) and that flipping the "show
  completed" toggle reveals the completed rows in every chip.
- Type a substring that appears only in one plan's label, plan_dir, or
  pause message; confirm the queue narrows to that plan after the
  150 ms search debounce and that the empty-state element renders with
  `data-state="no-search-results"` (and a "clear search" recovery
  button) when the query matches nothing. Switch the sort `<select>`
  through `urgency`, `recent`, `label`, `path`, and `oldest` and confirm
  the order changes accordingly. Reload the page and confirm the search
  text, sort mode, completed-visibility toggle, active filter chip,
  and selected plan all survive via sessionStorage (`wb.search`,
  `wb.sort`, `wb.completed`, `wb.filter`, `wb.selected`).
- Seed at least two complete plans and confirm the queue/preview show
  each plan's completion time (or "not recorded" when no `completed_at`
  was captured) and that non-complete rows show `-`. Enter cleanup mode,
  filter to the `complete` chip, and confirm completed plans appear
  without flipping "show completed". Sort `oldest` and confirm the
  stalest completed plans (by `completed_at`) lead. Press
  `select visible` and confirm every visible archivable completed row is
  selected for bulk archive while active-lock rows stay unselected; press
  `select candidates` and confirm only rows past the staleness threshold
  are selected. Exit cleanup mode and confirm `wb.completed` was not
  changed.
- Open the Archive view with several archived plans. Press `select all` and
  confirm every loaded entry is checked, the count reads `N of M entries
  selected`, and `select all` disables once all loaded entries are selected.
  Press `restore selected`, accept the plain confirm prompt, and confirm the
  entries restore best-effort through `POST /api/archive/restore` (feedback
  reads `restored X`, or `restored X, Y failed` on partial failure) with
  successfully restored entries leaving the selection. Re-select entries, press
  `purge selected`, type the exact count to confirm the typed-count purge, and
  confirm a mismatched count is rejected without deleting anything. Press `clear
  selection` and confirm the selection empties without restoring or purging any
  entry.
- On a viewport ≥ 960 px, click a row body (outside the label and
  action button) and confirm the right pane shows the selected plan
  preview (state badge, triage reason, last-message preview) without
  leaving the queue route. Resize below 960 px and confirm the layout
  collapses to a single pane with no overlapping controls.
- Click a row's "reply" action button on a paused-for-reply plan and
  confirm the URL becomes `#plan?dir=…&focus=reply`, the detail view
  scrolls the reply panel into view, and the reply textarea is focused.
  Repeat with an "unlock" action and confirm the controls panel
  scrolls into view and the Force-unlock button is focused (bail
  without confirming to verify the confirmation gate still works).
- Start typing a reply draft in the paused-Q&A panel, wait through two
  or three 5 s refresh polls, and confirm the draft is still present
  in the textarea (the reply text must never be persisted to
  sessionStorage/localStorage and must never be overwritten by
  polling).
- Seed representative Recovery Center plans in the Workbench state
  root: ready (`kind: none`), running (`kind: none` with active lock),
  reply-needed, not-loop-ready, verify-failed, stale-lock or
  stale-state, commit-failed, and complete (`kind: none`). Open each
  detail view and confirm the card kind, severity, evidence rows,
  action-card targets, copy-only commands, and artifact links match the
  seeded state without executing arbitrary repair commands.


### Redirect recovery before restarting

`redirect_restart.py` checks durable predecessor evidence before startup
reconciliation, commit recovery, another attempt, or plan completion. Each
accepted turn owes proof of termination even after its runner releases the lock
or dies. Private `redirect-predecessors/` state records the worker group and
sampled descendant identities. An uncleared predecessor is checked again on
restart without sending signals. A live group, surviving descendant, unknown
identity, foreign host, missing evidence, or process-sweep opt-out keeps
`redirect-successor-blocked` in force. A proven stopped predecessor permits
normal recovery. A new run ID or `--fresh-conversation` cannot bypass this gate.
Inspect the `evidence_path` and confirmation in the pause details, stop the
identified predecessor through the normal operator process procedure, then
rerun. Missing or corrupt historical evidence requires restoring valid evidence;
rerunning or deleting a lock does not manufacture termination proof.

An open native submission window also blocks retries and relaunches of its
session with `redirect-submission-unresolved`. Inspect provider history and
`asl control redirect-status PLAN_DIR --json`, then explicitly record the
observed outcome:

```bash
asl control resolve-redirect PLAN_DIR --operation-id ID --outcome submitted --reason "Observed in native history"
asl control resolve-redirect PLAN_DIR --operation-id ID --outcome not-submitted --reason "Confirmed never submitted"
```

Choose one outcome from evidence. `submitted` permits execution without
replaying the correction or fabricating acknowledgment. `not-submitted` releases
the correction for the next prompt, but is refused if delivery is already
recorded. The command requires a reason and refuses a live runner; the decision
is durable before any release. There is no timed or default resolution. Any
other `--outcome` is refused with reason `invalid-outcome`, in the `--json`
envelope like every other refusal of this command.

For a registered `--commit-per-session` run paused after independent repository
changes, the operator can acknowledge the historical incident and explicitly
checkpoint retained owned work:

```sh
asl acknowledge-external-changes PLAN_DIR --session SESSION_ID --repository ALIAS --json
asl acknowledge-external-changes PLAN_DIR --session SESSION_ID --repository ALIAS --accept DIGEST --reason 'Independent changes authorized by operator' --checkpoint-retained --json
```

Repeat `--repository` for each independent alias. Preview never establishes
authorship or changes Git contents, indexes, refs, or session status. Acceptance
requires explicit operator authorization, a reason, the exact reviewed digest,
complete evidence, unchanged original roots/origins, binding, registry, scope,
plan content and guard policy, and no active runner. External changes cannot be
acknowledged in a writable repository. Dirty read-only repositories needed by
the current session block recovery. The retained writable paths must match the
rejected turn's original file and index fingerprints; unrelated staged files,
new dirt or content drift block checkpoints. Linked worktrees remain under their
owning alias and use their own staging root.

Recovery persists progress before committing and records checkpoint SHAs. A
partial failure prints `resume_argv`; rerun that exact command after repairing
the failure. Recorded successes are never recommitted, and interrupted commits
require exact parent, message, paths and content proof. Acknowledgement is
recorded only after checkpoint targets are clean and unrelated evidence remains
unchanged. The rejected turn and assurance issues remain intact. Checkpoints
retain unverified work and leave the session pending; verification and review
still run on resumption.

Only the current session's read/write union needs clean repositories before a
registered per-session worker launch. This check repeats before each worker, so
a previously unused dirty repository blocks when a later session needs it.
Missing or malformed scope fails closed. Complete workspace mutation guards
remain active: new changes in unused repositories still reject the turn and
require a fresh acknowledgement. Legacy and `--commit-at-end` checks retain
their existing behavior. `asl status --json` exposes preview commands and durable
recovery progress for operator inspection and supervisor audit.
