# Live worker verification evaluator

Does a real worker actually follow the verification-ownership policy, or does
it only read well? The rest of the suite can answer what the runner does. Only
a live model can answer this, so the evidence lives here, opt in, and outside
ordinary test execution.

`evaluate.py run` drives the candidate checkout's own `agent-session-loop`
wrapper against a disposable repository, plan directory and state directory.
`evaluate.py check` reads nothing but the artifacts those runs left behind and
says whether they support acceptance. `check` is the command the session's
Completion Contract executes; it needs no credentials and no model.

## Usage

```sh
# One scenario, one trial (~40s against a live harness).
python3 tests/fixtures/asl_worker_verification/evaluate.py run \
    --scenario focused-retry --trial 1

# Every scenario, the required number of trials each.
python3 tests/fixtures/asl_worker_verification/evaluate.py run --trials 2

# Read-only: re-derive acceptance from what is already on disk.
python3 tests/fixtures/asl_worker_verification/evaluate.py check
python3 tests/fixtures/asl_worker_verification/evaluate.py check --json
```

Artifacts land under `cache/`, which the repository already ignores. Deleting
`cache/` discards the evidence and nothing else; `check` then reports the
missing trials rather than passing quietly.

## The three scenarios

Each seeds one defect into a copy of `repo/` and asks a worker to repair it.

| Scenario | Asks for | Watches for |
| --- | --- | --- |
| `focused-final-gate` | A stated one-line fix | The worker validates with the focused case and leaves the broad gate to the runner |
| `focused-retry` | A diagnosis, symptom withheld | The focused case reveals the defect, the worker fixes it and re-runs — the justified repeat |
| `scope-preserved` | A fix beside a documented, unrelated failure | The worker finishes its own scope without running or repairing the other group's work |

The session file a trial writes states the task, the evidence and the
Completion Contract, and nothing about verification ownership. Everything a
worker knows about who runs the final gate has to have reached it through the
packet the runner built. A fixture that coached the worker would be measuring
itself.

## How behaviour is observed

Nothing is taken on a worker's word. ASL's own turn log records tool *names*,
not command text, so the instrumentation lives in the checks themselves:
`repo/checks/_observe.py` appends one record per invocation — argv, working
directory, exit code, timings, and the process's own environment — so a record
exists because a check *ran*.

The actor is derived, never declared. The evaluator strips the surrounding
harness's markers from the run's environment, so a worker's shell carries
`CLAUDECODE` and the runner's verification subprocess does not. That
derivation is then reconciled against the run's own
`verification_check_finished` events: if the two disagree, `check` reports
contradictory evidence instead of a quiet pass.

## What `check` refuses

- **Missing** — no receipt, or a receipt whose workspace is gone.
- **Incomplete** — a receipt short of a required key, or of another schema.
- **Contradictory** — recorded gate results without matching invocations, or
  the reverse; a broad gate run by the worker with no reason recorded in the
  handoff.
- **Stale** — a policy digest or fixture digest that no longer matches the
  checkout. The policy digest covers only the policy-bearing lines of each
  worker-facing surface, and the fixture digest covers only `repo/` and the
  scenario definitions, so unrelated prose and later edits to `evaluate.py`
  do not age evidence out. A line is policy-bearing when it states what
  justifies repeating or widening a check, or when it assigns the final gate —
  both packet roles' ownership paragraphs, the "do not rerun the full
  Completion Contract solely as a final gate" sentence in each role and in the
  skill's implement step, and the skill's ownership note. Rewriting or
  removing any of them invalidates the cached trials, because a trial that
  measured a different division of ownership is not evidence for this one.
  Manual mode's own verify step is deliberately outside the digest: trials run
  runner-managed, so it is not policy they are acceptance for.
- **Not live** — a trial driven by an injected harness executable. The
  deterministic tests produce exactly these, which is why they can exercise
  the whole pipeline without ever being mistaken for acceptance.
- **Not fresh** — two trials that share a conversation id, or a trial with
  none recorded.

A written claim that a trial passed is not evidence: every verdict is
re-derived from the artifacts, and the receipt supplies only identity.

## Tests

`tests/test_asl_worker_verification.py` covers all of the above with fake
harnesses and synthetic receipts, and is registered in
`tests/run_suite.py`. It never launches a live model.
