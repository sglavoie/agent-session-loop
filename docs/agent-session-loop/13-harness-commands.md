## Harness Commands

The built-in adapters construct argv lists directly and never use shell
interpolation.

Codex runs over the app-server transport, so its argv names only the binary and
everything else travels the JSON-RPC conversation:

```text
codex app-server
```

The turn plan the client then sends carries what argv used to. `thread/start`
takes the working directory, `approvalPolicy: never`, the sandbox policy, the
writable roots, and the model; `turn/start` takes the reasoning effort and the
prompt. The prompt is therefore not a command-line argument at all and does not
appear in a process listing. `thread/start` echoes the sandbox it actually
applied, and the client refuses to send the turn if that echo is not the profile
that was asked for.

A turn that continues a conversation sends `thread/resume` instead, carrying the
same plan plus the exact `threadId` it was handed — there is no "resume the
latest" call anywhere in the client. A restored thread carries the settings of
the thread it restores, so its echo is re-checked further: a returned thread id
that is not the one asked for, a sandbox wider than the requested profile, or a
`cwd` or `approvalPolicy` that disagrees with this call is refused before any
turn is sent. The model is reported through `observed_model` rather than
refused, because the server canonicalizes model names. Whichever call opened the
thread, its identifier is persisted before `turn/start` is sent, and the turn is
abandoned unsubmitted if that write fails.

`turn/interrupt` stops the turn this session started, naming its exact thread and
turn. It is sent immediately before the wait loop's forced kill and again from
the transport's shutdown while a turn is still live, so the conversation records
an `interrupted` turn rather than one that stopped answering. An interrupted turn
is not a failed turn: only `status: failed` becomes a protocol error, and a
refused or unanswered interrupt is not recorded as one either, since the turn is
ending regardless.

The one-shot form is still built, and is used only when the app-server
conversation fails *before* the turn is accepted and before any work is
emitted — the one state in which running the turn again cannot run it twice.
A turn that asked to resume has no such window at all, because `codex exec`
starts a fresh session and falling back would answer "continue this
conversation" with one that continues nothing:

```text
codex exec -C <cwd> --add-dir <plan-dir> --dangerously-bypass-approvals-and-sandbox [--model <model>] [-c model_reasoning_effort="<effort>"] -o <last-message-file> <prompt>
```

`invocation.transport` in the turn's persisted result and in `--dry-run` says
which of the two ran: `app-server` or `exec`. Under `app-server` the invocation
also carries a prompt-free `turn_plan`, which `--dry-run` prints below argv so
an operator can still read the access profile off a planned turn.

`codex exec` submits its prompt on argv, so it has no handshake in which to learn
a conversation first. It reads the session id out of its own output instead,
which is continuity observed after the fact: the work is already submitted, so a
capture that fails there cannot retract anything and the turn reports
`continuity_unavailable` rather than being abandoned. A result says the same
thing when the output named no session id at all, and only when a caller asked —
an ordinary one-shot turn is not missing something it was never meant to have.

Claude runs over bidirectional stream JSON, so its prompt is not on argv either:

```text
claude --print --verbose --output-format stream-json --include-partial-messages --input-format stream-json --replay-user-messages --add-dir <plan-dir> --permission-mode bypassPermissions [--model <model>] [--effort <effort>] [--session-id <uuid>|--resume <id>] --settings <overlay>
```

`--input-format stream-json` makes stdin a message stream. The prompt is
written to it as the conversation's first user message, and supervisor guidance
can be written to it while the turn runs. `invocation.transport` says
`stream-json` for such a turn.

A claude turn states its conversation on argv rather than discovering it.
`--session-id <uuid>` assigns the identifier the CLI would otherwise have
minted, and `--resume <id>` restores exactly the one named; `invocation.
assigned_conversation_id` carries whichever applies. Because it is settled
before the process is spawned, the turn persists its conversation *before* the
prompt is written, and a sink that fails abandons the turn with nothing
submitted (returncode 70, `conversation_error`) exactly as the codex handshake
does. `--fork-session` is never passed: it answers "continue conversation X"
with a different one. An audit turn runs with `--no-session-persistence`, so it
is assigned nothing and refuses a resume outright.

The `system`/`init` record echoes the session the CLI actually opened, and it is
re-checked against the stated one. A disagreement does not stop the turn — the
prompt went out before the first record arrived, so the model is already
changing the workspace — but it is rendered on the turn log as `claude session
mismatch: ...`, repeated on stderr, and reported as `continuity_unavailable`
with `conversation_id` cleared, so nothing resumes from an identifier the work
did not go into.

Interruption rides the same stdin stream, as the CLI's own control protocol
rather than an ASL invention: a `control_request` naming a `request_id` with
subtype `interrupt`, answered by a `control_response` carrying that identifier
back. Attribution is by `request_id` and nothing else, because arrival order is
exactly what lets a late answer to one request stand in for the next. The
request is sent once per turn, from the daemon thread beside the wait loop's
forced kill, while the stdout readers are still alive to deliver the answer; it
gives up on a five-second bound or the moment the process is gone, and neither
outcome changes what the kill does. Nothing is sent from the transport's
shutdown, unlike the app-server: by then the process has been waited on and the
readers abandoned, so a request from there could never be correlated.

Past an interrupt the turn stops accepting evidence for anything it might still
be asked to have done. The result that follows is drained rather than credited,
so it can neither settle guidance the model never ran nor leave a later result
looking like a resumed turn's own; an echo the CLI emits while draining stdin on
the way out is consumed for accounting but confirms nothing; and guidance
offered afterwards defers as `stale-turn` instead of being written. A receipt
the `PreToolUse` hook already wrote is the one exception, because the hook
writes one only after handing the text to a tool call that then ran.

`--settings <overlay>` names an ephemeral per-turn settings file created under a
temporary directory, holding exactly one `PreToolUse` hook entry and nothing
else. It is deleted when the turn ends, and the operator's own Claude
configuration is never read, written, or modified. Audit turns get no overlay:
an audit observes, and a hook that injected operator guidance into one would
make it something else.

Copilot, whose two access profiles use two different transports:

```text
copilot -C <cwd> --add-dir <plan-dir> [--allow-all-tools --allow-all-paths] --no-ask-user --silent [--model <model>] [--effort <effort>] --acp
copilot -C <cwd> --add-dir <read-root>... --available-tools=<audit tools> --deny-tool=<denied>... --disable-builtin-mcps --no-custom-instructions --no-remote --no-remote-export --disallow-temp-dir --no-ask-user --silent [--model <model>] [--effort <effort>] -p <prompt>
```

A worker turn runs over the Agent Client Protocol: `--acp` puts the same binary,
with the same flags, into a newline-delimited JSON-RPC conversation on stdin and
stdout instead of a one-shot `-p`. Everything that decides what the turn may do
is still on that command line — the repository grant, the bypass flags, the
compiled `--deny-tool` rules, the model, and the effort are all configured at
server launch rather than through the protocol — so moving the transport changes
nothing about the turn's access. What it changes is what the turn can be *told*
afterwards. The prompt travels as a `session/prompt` payload instead of an argv
word, so it never reaches a process listing or a crash report; the assistant
message arrives as streamed `agent_message_chunk` updates and becomes the turn's
last message directly, rather than being scraped back out of stdout; and the
turn ends with a stated `stopReason`, which is what lets a refused or truncated
turn be reported as one instead of as whatever exit code the agent happened to
leave behind.

The conversation is deliberately narrow. `initialize` advertises *no* filesystem
and *no* terminal capability, so the agent cannot ask ASL to read a file, write
one, or run a command on its behalf — the only access it has is the access its
launch argv granted. Any other client-side method it asks for is answered with
JSON-RPC "method not found" and logged as a refused unadvertised request.

`session/request_permission` is answered explicitly, and never blankly. The
responder restates the grant the launch argv already made and can never exceed
it: a turn launched with `--allow-all-tools --allow-all-paths` answers
`allow_once`, and a turn launched without them answers `reject_once`, because
`--no-ask-user` means there is no operator at this turn and approving on their
behalf would invent an authorization nobody gave. The two standing options,
`allow_always` and `reject_always`, are never selected — a single answer must not
stand for calls nobody has seen yet — and a request that offers only those is
declined with the protocol's own `cancelled` outcome. Every rejected or
cancelled request is reported on the turn's stderr. Guard rules are not
re-derived here: they are shell-command rules the agent applies to its own tool
calls before it would ever ask, and matching them again against a permission
payload could only be a weaker guess at the same rule.

An audit turn keeps the one-shot form exactly as it was. Its restrictions are
its command line — a fixed `--available-tools` set, denied tool families, no
built-in MCPs, no remote, no temp directory — and nothing about observing a
repository needs a conversation, so it does not get one. It refuses a resume
outright, both when its invocation is built and again at the transport, and a
caller that asks it which conversation it ran on is told that it names none
rather than handed a silent `None`. Both transports decline a mid-turn steer
with `unsupported-active-steer` rather than ignoring it; that behavior is
unchanged.

The ACP session carries the conversation. Its identifier is whatever
`session/new` returned or whatever `session/load` restored, and it is captured
between the session and the prompt — after there is a session to name and before
there is work to lose — so a sink that fails abandons the turn rather than
spending it into a conversation nothing could find again. Resuming calls
`session/load` and never `session/new`: a fresh session standing in for a failed
restore would be a new conversation wearing an old conversation's name. Four
things refuse the restore before the prompt is sent — an agent that never
advertised `loadSession` in its `initialize` answer, a load that failed, an
answer naming a different session than the one requested, and a replayed
transcript that could not be read — and the restore restates this turn's working
directory and empty MCP server list rather than inheriting whatever the session
was last opened with.

The replay itself has no analogue in the other two transports. `session/load`
streams the entire prior conversation back as `session/update` notifications
*before* it answers, and none of it belongs to this turn: it is not the turn's
log, not the turn's final message, not new activity by the resumed worker, and
not an acknowledgment. Replayed updates render nothing and are counted apart
from live ones, which is what keeps a replayed prompt echo out of a persisted
artifact and what lets the turn's stderr say how much transcript came back and
how much the worker actually did. A replay cannot finish a turn: only the
`session/prompt` response carries a stop reason.

Stopping a turn is said to the conversation as well as to the process.
`session/cancel` is a notification rather than a request — the protocol's
acknowledgment is the pending `session/prompt` returning `cancelled` — and it is
sent once, naming the session, beside the wait loop's forced kill. Nothing is
sent from shutdown, following claude's stream-JSON transport rather than the
codex app-server, because by then the process is reaped and the readers
abandoned. A cancellation that cannot be written reports that it was not sent
instead of recording a protocol error, since the turn is ending regardless.

There is no fallback from ACP to the one-shot form. It is the same binary with
the same flags reading its prompt from a pipe, so a conversation that fails after
`session/prompt` has already spent the turn, and re-running it would execute the
same work twice.

Harness prompts use the expected skill prefix:

```text
$session-run <plan-dir>/ --runner-managed
/session-run <plan-dir>/ --runner-managed
```

Codex writes a last-message file with `-o`. Claude streams JSONL in print mode;
the adapter renders that stream as concise `claude ...` stdout log lines for
live tailing and keeps the final `result` text as the last message. Copilot does
not use a last-message file: a worker turn reassembles its last message from the
ACP `agent_message_chunk` updates, and an audit turn's capture falls back to
stdout.
ASL's `--harness-model` is forwarded only to turns using the implementation
harness declared by `--harness`. `--review-harness-model` is forwarded only to
generated review turns and requires `--review-harness`; it never affects
implementation turns. Omitting either model flag preserves the corresponding
harness CLI default. Reporting still uses per-turn best-effort metadata from
Codex `~/.codex/config.toml`, Copilot `~/.copilot/settings.json`, or
`native CLI default` with source `unresolved` when the default cannot be read.
If a text TTY run switches implementation harnesses with `1`, `2`, or `3`, the
switched harness uses its native or configured default rather than inheriting
`--harness-model`. `--effort` remains shared and carries across implementation
and review turns, including later harness switches. `doctor --harness-model`
checks the selected harness/model pairing without adding a review role.

`model` and `model_source` report what was *requested*. Claude's stream also
names the model it actually ran, in the `system`/`init` record and again in each
`message_start`, and the adapter carries the first such name through to the turn
payload and `state.json` as `observed_model` with `observed_model_source:
harness_reported`. A copilot worker turn names one the same way, from the `model`
config option `session/new` echoes back — the only place a flag applied at server
launch can be confirmed rather than assumed. A model or effort echo that
disagrees with what was asked for is reported on the turn's stderr as a launch
control mismatch and nothing more: neither control changes what the turn is
allowed to do, so unlike an access profile it is surfaced rather than enforced.
The requested and observed values sit side by side rather than
overwriting each other, so an alias or a server-side substitution is visible
instead of silently reported as the request. Harnesses that name no model leave
both fields `null`. `observed_model_source` is observed, never selected, so it
never appears as a `model_source`.

The supervisor bundle spells that same request as `requested_model` and
`requested_model_source`, while retaining `model` and `model_source` as
compatibility aliases. These run-level fields are not an effective-model
verdict: `model_projection` is authoritative per role. In particular,
`requested_model_source: unresolved` beside a role whose projection resolved a
harness-reported model is expected evidence, not a contradiction.

`state.json` also carries `model_projection`, where the two are reconciled per
role. Every turn folds into one record keyed by its session kind and harness,
so implementation and review keep separate provenance and one harness's model is
never attributed to the other. Each record keeps `requested_model`,
`requested_model_source`, the sorted `observed_models`, and a `turns` count, and
resolves a `model`/`model_source` pair from them: a requested model is the run's
own answer and an observation cannot outrank it, while an `unresolved` request
resolves to the observation when every turn of that role reported the same
model, with source `harness_reported`. Disagreeing observations resolve to
nothing and leave the unresolved request standing, because two answers are not
an answer. The projection accumulates turn by turn rather than being recomputed
from the retained turn list, so trimming old turns cannot erase what the run
already observed. The supervisor audit reads it in the `state` section beside
`model` and `observed_model`.

### Harness Version Compatibility

`asl harness-compat [--harness NAME ...] [--strict] [--json]` reports which
installed harness CLIs fall inside the supported version window. The window is
pinned per harness in `agent_session_loop/harness_compatibility.py` as a
`latest`, a `previous`, a `minimum`, and an explicit `blocked` tuple, and a
resolved executable is classified `supported`, `unknown`, or `incompatible`.

The command never uses a model. The only subprocess it constructs is
`<executable> --version`, and a test asserts the recorded argv is exactly
`["--version"]`, so a scheduled compatibility smoke costs nothing but a process
spawn and cannot consume provider quota. Probes are memoized in process on
`(path, size, mtime_ns)`, bounded by a 20-second timeout, and disabled entirely
by `ASL_HARNESS_VERSION_PROBE=0` — in which case every harness degrades to
`unknown`.

Unknown warns, known-incompatible blocks. Default exit is 0 when nothing is
`incompatible` and 1 when something is; `--strict` exits 3 when any harness is
not `supported`, which is the mode the scheduled CI lane runs so that a new
upstream release surfaces as a warning to be triaged rather than as a red build.
Every probe failure — an unresolvable executable, a non-zero `--version`, a
timeout, unparseable output — degrades to `unknown`, never to `incompatible`, so
"not installed" stays distinguishable from "known broken" in release evidence.

`preflight` applies the same policy to the selected harness with one deliberate
asymmetry. A known-incompatible release is a `red` finding and blocks the run. An
unrecognised release is `green`: it is reported on every preflight and in the
preflight JSON, but it does not block. `yellow` is not available for the warning,
because `PreflightReport.ok` and `_blocking_findings` both treat yellow as a
blocker — a yellow warning would refuse every run against any harness release
newer than the pinned window, which is precisely the case that must stay
runnable.

### Live Harness Acceptance

Version compatibility says a harness is *installed and inside the window*. It
says nothing about whether that harness actually interrupts, resumes, and
carries corrections the way the redirect subsystem assumes. Those are behavioural
claims, and the only thing that can settle them is a real turn against the real
CLI. `tests/asl_live_redirect_acceptance.py` is where that happens, and it is the
only module in the tree permitted to spend provider quota.

```sh
python3 -m tests.asl_live_redirect_acceptance --all-harnesses            # probes only
python3 -m tests.asl_live_redirect_acceptance --all-harnesses --live     # spends quota
python3 -m tests.asl_live_redirect_acceptance --harness codex --live --json
```

It is opt-in and off by default. Without `--live` or `ASL_LIVE_ACCEPTANCE=1` it
launches no harness turn at all: it runs the same model-free `--version` probe
described above, reports every scenario as `skipped`, and exits 0. That default
is what lets a scheduled lane invoke it on the same footing as `harness-compat`.
It is deliberately absent from every shard list in `tests/run_suite.py` and named
so that `unittest discover` cannot pick it up; its deterministic behaviour — the
opt-in guarantee, the report shape, the refusal to generalise — is tested from
`tests/test_asl_harness_compatibility.py`, which is in the fast lane.

Each harness runs in a disposable git repository created and deleted per
sequence, against synthetic prompts, and each of the seven rows names one claim:

| Scenario | What a pass means |
| --- | --- |
| `earlier-context-survives` | A resumed turn recalled a passphrase held only in the prior turn's conversation. The prompt forbids writing it to disk and forbids searching for it, so the row cannot be satisfied by looking around. |
| `existing-edits-remain` | A file written before the interruption is still there, with the nonce it recorded. |
| `active-work-stops` | Arming the turn's `AbortSignal` during a long-running shell tool ended the turn in the `execution` window with returncode 137. |
| `native-cancellation` | `HarnessTransport.interrupt()` alone ended the turn — no kill, no timeout. The forced kill is held back as a safety net so a harness that ignores its own protocol fails the row instead of hanging the check. |
| `forced-termination` | `process_tree.confirm_termination` reports `complete` for the worker group and every descendant tracked while it was alive. "Killed" and "collected" are separate claims; this is the second. |
| `correction-reaches-continuation` | A correction went through the real journal — `submit_request`, `accept_request`, `carry_corrections` — was rendered by the real `run_prompts._correction_context_body`, and the resumed worker echoed back the exact `--attempt` token and `--redirect-id` it was given, which `redirect_ack.acknowledge_correction` then accepted. |
| `unavailable-history-requires-user` | The real binary refused a conversation it has never seen, and blamed the conversation for the refusal. A setup error that stops the same turn is reported as a failure, not a pass. |

The report records the exact versions it exercised under `verified_versions`, and
a version only appears there when a scenario actually passed against it —
installed-and-probed is inventory, not evidence. The document carries its own
disclaimer saying so, because the pinned window in `harness_compatibility.py` is
a support *policy* and this report is the *evidence*, and conflating them would
let a release claim verification it never earned.

Only the harness half of `unavailable-history-requires-user` is observable live.
What ASL does about missing history is decided by the run loop, not the harness:
`conversation-recovery-required` classifies as `requires_user` with
`user_action: inspect` and zero retries, which is pinned deterministically
alongside the opt-in tests.
