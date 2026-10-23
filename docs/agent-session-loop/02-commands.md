## Commands

```text
asl run PLAN_DIR [--cwd ROOT] [--harness codex|claude|copilot] [--harness-model MODEL] [--review-harness codex|claude|copilot] [--review-harness-model MODEL] [--effort EFFORT] [--timeout SEC] [--audit-harness codex|claude|copilot] [--audit-harness-model MODEL] [--audit-effort EFFORT] [--audit-timeout SEC] [--supervisor-audit|--no-supervisor-audit] [--max-sessions N] [--max-pending-sessions N] [--max-review-fix-cycles N] [--max-repair-group-attempts N] [--max-review-fix-cycles-ceiling N] [--review-guard-exclude PATH] [--review-guard-include-ignored] [--allow-external-changes ALIAS] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--unsafe-bypass|--no-unsafe-bypass] [--no-mailbox] [--allow-not-loop-ready] [--reply TEXT|--reply-stdin] [--json] [--dry-run]
asl preflight PLAN_DIR [--cwd ROOT] [--harness codex|claude|copilot] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--max-pending-sessions N] [--mark-loop-ready] [--json]
asl verify PLAN_DIR --session ID [--cwd ROOT] [--artifact-dir DIR] [--json]
asl contract-check PLAN_DIR [--session ID] [--cwd ROOT] [--agent-mux] [--state-dir DIR] [--json]
asl harness-compat [--harness codex|claude|copilot ...] [--strict] [--json]
asl baseline PLAN_DIR [--cwd ROOT] [--per-check-timeout SECONDS] [--total-timeout SECONDS] [--json]
asl split (--plan-file PATH | --inline-stdin) --contracts-file PATH [--cwd ROOT] [--base-dir DIR] [--force-new] [--skill-prefix auto|codex|claude|copilot] [--analysis-file PATH] [--json]
asl prepare PLAN.md [--cwd ROOT] [--harness codex|claude|copilot] [--harness-model MODEL] [--validator-harness H] [--review-harness H] [--review-harness-model MODEL] [--base-dir DIR] [--force-new] [--state-dir DIR] [--unsafe-bypass|--no-unsafe-bypass] [--json]
asl prepare --resume PLAN_DIR [--rebind-workspace ...]
asl expand-ingest PLAN_DIR --batch-file PATH [--json]
asl register-repositories (PLAN_DIR | --plan-file PATH | --inline-stdin) [--cwd ROOT] [--output PATH] [--state-dir DIR] [--json]
asl plans [--stale-days N] [--never-run] [--readiness STATE] [--json]
asl status PLAN_DIR_OR_ID [--state-dir DIR] [--json]
asl questions PLAN_DIR [--state-dir DIR] [--json]
asl review-verdict PLAN_DIR [--state-dir DIR] [--json]
asl tail PLAN_DIR [--state-dir DIR] [--lines N] [--no-follow]
asl sessions [--running] [--recent] [--limit N] [--state-dir DIR] [--verbose] [--json]
asl doctor --harness codex|claude|copilot [--harness-model MODEL] [--effort EFFORT] [--cwd ROOT] --plan-dir PLAN_DIR [--unsafe-bypass|--no-unsafe-bypass] [--json]
asl supervisor-guide [--harness auto|codex|claude|copilot] [--json]
asl inbox guidance PLAN_DIR_OR_ID (--text TEXT | --text-stdin) [--wait-delivered] [--wait-timeout SECONDS] [--state-dir DIR] [--json]
asl inbox reply PLAN_DIR_OR_ID --to QUESTION_ID (--text TEXT | --text-stdin) [--wait-consumed] [--wait-timeout SECONDS] [--state-dir DIR] [--json]
asl inbox resolve PLAN_DIR_OR_ID --to QUESTION_ID --action continue|pause [--note TEXT|--note-stdin] [--wait-consumed] [--wait-timeout SECONDS] [--state-dir DIR] [--json]
asl inbox read PLAN_DIR_OR_ID [--peek] [--state-dir DIR] [--json]
asl inbox acknowledge PLAN_DIR_OR_ID [--redirect-id OPERATION_ID ...] --attempt TOKEN [--state-dir DIR] [--json]
asl ask PLAN_DIR_OR_ID (TEXT | --question-id QUESTION_ID) [--wait] [--wait-timeout SECONDS] [--state-dir DIR] [--json]
asl notify PLAN_DIR_OR_ID (TEXT | --text-stdin) [--state-dir DIR] [--json]
asl outbox wait PLAN_DIR_OR_ID [--include questions|notices|all] [--supervision-id UUID] [--since SEQ] [--timeout SECONDS] [--state-dir DIR] [--json]
asl outbox supervise PLAN_DIR_OR_ID --supervision-id UUID [--timeout SECONDS] [--state-dir DIR] [--json]
asl assurance list [--limit ROWS|--all] [--state open|resolved|dismissed|accepted-risk] [--severity low|medium|high|critical] [--state-dir DIR] [--json]
asl assurance show ISSUE [--state-dir DIR] [--json]
asl assurance resolve ISSUE [--note TEXT] [--state-dir DIR] [--json]
asl assurance dismiss ISSUE [--note TEXT] [--state-dir DIR] [--json]
asl assurance accept-risk ISSUE [--note TEXT] [--state-dir DIR] [--json]
asl assurance merge ISSUE TARGET [--note TEXT] [--yes] [--state-dir DIR] [--json]
asl assurance reconcile [--apply MANIFEST [--yes]] [--state-dir DIR] [--json]
asl assurance closeout RUN_ID --outcome accepted|reworked|escaped [--rework-minutes N] [--issue ISSUE ...] [--note TEXT] [--plan PLAN_DIR_OR_ID] [--replace] [--state-dir DIR] [--json]
asl assurance process-pending [--limit RUNS] [--run-id RUN_ID] [--plan PLAN_DIR_OR_ID] [--audit-harness NAME] [--audit-harness-model MODEL] [--audit-effort LEVEL] [--audit-timeout SECONDS] [--state-dir DIR] [--json]
asl assurance plan ISSUE --harness NAME --output PATH [--harness-model MODEL] [--effort LEVEL] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control stop-after-current PLAN_DIR_OR_ID [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control abort PLAN_DIR_OR_ID [--grace SECONDS] [--reason TEXT] [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control select-harness PLAN_DIR_OR_ID --harness codex|claude|copilot [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control set-model PLAN_DIR_OR_ID [--harness codex|claude|copilot] [--harness-model MODEL|--clear-harness-model] [--effort EFFORT|--clear-effort] [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control guard add PLAN_DIR_OR_ID [--deny-command "CMD ARG ..."] [--protect-path ALIAS:PATH] [--reason TEXT] [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control guard remove PLAN_DIR_OR_ID --id GUARD_ID [--wait] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control guard list PLAN_DIR_OR_ID [--state-dir DIR] [--json]
asl control redirect PLAN_DIR_OR_ID (--text TEXT | --text-stdin) [--session N] [--turn N] [--run-id RUN_ID] [--operation-id ID] [--ttl SECONDS] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control redirect-status PLAN_DIR_OR_ID [--redirect-id ID ...] [--wait-acknowledged] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control recover-conversation PLAN_DIR_OR_ID [--state-dir DIR] [--json]
asl unlock PLAN_DIR [--state-dir DIR] [--force] [--json]
asl compact-state PLAN_DIR [--state-dir DIR] [--apply] [--json]
asl gc [PLAN_DIR] [--state-dir DIR] [--max-age-days DAYS] [--dry-run|--apply] [--json]
asl maintenance install [--executable PATH] [--state-dir DIR] [--hour HOUR] [--minute MINUTE] [--max-runtime-seconds SECONDS] [--no-bootstrap] [--json]
asl maintenance run [--state-dir DIR] [--dry-run|--apply] [--force] [--max-age-days DAYS] [--max-runtime-seconds SECONDS] [--json]
asl maintenance status [--state-dir DIR] [--executable PATH] [--no-probe] [--json]
asl maintenance uninstall [--state-dir DIR] [--no-bootstrap] [--json]
asl migrate-state PLAN_DIR [--from OLD_PLAN_PATH] [--state-dir DIR] [--apply] [--json]
asl metrics [--since-days N] [--state-dir DIR] [--json]
asl commit [PATH] [--yes] [--verify|--no-verify] [--staged] [--dry-run] [--guidance TEXT] [--from-id SHA] [--pathspec PATH ...] [--expect-snapshot TOKEN] [--harness codex|claude|copilot] [--model MODEL] [--effort EFFORT] [--timeout SECONDS] [--prepare-only] [--message-file PATH] [--json]
asl bridge serve [--stdio] [--protocol VERSION] [--state-dir DIR] [--plan-root DIR]
asl agent-mux pairing [--state-dir DIR] [--migrate] [--json]
asl diagnostics export --agent-mux [--state-dir DIR] [--run-id ID] [--plan-dir DIR] [--output PATH] [--json]
asl overseer scan [--plan PLAN_DIR ...] [--project DIR ...] [--pause-window-hours H] [--state-dir DIR] [--json]
asl overseer watch --watcher-id ID [--plan PLAN_DIR ...] [--project DIR ...] [--for-seconds SEC] [--interval-seconds SEC] [--sweep-seconds SEC] [--lease-seconds SEC] [--pause-window-hours H] [--release] [--timeout SEC] [--state-dir DIR] [--json]
asl overseer inspect PLAN_DIR [--kind KIND] [--summary TEXT] [--incident-id ID] [--expand status|questions|run_config|verification|worker_changes|supervision ...] [--limit CHARS] [--timeout SEC] [--state-dir DIR] [--json]
asl overseer decide PLAN_DIR [--operation reply|guidance|verify|commit|resume] [--incident-id ID] [--raises TOPIC ...] [--list-statements] [--claims-file PATH|--claims-stdin] [--reviewed-evidence DIGEST] [--no-history] [--generated-class CLASS ...] [--timeout SEC] [--state-dir DIR] [--json]
asl overseer act PLAN_DIR --watcher-id ID --operation reply|guidance|verify|commit|resume --run-id ID --incident-id ID --token TOKEN --scope DIGEST [--raises TOPIC ...] [--question-id ID] [--text TEXT] [--check NAME] [--message TEXT] [--session-id ID] [--claims-file PATH|--claims-stdin] [--reviewed-evidence DIGEST] [--no-history] [--generated-class CLASS ...] [--timeout SEC] [--state-dir DIR] [--json]
asl overseer reconcile PLAN_DIR [--timeout SEC] [--state-dir DIR] [--json]
```

### Overseer

`asl overseer` is the operator surface the `asl-overseer` skill drives. It is
the only supported way for a host agent to watch ASL runs it did not start: the
guards live in Python, the judgement lives in the host, and the two meet at
these six leaves.

The loop is observe, authorize, collect context, reason, dispatch, reconcile,
report. `scan` lists what discovery can see and asks AgentMux nothing, so its
`eligible` flag is ASL's own opinion and not permission. `watch` is what asks:
it sweeps the fleet, authorizes each plan in scope, and takes a lease on the
ones AgentMux allows, re-sweeping until `--for-seconds` elapses so runs that
start mid-invocation join without re-invoking anything. `--release` hands the
leases back; an abandoned lease expires on its own, but not before it has kept
another watcher out for its whole term. Selectors (`--plan`, `--project`) only
narrow, and exclusions take precedence over them — except that naming a plan
exactly relaxes the 24-hour pause window for that plan alone.

`inspect` opens an incident and captures a bounded packet: pending questions,
the run's own summary, and the `gaps` naming what could not be read. `--expand`
widens exactly one named source under a 4000-character, 50-record ceiling and
re-checks the grant each time, so context grows because the host asked for
something rather than because a run was verbose. The `scope` digest and action
`token` it prints are what `asl overseer act` must be given later; they are
captured at read time on purpose, so a run that moves in between produces a
`stale_action` refusal instead of an action aimed at a state that is gone.

`decide` resolves one operation against decisions humans already recorded.
`--list-statements` prints every statement in scope with the stable ID a claim
must cite, and the `evidence_digest` naming that reading; the host reads what
those statements mean and passes claims back through `--claims-file` or
`--claims-stdin`, together with that digest as `--reviewed-evidence`. Claims
arriving without one are refused `evidence_unbound`: claims about a record
nobody named are not a reading of anything. The register rejects claims that
cite nothing, widen their scope, or name an unknown topic, and reports each
rejection rather than failing the command. A refusal is an escalation: the
payload carries the question to put to a human, the work that stays blocked, and
the other eligible plans the invocation may keep working on meanwhile. It
carries no default, no timeout, and no deadline — there is nowhere to express
"proceed if nobody answers".

Acceptance, scope, design and safety are the questions that stay human, and a
decision on one of them never speaks to an operation: it is a settlement a
request has to *raise* before the ruling can weigh it. Both leaves raise two
things. `--raises TOPIC` (repeatable on `decide` and on `act`) is what a host
names when it knows a question is open, and a topic no in-scope decision settles
is refused `human_decision` however the operation itself was allowed — an
operational allowance is not an answer to a question nobody asked the person.
The register raises the rest: any human topic a surviving claim spoke to about
this run is carried whether or not the invocation named it, so a safety hold the
host read out of the conversation and claimed honestly cannot be stepped past by
an invocation that forgot to mention safety. Scope reads the same way in both
directions — a human decision made about another project, plan, run or incident
stays a precedent and raises nothing here, because a neighbour's hold no more
poses this run's questions than a neighbour's allowance settles them. Raising is
not settling: a topic is raised whether the claimed decision allows or holds, and
a hold, a withheld settlement and an absent one all read out the same way in the
ruling's `unresolved` list.

Statements come from two boundaries that are not interchangeable. A plan's
mailbox records that an instruction *arrived* and records no author, so every
statement it yields is relayed: citable as corroboration, never an authorization
on its own, and rejected `not_user_authored` when a claim rests on one alone.
Only the harness transcript carries a turn a person typed, and a transcript
statement is scoped to the project AgentMux authorized it under: the
conversation's working directory, resolved to whichever repository the run
declares that holds it, so a conversation started inside a checkout speaks for
that checkout. A project is a repository and never the directory above one — a
run's workspace may hold several checkouts nobody spoke about, so the scope a
request carries names the declared repository roots instead. A statement names
no plan, so a claim that wants one must narrow the scope itself or be rejected
`scope_widened`; and a plan declared over several repositories names all of
them, so a claim covering it has to cite a human statement in each, listed in
`corroborating`. One project's instruction never supplies another's authority. `decide` and `act` read both boundaries by default;
`--no-history` reads the mailbox alone, which authorizes nothing by
construction. Authorship comes from AgentMux's own classification of the whole
conversation, before the turn is read and with no flag to set: only a `main`
session contributes instructions, an `automated` one contributes generated
prompts, and `unknown`, an absent class and any spelling this build does not
know all contribute `unattributed` statements that authorize nothing.
`--generated-class CLASS` (repeatable) marks further classes machine-written;
it can only restrict, so no value it carries turns a class into a human one and
forgetting it costs no safeguard. A transcript that could not be read
comes back as a `history` object carrying its own `failure` and contributing no
statements, which keeps "no authorization was found" distinct from "the evidence
could not be reached".

`act` is the only leaf that changes anything, and it changes it only through the
guarded dispatcher, which refuses unless the plan is still observed, the run is
the one that was read, the scope has not widened, the watcher still holds the
lease, the token still matches, and a ruling still authorizes it. Authority is
re-derived from live supervision twice — before the adapter prepares and again
immediately before it performs — so a revocation landing in between stops the
action. Revocation stops later context reads and queued actions but never undoes
a dispatch that already happened. `--raises` is read once and handed to both
rechecks, and what the register itself raises is re-derived at each one, so
neither pass can authorize a human question the other refused.

Re-deriving catches a withdrawn grant and does not on its own catch a *new*
statement: claims cite what the host read, a hold nobody claimed never becomes a
decision, and the second reading of the same claims agrees with the first
however the record moved. `--reviewed-evidence DIGEST` closes that.
`decide --list-statements` prints an `evidence_digest` summarizing every human
statement in scope — the instructions and relays a host would have read, and
deliberately not ASL's own acks and receipts, which would otherwise report a
change on every action. That listing is the only reading that mints one, because
it is the only reading that *shows* the host the record it names. The host
carries it back to `decide --claims-file` and to `act`, and each refuses
`evidence_changed` when the record has moved since: at `decide`, closing the
window between the listing and the claims written from it; at the first
authority check, closing the window between `decide` and `act`; and at the
second, closing the window inside the dispatch. It is required whenever claims
are passed. An invocation that omits it is refused `evidence_unbound` rather
than bound to whatever it happens to observe — pinning a fresh read would be the
CLI writing the host's reading for it, and would absorb every hold that landed
beforehand into the record the host is then said to have reviewed. A refused
`decide` publishes no digest for the same reason read from the other end: the
new record's name in the host's hand would match at `act`, and the stale claims
would go through as though the hold had been reviewed. That is a rule about the
whole answer rather than about one field — no refusal at either leaf names the
record it observed, in its payload, its ruling summary, its escalation or the
lines it prints without `--json`, and `act`'s `evidence.observed` reports the
binding its checks confirmed or nothing at all. A record acquires a name by
being read, and `decide --list-statements` is the reading. `evidence_changed` is
not a retry: the reading was about a record that has changed, and the answer is
another reading. `commit` writes a checkpoint across every
declared repository as one durable group, so a crash partway through leaves a
group `reconcile` can finish. A recoverable refusal is retried at most twice
before it becomes `recovery_exhausted`.

The operational scope is `reply`, `guidance`, `verify`, `commit` and `resume`,
and the registry is closed: an operation no adapter implements is refused
`operation_unsupported` before the ledger is touched. `verify` runs one check
the plan already declared — `--check` names a check in the completion contract
of the session the run is on, and the argv comes from that contract rather than
from the action, so a different command means a different plan file. A name the
contract does not declare is refused `check_undeclared`, and a check that ran
and failed reports that failure rather than a refusal.
Application-code repairs stay with the worker session.

`reconcile` settles rows a dispatch left in doubt after dying between performing
an action and recording its receipt. Only the adapter can answer, and it answers
from the ledger row alone because the process that held the action is gone.
Running it before acting again is what keeps a retry from becoming a duplicate.

Every leaf exits `0` when the answer was yes, `1` on a refusal or a partial view
(an incomplete sweep, a withheld packet, an unsettled row), and `2` on a
malformed invocation, so a `&&` chain stops at the first refusal instead of
reading "could not look" as "nothing to do".

The overseer's contracts are shared with a program released on its own schedule,
so the version that matters is the one the two *installed* programs agree on.
`just overseer-integration asl_root=/path/to/agent-session-loop`, in the Agent Mux
checkout, is the gate for that: it drives these leaves against the `amx` in
`~/.local/bin` and the `asl` on `PATH`, in a throwaway world the suite builds
and deletes — its own repositories, state root, `config.toml`, profile home and
fake harness processes — and checks compatibility both ways, that the installed
binary answers the contracts this client speaks and that a component which does
not is refused rather than read past. It reads no operator configuration and
touches no real plan. The suite is `tests/test_asl_overseer_paired_smoke.py`; it
skips when nothing is installed and `AMX_ASL_OVERSEER_SMOKE` turns that skip
into a failure, which is what the recipe sets.

`asl commit` writes Conventional Commits messages for the worktree containing
`PATH` (default: the current directory), and is the same command the auto-commit
groups above invoke. It is usable standalone, outside any loop or plan. Staging
is chosen by flag before anything else happens: `--staged` trusts the index and
never touches it, `--pathspec PATH` (repeatable) stages exactly what it names and
leaves every other change alone, and the default stages everything. The captured
diff is budgeted to 150 KB and always inlined into the prompt. One command, four
modes:

- **default** — capture the staged diff, generate the message in a single
  read-only harness turn (`--harness`, `--model`, `--effort`, `--guidance`
  steer it), format it, and commit. `--dry-run` prints the message and stops
  without committing; `--yes` skips the confirmation prompt, as does any
  non-interactive invocation.
- **`--from-id SHA`** — print a message describing an existing commit. It never
  stages and never commits; apply it with `git commit --amend` if you want it.
- **`--prepare-only`** — emit a single-line JSON header (`mode`, `yes`,
  `no_verify`, `guidance`, `is_truncated`, `truncation_notes`, `from_id`,
  `snapshot`), a blank line, and the budgeted diff, then stop. No harness turn
  is taken; this is how the `gitsummary` skill hands the diff to its own host
  agent.
- **`--message-file PATH`** — commit the supplied message instead of generating
  one, with `-` reading stdin. It is formatted and stripped of disallowed
  attribution trailers exactly as a generated message would be, and it is the
  one mode that needs no harness at all.

The generation turn is bounded. `--timeout SECONDS` (default: 900) moves that
bound and cannot remove it: a value of zero or less is refused as an invalid
argument before anything is staged. When the bound expires, the harness process
group is killed and swept, nothing is committed, whatever was staged is left
staged for a retry, and the failure is reported under reason
`generation-timeout` — the same soft-failure path as any other refused commit,
not a hang. `--from-id` and the default mode are the two modes the bound
applies to; `--prepare-only` and `--message-file` take no harness turn at all.

A scoped commit is also snapshot-checked. `git commit --only` takes the named
paths from the worktree, not from the index, so the content it commits is
whatever those paths hold at commit time — minutes after the diff the message
describes was captured. Before committing, `--pathspec` re-checks that every
named path still matches the captured snapshot and refuses with reason
`snapshot-drifted` when one has been edited, deleted, or restaged in the
meantime; nothing is committed and no index entry is changed. The two-step
skill flow spans two processes, so `--prepare-only` reports that snapshot as
the header's `snapshot` token and the committing call passes it back with
`--expect-snapshot TOKEN`, which is checked before staging and again before
committing. `--expect-snapshot` names a scoped snapshot and is refused without
`--pathspec`.

`--verify` runs git hooks; `--no-verify` is the default. Stdout carries the
commit message and nothing else, so the command can be piped or redirected;
progress, truncation warnings, and the resulting SHA go to stderr. `--json`
replaces both with one scriptable payload.
