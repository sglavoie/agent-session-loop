### Frontend behavior

The bundled UI lives at `asl_workbench/static/` and ships with the
portable package (no build tooling, no third-party JS). There is one
copy of it: the laptop's `LocalProvider` and the Pi's `RemoteProvider`
are two front doors onto the same `asl_workbench.http.Application`,
and `shell_bundle()` serves both of them the same `index.html`,
`app.js`, `styles.css`, and `static/modules/` tree. A fix made once is
therefore a fix in both places, which is the point of having moved it.

`app.js` is an entry point rather than the program: it imports the
modules under `static/modules/` as native ES modules, which the browser
resolves itself. Nothing is bundled, minified, or transpiled, so no
deployment needs a Node runtime or a build step — the Pi in particular
serves the files exactly as they sit in the package. The cost is that
one page load opens one connection per module, which is why both
`WorkbenchHTTPServer` and the test fixture raise `request_queue_size`
well above `socketserver`'s default backlog of five: an overflowed
accept queue is a reset connection, and a reset module request is a
blank page rather than a slow one.

The two deployments publish the same plans under different field
names, because each names what it actually knows: the laptop reads the
plan on disk and derives `label`, `searchable_text`, and
`next_action_hint`, while the Pi carries `display_label`/`title` and
`search_text` from the observation the connector sent and has no hint
at all. `static/modules/identity.js` reconciles the two vocabularies in
one place (`planLabel`, `planSearchText`, `planHint`), alongside the
addressing duality it already held (`plan_dir` on the laptop,
`plan_key` on the Pi), so no rendering module reads a provider-specific
field name directly.

Pages:

- **Queue (`#queue`)** — the operator home, framed as a triage inbox.
  Filter chips collapse `verifying` under `running` and `invalid` under
  `failed`; the other primary states map 1:1. Rows show plan label/path,
  primary state, counts, last-activity preview, next-action hint, and an
  artifact link block. Queue metadata shows the resolved state root and
  last update; if polling fails, the visible rows are explicitly marked
  stale. The list polls every 5 s, pauses while the tab is hidden, and
  diffs rows by `plan_dir` so scroll position and filter selection
  survive each refresh.

  The queue defaults to triage mode: rows sort ascending by
  `urgency_rank` (paused-for-reply first, complete last), and any plan
  whose `urgency_group === "complete"` (or `primary_state === "complete"`)
  is hidden unless the operator flips the "show completed" toggle or
  selects the `complete` chip. A control bar between the filter chips
  and the inbox shell hosts a search input (matches against
  `searchable_text`), a sort `<select>` with the five supported modes
  (`urgency`, `recent`, `label`, `path`, `oldest`), and the "show
  completed" toggle. The search input debounces at 150 ms so a typing burst
  collapses into one `/api/queue` fetch; the clear button bypasses the
  debounce. When zero rows match, the empty-state element writes a
  stable kind onto `data-state` (`loading`, `no-plans`,
  `no-search-results`, `no-filter-matches`, `completed-hidden`, or
  `no-actionable`) and surfaces context-aware recovery actions (clear
  search, show completed, reset filter) for the kinds that have one.

  Row action buttons are navigation-only: clicking a row's action
  routes to the detail view with a focus hint (`#plan?dir=…&focus=<key>`)
  instead of issuing mutating POSTs from the queue. The button carries
  a `data-action-kind="<RecommendedAction>"` attribute (`watch`,
  `reply`, `inspect-failure`, `unlock`, `view`, `run`, `repair-plan`)
  so CSS can style row intent without re-parsing labels. Clicking the
  row body (outside the label `<a>` and the action `<button>`) selects
  the plan and updates the preview without leaving the queue route.

  On viewports ≥ 960 px the queue and detail panels render as a
  two-pane shell: the queue stays sticky on the left while the right
  pane shows either a placeholder ("select a plan to inspect") or the
  selected plan's preview panel. The preview reads from the cached
  queue payload (no extra fetch) and re-renders on selection clicks so
  the operator can scan triage state, pause reason, and last-message
  preview without leaving the queue. Below 960 px the layout collapses
  to a single-pane route toggle (queue ↔ detail) so phones get a clean
  stacked layout with no overlapping or clipped controls.

  The shell exposes three data attributes (`data-route="queue|detail"`,
  `data-viewport="mobile|desktop"`, `data-preview="empty|active"`) so
  CSS can target the placeholder/preview swap without re-querying
  `matchMedia` on every render.
- **Detail (`#plan?dir=<encoded>[&focus=<key>]`)** — per-plan view with
  the Recovery Center, active turn, active verification, session table,
  recent events, progress journal, artifacts, the urgent-correction
  composer, and operator controls (pin/unpin, edit profile, launch,
  reply, stop-after-current, select harness, unlock).
  The Recovery Center appears near the top of the
  detail page so a paused/failed/stale plan shows the diagnosis,
  evidence, copyable commands, related artifacts, and confirmed next
  actions before the raw logs. Dangerous actions (unlock with an active
  lock, launch under
  unsafe-bypass) prompt for in-page confirmation before the API call.

  The optional `focus=<key>` hash query parameter accepts the keys
  registered in `DETAIL_FOCUS_TARGETS` (`recovery`, `reply`, `events`,
  `active`, `launch`, `unlock`, `redirect`). The router validates the
  key against the map and silently drops unknown values.
  `applyDetailFocus` is
  one-shot — it
  scrolls and focuses the target element on the first render after
  navigation, then clears `state.route.focus` so subsequent 5 s polls
  do not keep snapping the viewport (the param itself stays in the URL,
  so a manual reload will re-focus once).

  One router handles both link formats the two halves left behind, so
  links already sent out keep working. The Workbench's own form names a
  plan directly — `#plan?dir=<encoded>` from the laptop, `#plan?key=<plan_key>`
  from the Pi, either optionally with `&focus=`. The Companion's form
  names a run instead (`#run=<run_key>`, minted by
  `catalog.run_key(plan_key, run_id)` and still emitted by the ntfy
  notifications the Pi sends), which the shell cannot resolve on its own
  because a run key says nothing about which plan owns it. So
  `resolveRouteFromPlans` waits for a catalog page, finds the plan whose
  run matches, and `replaceHash` rewrites the URL to the canonical
  `#plan?…` form; until that resolves, the detail header reads
  `(opening run)`. A run key with no plan in the catalog leaves the hash
  alone and says so, rather than rendering an empty plan.

  The Recovery Center renders compact queue/detail `plan.recovery`
  immediately and then replaces it with full `/api/recovery` evidence
  when that request succeeds. If the detail fetch fails, the compact
  fallback remains visible when available; otherwise the panel renders a
  deterministic "Recovery unavailable" card. Reply-needed recoveries
  load the embedded reply form through `/api/questions` and submit only
  through `/api/reply`. Launch and unlock action cards call the same
  `/api/launch` and `/api/unlock` handlers as the ordinary controls;
  inspect and navigate actions scroll/focus existing detail sections.

  The session table iterates `payload.sessions` from `/api/state` and
  renders the dotted `id` verbatim (e.g., `2.1`, `2.2`), the basename
  of `path` (e.g., `session-2.1.md`), and the row's own `status`
  string (`pending`, `complete`, `empty`, `unknown`). The row whose
  `id` matches `state.active_turn.session_id` by exact string equality
  is overridden to `active` — there is no integer-loop or
  `parseInt(next_pending_session_id)` inference. The empty-state hint
  still reads `state.detailPlan.next_pending_session_id` (sourced from
  `/api/plan`, not `/api/state`); that fallback only surfaces before
  the first successful `/api/state` poll.

  **Urgent correction composer.** `#wb-redirect-panel` is the browser
  entry point to the redirect path. Its submit button is labeled with
  both halves of what it does — "interrupt turn & resume with this
  correction" — and a confirm dialog restates that the session stays
  pending and the interrupted attempt earns no completion. The panel
  polls `/api/redirect-status` on the same 5 s detail cycle and takes
  three things from that response and nowhere else: the `action_token`
  it must return with a submission, whether `run.redirect` appears in
  `permitted_actions`, and the `disabled_reasons[run.redirect]` string
  shown when it does not. A composer never derives its own freshness
  proof, and it never infers availability from the queue row it was
  opened from.

  Progress renders as six fixed phases — awaiting acceptance,
  cancelling the turn, resuming with the correction, worker
  acknowledged, unresolved, settled — with `REDIRECT_STAGE_PHASES`
  mapping every `redirect_cli` stage onto exactly one of them.
  Acknowledgment is deliberately its own phase: "the turn was stopped"
  and "a worker receipted the correction" are different facts, and only
  the second says the correction landed. Operation rows carry
  `data-stage` and `data-phase` and show the journal's
  `stage_description`, feedback digest, and byte count — never the
  correction body.

  Operation identity outlives any one answer. The composer mints a
  memory-only `operation_id` on first submit and keeps it through every
  uncertain, refused, or failed response, because the journal treats a
  resubmission carrying the same id as the same request; minting a
  fresh id on retry is how one correction silently becomes two. The
  textarea is cleared only on a durable answer (a 202 that persisted, or
  a 200 replay that recognized an id already in the journal) and is
  preserved on every failure path. A submission in flight sets
  `state.redirect.inFlight`, which both disables the button and refuses
  a second submit arriving by keyboard, so a double click is one
  correction. Discarding the draft discards the id with it, and
  switching plans resets the composer rather than carrying a draft
  across.

  A retry is only a retry while the draft is unchanged. If the operator
  edits the text (or the tab names a different turn) after the id was
  accepted, the server refuses it `409
  redirect-replay-conflict` rather than replaying the old receipt, and
  the composer keeps the draft and reveals `#wb-redirect-conflict`: what
  changed, the recorded digest, that nothing was resubmitted and the
  recorded correction is unchanged, and the same reconciliation hint the
  CLI prints. The only offered way forward is the explicit "keep this
  draft and send it as a new correction" button, which mints a fresh id
  and sends nothing — the operator still presses submit — because
  auto-retrying under a new id is exactly the silent second correction
  the id was minted once to prevent.

  Because nothing is persisted, a reload repopulates the panel purely
  from `/api/redirect-status`. Operations still in `submitted` or
  `uncertain` are surfaced by id under "Unresolved after reconnect",
  carrying a verbatim mirror of `redirect_cli.RECONCILE_HINT`, so a
  reconnecting operator reconciles by id instead of resubmitting blind.
  A failed poll marks the panel unreadable and disables submission but
  leaves the rendered operations in place — a failed read is not
  evidence that a correction went away.

  **Conversation recovery.** When `/api/recovery` classifies the pause
  as `conversation-recovery`, the Recovery Center reveals
  `#wb-conversation-recovery` and fills it from
  `/api/conversation-recovery`: the decision, pause reason, whether the
  run resumes automatically (it does not), who must authorize it, the
  inspect and authorization commands as copyable text, the one-use note
  when an authorization exists, and the corrections a fresh start would
  still carry. This group is report-only by construction — it issues no
  POST and ships no button that performs the recovery — because
  authorizing a fresh conversation discards the agent's saved context
  and the authorization is one-use, so it is typed deliberately into a
  terminal rather than clicked.

Persistence and privacy:

- The frontend persists operator preferences under the `wb.*`
  sessionStorage namespace: `wb.token` (bearer token), `wb.filter`
  (active chip), `wb.search` (search text), `wb.sort` (sort mode),
  `wb.completed` (show-completed toggle), and `wb.selected` (selected
  plan_dir for the preview pane). Each refresh poll preserves these
  preferences and the queue/detail scroll position. Cleanup mode reveals
  completed plans on its own gate and never writes `wb.completed`, so the
  persisted show-completed preference is left untouched when the operator
  exits cleanup mode.
- The reply textarea is intentionally **not** persisted: no
  sessionStorage or localStorage key under the `wb.reply` namespace is
  written, and no refresh path mutates `#wb-reply-text.value` outside
  the explicit clear-on-success flow. An in-progress reply draft
  survives any number of 5 s polls without being clobbered.
- The urgent-correction textarea follows the same rule and is held to it
  by a regression test: no `wb.redirect` storage key exists, no
  `setItem` call touches the correction text, and no refresh path
  mutates `#wb-redirect-text.value` outside the clear-on-durable-success
  flow. The minted `operation_id` is likewise memory-only, so it does
  not survive a reload — the journal does, which is why unresolved
  operations are recovered by id from `/api/redirect-status` rather than
  from the browser.

Static asset caching:

- The HTML shell uses `Cache-Control: no-store` so a server upgrade is
  never masked by a stale cached index.
- `/static/<path>` uses `Cache-Control: no-cache, must-revalidate` with
  a strong `ETag` derived from `st_mtime_ns + st_size`. The browser
  validates each request; identical bundles short-circuit to `304 Not
  Modified` without re-transferring bytes. If a build pipeline ever
  normalizes mtimes, switch the ETag generator to a content hash before
  the cache policy starts handing out stale 304s.

### Shutdown and lifecycle

`workbench_serve_from_args` installs signal handlers for SIGINT and
SIGTERM via `_install_shutdown_handlers`. Both trigger
`server.shutdown()` from a daemon thread, then the previous handlers
are restored. Ctrl+C remains the documented exit path; `kill <pid>` or
a systemd-style stop also exits cleanly and releases the listening
socket immediately (re-binding to the same port should succeed without
`EADDRINUSE`). Signal installation is silently skipped when the server
is constructed off the main thread; test harnesses call
`server.shutdown()` directly in that case.

Race-safe reads: `/api/logs`, `/api/last-message`, `/api/state`, and
`/api/verification` treat `FileNotFoundError` raised mid-read as "file
absent" — they return `404` (or `present: false`) rather than a half-
filled body with `error: ...`. Operators chasing a transient 500 during
a runner cleanup window should retry once before opening a bug.

### Workbench troubleshooting

- **Token forgotten / browser refused** — restart the server. Tokens
  are regenerated on every start, so reopening
  `http://127.0.0.1:8765/?token=<new>` (or running with `--open`)
  recovers access.
- **Port in use** — bind fails with `EADDRINUSE`. Pass `--port 0` to
  bind ephemerally, or `--port <free>` to choose another fixed port.
- **Plan does not appear in the queue** — the discovery sources are
  read in canonical order (`state`, `registry`, `plan-root`,
  `agent-plans-dir`, `tmp-default`, `share-default`). If the plan
  directory does not have both `manifest.md` and `context.md`, no
  source will surface it; add a `--plan-root` parent or pin it through
  the UI to force it in.
- **Running loops look ready** — confirm the queue metadata state root
  matches the root used by `asl run`/`asl sessions --running --json`.
  Workbench and runner state only line up when they use the same
  `--state-dir` value.
- **Pinned plans missing after a manual edit** — `load_registry`
  reports malformed entries as warnings on the returned payload. Check
  `GET /api/registry` (or the UI badge) for the warning list before
  re-pinning by hand.
- **Stale lock CTA** — `/api/unlock` returns 409 for an active lock
  unless `force: true` is set. Confirm the owning process is dead with
  `asl status --json` (look for `runner_state.stale_unfinished`) before
  unlocking with force.
- **Reply submitted but the run did not resume** — `/api/reply`
  forwards the body to `asl run --reply-stdin`. The runner refuses the
  resume with `reason: no-paused-run`, `missing-question-context`, or
  `reply-context-mismatch` if the paused turn is stale; the response
  body carries the verbatim `reason`.
- **Recovery Center reports `not-loop-ready`** — inspect the readiness
  evidence first. Copy `asl preflight <plan-dir> --mark-loop-ready` only
  after fixing non-derived preflight findings, or use the displayed
  `$plan-preflight-iterate <plan-dir>/` command for an interactive
  repair pass. The Workbench displays both commands but does not run
  either one.
- **Recovery Center reports `verify-failed`, `no-contract`, or
  `malformed-contract`** — open the failed-turn artifacts from the card,
  repair the session or its Completion Contract, then copy the displayed
  `asl verify <plan-dir> --session <id> --json` command before retrying
  the run through the existing launch action.
- **Recovery Center reports `commit-failed` or `commit-preflight`** —
  inspect failed commit artifacts and the repository worktree. Review
  the launch profile's commit mode before retrying; recovery cards never
  mutate git state directly.
- **Recovery Center reports `stale-lock` or `stale-state`** — confirm
  no runner is still active, then use the existing unlock confirmation
  flow. Stale lock cards show lock evidence such as PID/reason when the
  lock file can be read.
