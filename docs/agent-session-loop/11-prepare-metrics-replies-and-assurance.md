<!-- Continuation of the previous part. -->

The replacement root must differ from the current registered binding, every
declared repository must resolve beneath it as an exact Git top-level with the
expected credential-free origin, and the plan must have no active runner lock.
New preparation, legacy plans, unchanged bindings, and ordinary `asl run`
invocations reject the flag. After validating the replacement workspace,
prepare holds the plan lock and copies the plan into an isolated system-temporary
staging view while preserving symlinks.
It discards the staged copy's old aggregate and baseline cache, revalidates all
sessions, regenerates every expansion and correction, runs fresh baselines,
generates replacement metadata, and completes final loop-ready preflight there.
The original plan is untouched until one `PlanTransaction` publishes the new
metadata, validator aggregate, expansion files, corrected session text,
baseline cache, and readiness manifest. The transaction records immutable
payloads in a durable journal before replacing any destination: ordinary I/O
failures roll back immediately, while the next authoritative plan read rolls a
process interruption forward to the complete new generation. Publication and
recovery share a marker-scoped mutex; acquisition has a bounded wait, and a
timeout reports the lock path plus its recorded process and thread holder rather
than leaving plan readers blocked indefinitely. A failure before publication
discards the staged view, releases the lock, and preserves all old plan bytes;
staged blocker and step paths are rebased to the durable plan directory. Prepare JSON
adds `workspace_rebind` with credential-free `old` and `new` workspace and
repository projections, a `failure_stage` and `error` on failure, and the
relative `published_files` list on success. Failed rebind guidance retains
`--rebind-workspace` in its resumable command. Lock and concurrency races use
the distinct `workspace-rebind-failed` reason and include `workspace_rebind`
context rather than being classified as input errors.

`asl expand-ingest` is the standalone ingestion step `prepare` runs internally,
exposed for the manual `session-expand` path: it reads one validator-output
batch file (`--batch-file`), parses it fail-closed, merges it into the stored
`prepare-validation.json` aggregate (`ingest_validator_output`), (re)renders the
batch's `session-<id>.expand.md` files (`write_expansions`), and applies every
proposed factual correction for the batch (`apply_corrections`). The success
payload reports the ingested `sessions`, the written `files`, and per-correction
`corrections` outcomes — `applied`/`already-applied`/`blocked`, where a blocked
correction carries a stable `blocker` (`not-found`, `ambiguous`,
`contract-body`, `handoff-body`, or `structural`) — plus a `correction_summary`
count and `spark_missing`, the ingested session ids whose Spark eligibility
marker was absent and therefore pinned to baseline routing. A malformed payload
exits 1 with `reason: validator-schema` and the
`PlanError` message verbatim before any correction reaches disk; an unreadable
batch file exits 1 with `reason: input-error`.

`prepare` also writes `PLAN_DIR/plan-metadata.json` as soon as the authored or
resumed plan directory is bound, including plans retained after a later
preparation failure. Legacy plans retain the deliberately small version 1
schema:

```json
{
  "version": 1,
  "cwd": "/canonical/absolute/repository/path"
}
```

Registered plans write version 3 with `workspace_root` and the ordered registry
digest. Version 2 remains readable; the next prepare metadata write uses
version 3. Version 3 files written before the retired workspace inventory
policy was removed still carry a `workspace_inventory_excludes` array; the
loader accepts a well-formed one and discards it. On ordinary resume, `prepare`
refuses to rebind when the stored root,
digest, or validation aggregate differs from the resolved workspace; the
explicit staged replacement flow above is the only exception. `asl run` also
refuses a root or registry mismatch and never rewrites an existing binding;
only an unbound legacy plan is backfilled on a non-dry run. `--dry-run` does not
write metadata. Registered runner state and JSON surfaces add `workspace_root`,
`registry_digest`, and ordered resolved `repositories`, while retaining `cwd` as
the workspace-root compatibility alias. Resume validates those persisted workspace fields against
the current durable binding before continuing. The
metadata file is not plan prose and is excluded from both the plan content and
stable readiness hashes.

Source-plan session headings use a tolerant, fence-aware grammar shared by
`session-split`, `plan-iterate`, and `asl prepare`. Outside matching backtick or
tilde fences, the parser accepts case-insensitive H2-H6 ATX headings with zero
to three leading spaces, horizontal whitespace after the opening hashes, and a
positive dotted-decimal ID after the `Session` token. Source components may be
zero-padded. The ID may stand alone or introduce a non-empty title with `:`,
`-`, `–`, or `—`; horizontal whitespace around that separator is optional, and
whitespace-delimited ATX closing hashes are allowed. For example, all of these
are explicit boundaries:

```markdown
## Session 01
 ### session 02 - Add validation ###
###### SESSION 03.004 — Ship docs
```

Accepted source headings remain verbatim: preparation does not require
normalization approval or rewrite the source. Artifact identities are strict
and canonical, however, so `Session 03.004` produces ID `3.4`,
`session-3.4.md`, and unpadded manifest and completion-contract IDs. The
general-purpose `SessionId` API remains strict; zero padding is tolerated only
while parsing the source heading. An H2-H6 line outside a fence that begins
with the `Session` token followed by a digit (after optional whitespace and at
most one `#`, `:`, `.`, or `-`) but has a zero component, unsupported or
dangling separator, or another malformed suffix raises a line-specific
`PlanError`. A heading whose next word is not an id, such as
`### Session cookies`, is ordinary content.
The grammar-neutral structure gate surfaces that diagnostic as
`needs-plan-iteration` before authoring any output rather than treating the
candidate as body text or as a plan with no boundaries.

`--harness-model` and `--review-harness-model` are preserved in JSON payloads
and forwarded into both the emitted `asl run` command and any emitted
`asl prepare --resume` command. `--review-harness-model` requires
`--review-harness`. Preparation deliberately has no validator-model flag:
`--validator-harness` always uses that harness's native or configured default.
Stable JSON reasons: `ok`, `needs-plan-iteration`, `validation-failed`,
`baseline-unsatisfied`, `not-loop-ready`, `input-error`, `cwd-error`,
`plan-structure`, and `already-split` (the source plan already has a split
directory, listed in `details.existing_outputs`; rerun with `--resume` on it,
or pass `--force-new` for an intentional second split).

`metrics` aggregates workflow reliability and storage numbers from the
persisted state corpus, defaulting to the last 7 days (`--since-days DAYS`
widens or narrows the window). It reports turn totals and unsuccessful
runtime, first-pass session completion, pause counts by reason (including the
never-retry quota/auth/invalid-model classifications), verification repair
convergence (verify-failed pauses, exhaustion, and sessions repaired after a
failure), review repair-cycle dispositions with introduced/closed/repeated
finding counts, reopen attempts and successful/failed reopen materializations,
reopen attempts by source disposition, and reopen failures by result. Those
cycle and reopen activity counters are timestamp-windowed; recovered reopen
backfills supply lineage but are not fresh attempts. Review materializations
are timestamp-windowed too: they are deduplicated by source review across the
full `review_cycle` and `review_repair_reopened` history plus generated repair
children, then counted only when the event that carries one falls inside the
window. Reconciling against the whole history is what keeps one source review
from being counted twice for legacy lineage-less cycles, while a
materialization recovered from durable children alone has no timestamp and is
excluded, exactly as an undated turn or event is. `review_resume` remains the
whole-history cap authority after materialize/event-append crashes, so its
`historical_materializations` deliberately exceeds a narrow window's count.
JSON exposes the explicit
`review_cycles.unique_materializations`, `reopen_attempts`,
`successful_reopen_materializations`, `reopen_failures`,
`reopen_attempts_by_source_disposition`, and `reopen_failures_by_result`
fields while retaining
`review_cycles.repairs_materialized` as a compatible alias with the same
idempotent value. Metrics also report artifact corpus size and in-window
growth, and projected compaction savings (a dry-run over the state directories
active inside the window, net of retained failure-artifact bytes; use
`asl compact-state` for a full per-plan projection). Every state directory is
sized, since corpus size is not a windowed question, but one whose files were
all last written before the window opened is skipped before its event log is
read, so a narrow window costs a stat walk instead of a full-corpus read. Those compaction savings
cover operational JSON only; `asl gc` projects the age-based reclaim over
`runs/<run-id>/` directories, which is where corpus size actually accumulates,
and renders its byte totals the same way so the two commands compare directly.

Metrics also reports an `execution_timing` section — a text
`Execution timing` block and an additive JSON object — that answers where the
runs finishing inside the window spent their wall clock. It is rendered from
[the execution timing collector](12-assurance-in-metrics-retention-and-consumers.md#execution-timing-accounting), which is a pure
function of the event records and turn results this command already read: the
section adds no scan of its own. Its window is a whole-run window rather than a
per-record one. A run belongs to it by its **finish** time and then contributes
every second it lasted, including seconds before the window opened, because half
a run measures nothing; the report never spans two runs, so the idle gap between
invocations is accounted nowhere. The evidence behind it is read unfiltered so
lineage and retry ordering can see what the window hides: a retry whose first
attempt predates the window is still recognized as a repeat, and a check that
started before the window still counts against the run that finished inside it.

The phases are the collector's, with worker execution split by lineage into
`implementation` and `generated_repair`. That split is never inferred from the
shape of a session id — an authored plan may use dotted ids legitimately. A
session is a generated repair child when materialization lineage names it
(`repair_session_ids` or `repair_groups[].repair_session_id` on a
`review_cycle` or `review_repair_reopened` event) or when its own session body
carries the generated review-repair marker; dotted ids only decide which bodies
are worth reading. Turns whose `session_kind` no record states stay in
`turn_unclassified` and are never filed under implementation.

Coverage is reported next to every number, because a share is not a census.
Measured seconds aggregate over **every** included run, so a run missing one
phase still contributes the phases it recorded and the per-phase denominators
differ; `runs_with_phase` publishes them. Shares aggregate over `complete` runs
alone and are `null` — not zeroes — when no run qualifies, since absent evidence
does not support the claim that no time went anywhere. `runs_outside_share_basis`
says how much of the window the shares leave out. Cumulative verification check
work is reported beside the verification phase rather than inside it: a parallel
recipe's checks overlap, so summing them into a phase would claim the same
seconds twice. Checks are ranked by repository key, working directory, and
command, never by name alone, so two projects whose contracts both name a check
`tests` never merge; a check with neither a repository key nor a working
directory falls back to its plan key rather than merging on the shared name, and
`checks_without_command` says how many rows rest on a check name alone. Repeated
attempts count in-window turns that followed an earlier attempt of the same
session and report the seconds those attempts took. That time is **not**
described as discarded: a later attempt is usually what produced the work that
landed. Review-scope selections are reported with their `fallback_reason`
counts, which is how often a review widened to full scope because recorded
evidence could not prove a narrower one. Text output ranks the 10
highest-duration plans and the 10 highest-duration check scopes, with the count
shown out of the total; `--json` carries every plan and every scope in
deterministic order. Plans aggregate across resumed invocations, so one paused
and resumed plan is one row whose `runs` names how many invocations it covers.

Metrics also reports a `cli_invocations` section — a text `CLI invocations`
block and an additive JSON object — that turns the
[CLI invocation trace](14-safety-modes.md#cli-invocation-trace) into operational diagnostics. It
pairs `start` and `end` records by `invocation_id` and reports starts,
completions, successes, nonzero exits, usage errors, help calls, interruptions,
exceptions, and unmatched starts, plus counts by command, help depth, exit code,
harness, and origin, duration median/p95/max, and a bounded
`recent_failures` list of the 10 most recent failing, usage-error, interrupted,
or exception invocations. The `origin` counts are what separate a supervisor's
own `direct` calls from `autopilot`, `workbench`, and `install-verify`
automation. `trace_root` and `retention_days` name where the records live and
how long they survive, so a zeroed section is distinguishable from a missing
one. Recent failures carry only the trace's own closed field set —
`invocation_id`, `timestamp`, `command`, `options`, `help_depth`, `harness`,
`origin`, `outcome`, `exit_code`, `duration_ms`, `exception_type` — so the
diagnostic names the failing command surface without exposing what was typed.
The reader tolerates a degraded corpus rather than failing. `records_read`
counts every non-blank line the trace files hold and `records_ignored` counts
the ones that produced no usable invocation: a malformed or non-object line, a
record with no usable `invocation_id` or `event`, and an invocation with no
parseable timestamp. A duplicate half is skipped without being counted as
ignored, since its invocation is already summarized. A pair whose
two halves straddle midnight or two daily files is still counted once, an
invocation whose `start` was pruned is still summarized from its `end`, and an
invocation belongs to the window by its start timestamp so narrowing
`--since-days` never raises a count. The report also excludes its own
invocation, whose `start` record is already on disk while the handler runs and
would otherwise always appear as an unmatched start.

The command is read-only:
it never mutates state, events, or plan directories.

`asl questions` reads either an active live mailbox wait or the durable paused
turn for a plan and renders an external resolver prompt that another agent (or
a human) can use. It never invokes the harness and never writes an operator
reply. A live resolver is headed `# Live Agent Session - Resolver Prompt` and
offers three explicit outcomes: submit a factual answer with `asl inbox reply`,
close a plainly nonblocking question without an answer using `asl inbox resolve
--action continue`, or end the live wait for ordinary paused-run recovery using
`--action pause`. Its JSON adds `context_kind: "waiting"`, `question_id`,
`waiting_for_reply`, `reply_command`, `reply_stdin_command`,
`continue_command`, and `pause_command`. A durable paused resolver is headed
`# Paused Agent Session - Resolver Prompt` and retains its `asl run --reply`
resume guidance. Both JSON shapes include `ok`, `command: "questions"`,
`session_id`, `next_pending_id`, verbatim `last_message`, assembled
`rendered_prompt`, and `state_path`. If neither context exists, `--json`
produces `ok: false` with a stable `reason` (`no-paused-run`,
`missing-question-context`, `plan-not-found`, or `malformed-plan`) and exits
cleanly. These
external operator-facing prompts remain distinct from the in-harness
`## ASL reply context` and `## ASL resolution context` blocks.

`asl review-verdict` is the authoritative, read-only surface for a plan's
final-review verdict. It merges the shared `load_review_verdict` payload (its
`ok`/`reason` and, when the structured `final-review.json` is present, the full
untruncated `findings` list) with the read-only continuation evaluation under
`review_resume`, then attaches the complete `summary` of `final-review.md` with
`summary_truncated: false` and `summary_total_chars`, plus `next_steps`
remediation commands. Unlike the autopilot helper's bounded excerpt — which
flags `summary_truncated` and points here via a `full_review` hint — this
command never truncates, so it is the escape hatch when a long review would
otherwise be clipped. It never mutates state, events, or plan directories.

`asl run --reply TEXT` resumes a paused plan by injecting the operator's reply
as a one-shot prefix on the next harness turn only. The reply is expanded into
a `## ASL reply context` block above the regular session prompt;
`build_reply_prompt` always reuses `build_session_prompt` byte-for-byte for the
first line. Under `### Operator reply`, the stripped body is the `text` value
of one ASCII JSON object between `BEGIN ASL OPERATOR REPLY JSONL` and
`END ASL OPERATOR REPLY JSONL`; the argv, stdin, and mailbox origins share this
representation. The runner persists the resume metadata as a `reply_provided`
event (no reply text) and tags the resumed turn with `prompt_kind: "reply"`
plus a `reply_source` summary; the literal reply text is never stored in
`state.json`, `events.jsonl`, `argv`, or turn artifacts (the persisted argv
substitutes a placeholder like `<asl reply prompt redacted: N chars>` for the
full in-harness reply prompt).
The reply is consumed only after the resumed harness turn starts; pre-harness
failures such as an uncleared `not-loop-ready` gate preserve the previous
paused `state.json` and `events.jsonl` so the same reply can be retried after
repair.
Subsequent turns in the same run drop back to the plain session prompt. The
flag is allowed only when the most recent turn paused with a non-empty
`last-message.txt`; otherwise `run --reply` exits non-zero with `reason:
no-paused-run`, `reason: missing-question-context`, `reason:
reply-context-mismatch` (paused turn references a different session than the
one currently pending), or `reason: empty-reply` (CLI-only validation; the
flag's argument was empty or whitespace). `run --dry-run --reply TEXT` prints
the additional `Resolver prompt:` block above the regular `Prompt:` and
`Argv:` blocks so operators can preview both surfaces without touching the
harness, consuming the paused state, or writing the preview payload to
`state.json`/`events.jsonl`.

`asl run --reply-stdin` is the same resume as `--reply TEXT`, except the reply
text is read from stdin instead of an argv value, so it never appears in the
process listing. `--reply` and `--reply-stdin` are mutually exclusive. This is
the channel the `$asl` autopilot skill drives when it resumes a paused run (see
[Autopilot skill](19-autopilot-skill-asl.md#autopilot-skill-asl)); operators typically use `--reply TEXT`
directly.

`asl assurance list` and `asl assurance show` are the read-only operator surface
over the assurance inbox. Both read the append-only `assurance-inbox.jsonl`
ledger through the same projection every other consumer uses, under a shared
lock; neither appends a record, mints an issue id, rebuilds the index cache, or
repairs a malformed line, so browsing a damaged inbox reports the damage instead
of quietly rewriting it. A missing ledger is an empty inbox, not an error.

The assurance ledger and the live per-plan mailbox are separate systems, and
nothing connects them. `assurance-inbox.jsonl` sits directly under the state
root, is shared by every plan in the corpus, records what audits found after
runs ended, is permanent, and is written under its own `assurance-inbox.lock`.
`mailbox.jsonl` sits inside one plan's state directory, carries guidance,
questions, replies, and notices for a run that is still going, is enabled per
run, and is written under its own mailbox locking with a separate
`mailbox-lease.json` supervisor lease. They share no file, no lock, no id
space, and no lifetime; `asl inbox ...` never reads or writes the assurance
ledger, and no `asl assurance ...` command writes a mailbox. The one crossing is
one-way and read-only: collecting a run's audit evidence bundle reads that run's
slice of `mailbox.jsonl` directly, without the mailbox's lock, so an
operator's ruling is visible to the audit that grades the run it ruled on.

`list` answers the default triage question — what is still open — by printing
the 50 most recently observed open issues, newest sighting first, with the
issue id, severity, state, occurrence count, last-seen timestamp, component,
the identity key naming the issue (`null` in JSON when no key does), and a
one-line summary preview per row. `--limit ROWS` widens or narrows the cap and
`--state`/`--severity` re-aim the filters; `--all` is the negation of the whole
default, meaning every state with no row cap, and is refused in combination with
`--limit` or `--state` rather than silently picking a winner. When the cap hides
matching issues the text form says how many and how to see them. Unusable filter
values exit `1` with `reason: input-error` before the ledger is opened.

`show ISSUE` prints everything the ledger records about one issue: current
state, the fingerprint metadata it deduplicates by (category, component,
invariant key, lane, schema version), the identity key the issue is named by
and any key it absorbed, the full summary and recommendation, observation
history with the run and audit artifact behind each sighting, recorded
dispositions, absorbed ids, reopen count, improvement plan drafts, and the
affected runs with their artifact references and closeouts. Occurrence
history is capped the same way rows are, with the newest kept and the remainder
counted. Because a merge redirects an id rather than deleting it, showing a
merged-away id succeeds and reports both the id asked for and the id it landed
on. An id that names nothing exits `1` with `reason: unknown-issue`.

Both surfaces name an issue the way the projection does. An identity key is
resolved at replay rather than at write time: the stored binding keeps naming
the issue it was written against, and the projection follows the settled merge
chain, so a key keeps naming whoever owns that history today without any record
being reinterpreted. An issue carries every key that resolves to it, which is
more than one exactly when it absorbed a named issue, and one deterministic
primary identity: its own key when that key is bound, and otherwise the
lowest-numbered key it absorbed. An issue no key names prints as `unbound`
rather than being left out. That same replay builds one identity catalog per
projection, covering every lifecycle state rather than the open backlog and
indexed by fingerprint, identity key, invariant key, normalized localization,
and normalized summary token, because a defect that recurs after being
resolved, dismissed, or accepted has to route back to the issue that already
owns it. Localization normalizes to the file a pointer names and a summary to
its sorted content words, with negations deliberately never treated as
stopwords; neither normalization changes a fingerprint or the text a human
reads. Catalog lookups are exact and never follow a merge chain, so a stale id
resolves to nothing instead of being silently redirected. The catalog also
carries a digest of the identity state it describes, covering each entry's
issue id, lifecycle state, fingerprints, and identity keys and deliberately not
its occurrence count or last-seen timestamp: a decision reviewed against a
catalog stays valid while a known defect merely recurs, and is invalidated only
when an issue, a fingerprint, a key, or a state actually moves. Each entry also
carries its invariant key split into segments, which is what tells a rewritten
key from an unrelated defect in the same file. Those indexes are what a
candidate query reads: it returns the issues that could already own a finding,
ranked by how many ways they matched, with the reason and the summary
similarity on each. Candidates are proposals, so nothing in the projection acts
on one.

Both commands accept the canonical `ASI-42` and the documented bare `42`
shorthand, and both always emit the canonical form, so any id printed by `run`,
`status`, or `list` pastes straight into `show`. One payload builder feeds the
text and JSON renderers, so the two surfaces cannot disagree about what an issue
is. Ledger lines the projection could not read are reported as warnings rather
than failures — on stderr in the text form, and as `warnings`, `warning_count`,
`warnings_omitted`, `rejected_count`, and `anomaly_count` in the JSON payload —
because the rest of the file is still history a human needs and hiding the
rejected lines would hide the only evidence that the ledger is damaged. `show`
prints those warnings before an `unknown-issue` error, since a rejected line is
often the reason a legitimately copied id no longer resolves.

`asl assurance resolve`, `dismiss`, and `accept-risk` are how an issue is
managed. Each appends one record carrying the decision and an optional
`--note TEXT`; nothing is edited and nothing is deleted, so revising a decision
leaves both records and the order they were reached in on file. None of them
removes the issue, and the printed confirmation says what the new state claims
about the future rather than that the issue is gone: a recurrence after
`resolve` reopens it, while a recurrence after `dismiss` or `accept-risk` is
recorded without reopening it. A disposition aimed at a merged-away id lands on
the issue that owns its history today and reports both ids. An id that names
nothing exits `1` with `reason: unknown-issue`.

`asl assurance merge ISSUE TARGET` moves one issue's whole history into
another. It cannot be undone, so a person at a terminal is asked to confirm
first; `--yes` skips that prompt, and a piped or `--json` invocation is treated
as a deliberate one and is never prompted. Declining exits `1` with
`reason: cancelled` and writes nothing. Every refusal reports the projection's
own reason — `merge-self`, `merge-unknown-source`, `merge-unknown-target`,
`merge-source-already-merged`, `merge-target-already-merged`, or `merge-cycle`.

`asl assurance reconcile` is how an inbox written before identity keys existed
is repaired, and it is deliberately two commands in one: a preview nobody has to
trust, and an apply of a decision somebody made. Run without `--apply` it reads
the projection under a shared lock and writes nothing at all — not even the
manifest it prints — because a command that left a file behind would invite
applying one nobody read. It reports how many canonical issues exist and how
many already carry an identity key, the catalog digest it read them at, the safe
bindings it can add, the bindings it cannot, and the pairs of issues that look
like one defect. A safe binding is only ever an issue's own minted
`issue.asi-N`; nothing is derived from a category, component, invariant key, or
summary, so rewording a defect can never rename the issue that owns it. A key
some other issue already owns is reported as blocked with the projection's own
`identity-key-conflict` reason rather than quietly skipped. Pairs come from the
catalog's semantic candidates — shared invariant key, shared localization, or
summaries at least `0.60` similar — and each pair is asked about once rather
than from both sides. A pair a previous review already ruled two defects is not
offered again; those are counted in `suppressed_pairs` so the difference between
a settled inbox and matching that stopped working stays visible. At most 200
pairs are offered at a time; weaker ones are counted in `truncated_pairs` and
shown by reconciling again after the first manifest lands, so a large backlog is
worked through in reviewable batches instead of arriving as one unreadable
document.

The preview's `manifest` key is that document: schema
`assurance-reconciliation-manifest` version 1, carrying the catalog digest it
was generated at, when it was generated, and up to 1000 decisions. A decision is
exactly one of `bind` (name an issue with an identity key), `merge` (fold one
issue into another), `keep-distinct` (these are two defects), or `review` (the
preview's own suggestion, which is a question rather than an answer). The
preview emits `bind` for every safe binding and `review` for every candidate
pair, and never emits `merge`: semantic similarity identifies candidates and a
human decides them. Extracting the manifest with
`asl assurance reconcile --json | jq .manifest > manifest.json`, rewriting each
`review` as `merge` or `keep-distinct`, and running
`asl assurance reconcile --apply manifest.json --yes` is the whole protocol.

`--apply` writes every decision as one append under one exclusive lock, or
writes nothing. A manifest still carrying a `review` decision exits `1` with
`reason: unresolved-decision` before the ledger is opened, which is what makes
"the tool never merges on a guess" structural rather than a convention. Inside
the transaction the checks run in a fixed order, and that order is what lets
apply be both stale-safe and idempotent: decisions the ledger already records
are dropped first, so a manifest that is fully applied is a no-op reporting
`applied: false` and zero records; only then is the manifest's catalog digest
compared against the digest of the inbox as it stands, and a mismatch exits `1`
with `reason: stale-catalog-digest`, because a verdict reviewed against a
catalog that has since moved was reached about issues that may no longer be the
ones named; only then is each remaining decision validated. The cost of that
order is worth naming: a manifest whose decisions were applied halfway by some
other route is refused as stale rather than completed, and reconciling again is
the way forward. Bindings are written ahead of merges within the single write,
since a binding's issue must still be canonical when its record replays, which
is precisely what lets one manifest bind `issue.asi-123` to ASI-123 and merge
ASI-123 into ASI-80 — the key keeps resolving to ASI-80 through the merge chain
afterwards. Decisions are also checked against each other rather than against a
frozen snapshot, so a manifest binding one key twice or merging one issue into
two targets is refused instead of having its last decision silently win. A
refused decision takes the whole manifest with it: nothing partial is ever
appended. `keep-distinct` writes an `issue_kept_distinct` ruling of its own,
between the bindings and the merges, because a review nobody stored is a review
the next preview asks for again. The ruling carries the identity digest each
named issue was reviewed at, so it suppresses that pair only while both issues
still read the way they did: an issue that absorbs a materially different
sighting is a different question and gets asked again, and a ruling naming an
issue that has since merged away is refused with `issue-not-canonical` rather
than answering a question the merge already answered the other way. A stored
ruling counts as already recorded only when the projection accepted it, so a
record the replay reported as an anomaly — one naming an issue nothing had
observed when it landed — cannot make apply skip the fresh verdict the preview
is still asking for; the anomaly stays on the ledger as evidence either way. A
manifest
containing merges asks for confirmation when a person is watching,
takes `--yes` for scripts, and is never prompted under `--json`; declining exits
`1` with `reason: cancelled`. A manifest that cannot be read or parsed exits `1`
with `reason: manifest-read-failed` before the ledger is touched.

`asl assurance closeout RUN_ID --outcome accepted|reworked|escaped` records what
one audited run's findings were worth, with optional `--rework-minutes N`,
repeatable `--issue ISSUE`, `--note TEXT`, `--plan PLAN_DIR_OR_ID`, and
`--replace`. The run id resolves through the state corpus: plan attribution is
recovered from whichever plan state directory holds the run, and `--plan` is
required only when several plans hold the same id, which exits `1` with
`reason: ambiguous-run` and lists the candidates. A `--plan` that never produced
the run exits `1` with `reason: unknown-run`, and a run directory that has been
pruned is not an obstacle, because the ledger rather than the filesystem decides
what can be closed out. An outcome outside the vocabulary and a negative or
non-finite `--rework-minutes` exit `1` with `reason: input-error` before the
ledger is opened; a run with no audit record exits `1` with
`reason: unknown-run`, an `--issue` the inbox never opened exits `1` with
`reason: unknown-issue`, and a run that already has a closeout exits `1` with
`reason: duplicate-closeout` unless `--replace` is passed. Replacing is an
append like everything else: the new record names the ledger line holding the
closeout it supersedes as `seq-N`, both verdicts stay readable, and the later
one is the one in force.
