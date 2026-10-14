### Plan catalog

The catalog is the one enumeration of plans the unified Workbench reads,
shared by the local UI and the Pi. It is split across the two halves of the
program, because the two halves of the job are different:

- `asl_workbench/catalog.py` holds the definition — the field partition,
  identity, labels, project grouping, ordering, and pagination. It imports
  nothing from `agent_session_loop`, so it loads on the Pi, and it takes a
  `WorkbenchSummary.to_payload()` mapping rather than a plan directory.
- `agent_session_loop/workbench_catalog.py` holds the filesystem half —
  discovery, classification, and plan titles — and returns one `CatalogScan`
  carrying both the portable catalog and the `WorkbenchSummary` objects the
  local UI still renders, from a single pass over the discovered plans.

Four properties are worth stating, because each is enforced rather than
intended:

**Every summary field is either cache-safe or private.** `CACHE_SAFE_FIELDS`
holds the 16 fields a remote provider may retain; `PRIVATE_FIELDS` holds the
27 it may not. The partition is total over the summary payload in both
directions, and a field the partition does not name raises rather than being
guessed into the cheaper half. Everything path-bearing is private, as is
`searchable_text`, which carries the plan's prose: the pause message and the
last-message preview are in it. A `CatalogEntry` therefore builds its own
`search_text` from cache-safe fields only, and the private half is reachable
only through `PlanDetail`, which refuses a cache-safe or unknown field name.

**Identity is a digest, not a path.** `plan_key` is
`sha256` over the canonical plan directory, truncated to 16 hex characters,
and `run_key` hashes the `plan_key`/`run_id` pair rather than the directory —
byte-identical to the Companion's `identity.py`, which is what lets the
migration keep the rows the Pi already stores. `display_label` prefers the
manifest's first heading, then a registry pin label, then the workspace root,
then the plan directory's last component, and agrees with the Companion
exactly when no title is available.

**The catalog is uncapped.** The bridge projection caps interventions at 200
rows of active work on purpose; a catalog that inherited that cap would report
a large corpus as a small one. Enumeration is complete and paged instead:
`PlanCatalog.page(offset=, limit=)` and `PlanCatalog.pages()` walk the whole
catalog, and a corpus of several hundred plans retains its ready, completed,
and paused work rather than losing the tail.

**An incomplete generation says so.** `generation` is a digest over the
ordered entry payloads and carries no wall-clock, so two scans of unchanged
plans agree. When a plan cannot be classified, the scan records a warning and
clears `complete` on the catalog and on every page cut from it, so an
incomplete read is never mistaken for authoritative absence.

Catalog reads are observational: summaries are built with `reclaim=False` and
no retention runs, so repeated reads leave `state.json`, `events.jsonl`, and
`lock.json` byte-identical. See `asl_workbench/changes.py` for what remains.

### Remote observation

`asl workbench connect` publishes what this Mac can see to a remote
Workbench service. It is the outbound half of the unified Workbench: the
local `serve` command renders the corpus into a loopback browser, and
`connect` sends the same corpus — as bounded, body-free observation frames —
to a service that is not on this machine.

```bash
asl workbench connect --target-id pi-1 \
  --origin https://workbench.example \
  --token-file ~/.config/asl/workbench-connector.json
asl workbench connect --target-id pi-1 --origin https://workbench.example \
  --token-file ~/.config/asl/workbench-connector.json \
  --heartbeat-interval 10 --observation-interval 60 --json
```

`--target-id` names the service being published to and is echoed in every
envelope, so a frame addressed to one service is refused by another.
`--connector-id` defaults to a sanitized hostname. `--token-file` is a JSON
document holding a `connector_token`; the file must be a regular file owned
by the invoking user with no group or other permission bits, which is the
same rule the service applies to its own secrets. The token travels only as
an `Authorization: Bearer` header over HTTPS to `POST <origin>/v1/ingest`.

Four properties are enforced rather than intended:

**It reads the shared services in process.** One `workbench_catalog.scan()`
yields the complete catalog and the summaries that back the run rows, and
`workbench_history.project_history()` supplies the checkpointed history
window. There is no bridge subprocess, no NDJSON hop, and no second
projection to drift: the command spawns nothing and the connector module
itself (`asl_workbench/connector_runtime.py`) never reads a plan or names a path.

**Observation cannot modify what it observes.** Summaries are built with
`reclaim=False`, lock files are inspected rather than read for recovery, and
no retention or cleanup runs, so publishing leaves `state.json`,
`events.jsonl`, and `lock.json` byte-identical. The published run shape
carries no filesystem path at all — `plan_dir`, `workspace_root`, the
display label, and the question body are dropped before a frame is built —
so a remote service can hold the whole catalog without learning this
machine's disk layout.

**A checkpoint moves only after delivery.** A pass publishes a heartbeat,
the current runs, every catalog page, and the history batches, and the
history window is committed only once every frame cut from that pass has
been accepted. A link failure or a shutdown mid-pass discards the pass
uncommitted, so the next pass re-reads the same window; ingest is idempotent
on `batch_id`, `entry_id`, and the catalog `generation`, so a reconnect
repeats rather than duplicates. Retry is deterministic exponential backoff
from 1s to a 60s cap, and a recovered link advertises a new generation,
because a restarted source is not a replay.

**Heartbeats do not wait for observation.** The observation pass runs off the
delivery path, so a slow scan delays the corpus and not the liveness signal.
A pass that fails or reports degraded discovery is published as a heartbeat
with `bridge_alive` false and a bounded `degraded_reason`, which is how a
remote service distinguishes "this Mac has nothing to report" from "this Mac
stopped reporting". Only one connector may run per state directory; the
second refuses with `reason: connector-locked` rather than publishing a
second, competing view. `SIGTERM` and `SIGINT` stop the loop, drop the
in-flight pass, and release the lock.

**Capabilities are derived, not declared.** The connector announces exactly
what it was wired with: `observe`, `catalog`, and `history` always;
`detail` because a detail responder is wired by default, scoped to the same
corpus the observations publish; and `mutate` only when
`--allow-remote-actions` was passed. Without that flag the hello says
`observation_only`, and the service refuses a mutating frame from this
connector by construction rather than by configuration.

```bash
asl workbench connect --target-id pi-1 --origin https://workbench.example \
  --token-file ~/.config/asl/workbench-connector.json --allow-remote-actions
```

With the flag, the same downward channel that carries a correlated detail
read may also carry a command, and the connector performs it through the
same services `asl workbench serve` uses — launch, resume, reply, question
reply, stop-after-current, abort, harness selection, redirect, unlock, and
archive — against the plan *key*, never a path. Everything decided against a
live turn goes under `STRICT` authorization; selecting a *future* harness is
the one control with no live turn for a proof to be about, so it goes the open
way the browser and the CLI already send it. Five refusals bound the lane, and
each is structural:

* **A command has a window.** Submission carries a deadline, capped at five
  minutes, and at most four may wait at once. A command whose window closes
  before it is attempted settles `expired` and is reported as never having
  run.
* **Cancellation is only possible before the attempt.** A pending command
  can be withdrawn; one that has been handed to ASL cannot, because the
  point at which it could be is already past.
* **The body is fetched at the moment of use.** A reply's prose has no
  envelope field — the contract set is pinned byte-for-byte — so the service
  announces a `reply_ref` and the connector redeems it over
  `POST <origin>/v1/commands/redeem` with the operation's single-use action
  token, immediately before handing it to ASL and never before. Reply text
  is never persisted, never journalled, and never held by a waiting command.
* **A restart settles; it never retries.** The journal is written *before*
  the side effect, so recovery can tell apart a command that certainly did
  not run (`expired`) from one that may have (`unconfirmed`). Both are
  reported as outcomes. Carrying a pending command across a restart and
  running it later — against state that has moved, for an operator who has
  stopped waiting — is the one thing the durable journal exists to prevent.
* **Only shared words go out.** Outcomes are reported in the transport's
  closed status and detail-code vocabulary; ASL's own refusal prose stays on
  this machine.

A remote action that ASL would refuse locally is refused identically here:
the same freshness check, the same observation-only setting, the same
current-capability list, and the same authoritative target state decide it,
because it is the same code deciding. A bulk archive is decomposed into one
operation per plan and reassembled into the same per-entry result the local
route returns, so a partial success reads the same from either side.

### Cleanup and archive workflow

Cleanup mode is a queue review mode for old, inactive work. It switches
the queue to `sort=oldest`, which ages complete plans off their
`completed_at` timestamp and every other plan off `last_activity_at`,
treating a missing timestamp as oldest. Cleanup mode reveals completed
plans on its own gate, so the operator can pick the `complete` filter
chip and review finished work in place without flipping "show
completed". The UI threshold control drives two bulk actions:
`select candidates` adds only the visible rows older than the chosen
number of days, while `select visible` adds every visible archivable row
regardless of age. Both skip rows with an active lock. Queue payloads
include `archivable` and `archive_blocker`; active runner locks are not
archivable and surface `archive_blocker: "active-lock"`.

Archiving moves the plan directory and the matching ASL state directory
together into the Workbench state root, along with available pin/profile
metadata and a summary snapshot. The detail action uses
`POST /api/plan/archive`; `POST /api/plan/delete` remains as a
backward-compatible archive alias for older clients. Both single-plan
routes require the same typed
full-path confirmation through `"confirm_plan_dir"`, refuse arbitrary
non-plan paths, and return `409` with `reason: "active-lock"` when an
active lock is present.

The archive storage and its safety gate live in
`agent_session_loop/workbench_archive.py`, which every surface — the HTTP
routes, the `asl workbench` CLI, and the remote connector — calls.

Bulk cleanup uses `POST /api/plans/archive` with `plan_dirs`. The server
archives each entry independently and returns per-entry results plus
`archived_count` and `failed_count`, so one blocked or invalid plan does
not prevent the rest from being archived.

The Archive view reads `GET /api/archive`, which returns archived
entries, `archive_retention_days`, each entry's `expires_at` and
`cleanup_state`, and the cleanup summary under `archive_cleanup`. The read
removes nothing: `archive_cleanup` reports `candidate_count`,
`candidate_ids` and `undated_count`, and every entry it names is still on
disk afterwards. `POST
/api/archive/restore` restores the archived plan and state back to their
original paths and restores pin/profile metadata when present. Restore
fails with `409` and `reason: "restore-conflict"` if either destination
that would be written already exists.

The Archive view exposes a bulk-selection bar (`#wb-archive-bulk`) with
`select all`, `restore selected`, `purge selected`, and `clear selection`
controls. `select all` adds every loaded archive entry to the selection; the
count reads `N of M entries selected` and `select all` disables once every
loaded entry is already selected. `restore selected` runs a best-effort bulk
restore: it copies the selected IDs, asks a plain `window.confirm` (not the
typed-count prompt reserved for permanent purge), calls the single-entry `POST
/api/archive/restore` endpoint once per selected ID, continues past per-entry
failures, drops successfully restored IDs from the selection, and reports
`restored X` or `restored X, Y failed` on partial failure. `purge selected`
reuses the typed `confirm_count` purge flow below, and `clear selection` empties
the current selection without restoring or purging any entry.

Permanent purge only applies to archive entries, not live plans. `POST
/api/archive/delete` accepts one or more `archive_ids` and requires a
typed `confirm_count` equal to the selected count; a mismatch returns
`reason: "confirmation-mismatch"` and leaves every entry in place. That route
is the only way an archive entry is emptied. Retention is a threshold rather
than a deadline: an entry whose age is at least `archive_retention_days` is
reported with `cleanup_state: "eligible"` and listed in
`archive_cleanup.candidate_ids`, and shortening the window marks more
candidates rather than destroying anything. An entry whose `archived_at`
cannot be read is `cleanup_state: "undated"` and is never a candidate, because
"I cannot tell how old this is" must not resolve to "old enough". No read
performs retention: neither `GET /api/archive` nor `GET /api/queue` empties an
archive entry or reclaims a plan lock, so the act of watching either view
changes nothing. Reclaim remains available where an operator asks for it, at
`POST /api/unlock`.

### One application, two modes

There is one HTTP application. `asl_workbench.http` declares the route
table, the role each route requires, the request policy, the static
bundle, and the pipeline that turns a request into an answer; it is
stdlib-only and imports nothing from `agent_session_loop`. A deployment
supplies three things and nothing else:

- a **provider**, which carries the operation out —
  `agent_session_loop.workbench_local.LocalProvider` on the Mac, where an
  operation reads the filesystem and spawns processes, and
  `asl_workbench.service.RemoteProvider` on the Pi, where the same
  operation is answered from retained observations, turned into a bounded
  read over the link, or journalled and handed down as a mutation;
- an **authenticator**, which decides what a caller proved:
  `LocalTokenAuthenticator` for the loopback server (see the bearer-token
  boundary below) and `OwnerIdentityAuthenticator` for the private
  network, where the owner is proved by the proxy's identity header and
  scoped credentials carry summary or connector roles;
- a **request policy** — `LOCAL_POLICY` or `REMOTE_POLICY` — which is the
  whole of the rest of the difference: whether a mutation must prove its
  origin, whether a body must declare its length, how large a body may
  be, and whether an unexpected failure is allowed to say what it was.

`agent_session_loop.workbench_server` is a compatibility adapter over that
application: it still resolves the CLI's roots, mints the loopback token,
serves the bundle, and prints the URL AgentMux opens, and the request
handler now translates a `BaseHTTPRequestHandler` request into a
`Request` and writes the `Response` back. Routing precedes
authentication, deliberately: a wrong method on a real path has always
answered `405`, and it still does whether or not a token came with it.

The routes are declared once, with a role each, so the two modes cannot
drift into disagreeing about who may read what. What they legitimately
differ on is what a given deployment can *do*, and that is reported
rather than discovered at the click — see `GET /api/capabilities`.
