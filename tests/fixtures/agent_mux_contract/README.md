# ASL/AgentMux compatibility contract

This directory is the canonical, ASL-owned description of what ASL and AgentMux
exchange. ASL is the sole run authority, so ASL owns the schemas, the fixtures
that pin them, and the identity a consumer checks before it trusts either.

Nothing here activates anything. It publishes the contract; the runtime
components adopt it separately.

## Layout

```
schemas/projection-v1.json        the agent_mux block ASL writes into state.json
schemas/lifecycle-hook-v2.json    the payload ASL pipes to `amx hook-indicator`
schemas/bridge-protocol-v2.json   the NDJSON frames `asl bridge serve` writes
fixtures/<component>/valid/*      documents the schema must accept
fixtures/<component>/invalid/*    documents the schema must reject, each naming
                                  the reason and pointer it must be rejected at
manifest.json                     derived; contract-set identity and capabilities
```

Every `.json` file is stored in one canonical rendering — sorted keys, two-space
indent, no ASCII escaping, trailing newline — which is why regenerating the
corpus is a no-op rather than a whitespace diff.

## Contract-set identity

The contract-set ID is a digest over the canonical bytes of every `.json` file
above, in sorted path order, truncated to 64 bits and prefixed `asl-amx-1.`. It
is derived, never declared: editing any schema or fixture byte moves it, and
nothing else can. `manifest.json` is excluded from the digest — it carries that
digest, so including it would make identity depend on itself. This README is
excluded too, so prose can be corrected without moving the contract.

Fixture documents carry `asl-amx-1.0000000000000000` as their contract-set ID.
A fixture cannot carry the real ID, because the ID is a function of the fixture
bytes. The value is a placeholder that satisfies the format and nothing more;
the authoritative ID lives in `manifest.json` and is reported by
`asl contract-check --agent-mux --json`.

## Rules the corpus enforces

- **Closed fields.** Objects declare `additionalProperties: false`. A field
  nobody declared is a rejection, not a value to ignore, so a producer cannot
  widen the wire without moving the contract-set ID.
- **Bounded values.** Every string carries an enum, a const, a format, or a
  `maxLength`; every array carries a `maxItems`; every document carries a byte
  ceiling. One corrupt plan cannot become an unbounded frame.
- **Explicit absence.** In the projection every field is always present, and
  absence is spelled `null`. A consumer never has to tell "missing" from "not
  applicable".
- **One timestamp spelling.** RFC 3339 with an explicit `+00:00` offset. `Z`,
  naive timestamps, and non-UTC offsets are rejected, so the same instant
  compares byte for byte across all three components.
- **Canonical paths.** Absolute POSIX, no `.` or `..` segment, no trailing or
  doubled slash, 1024 characters at most.
- **Body-free by construction.** Question and reply prose is refused by name at
  every depth, before any schema-guided walk runs, so a leak is reported as
  `body-not-allowed` wherever it hides. The one body-bearing frame —
  `question.get`'s response — declares its exemption as the JSON pointer
  `/result/body` in its own schema, and the linter reconciles declarations
  against exemptions in both directions: an undeclared exemption is a schema
  defect, and so is a stale one.

Bounded, ASL-authored diagnostics are not prose and are permitted: the lifecycle
`question_preview` (160 characters, whitespace-collapsed by the producer and
re-bounded here), `pause_message`, and the bridge `message` and `detail` fields.
The rule is not "no text" but "no undeclared text, and every declared text field
carries a ceiling".

## Regenerating and checking

```sh
python3 -m agent_session_loop.agent_mux_contract --check   # lint, validate, verify manifest
python3 -m agent_session_loop.agent_mux_contract --write   # rebuild manifest.json
asl contract-check <plan-dir> --agent-mux --json           # the same report, in the CLI
```

Rebuilding the manifest is not the whole edit. The contract-set ID is pinned as
`agent_session_loop.agent_mux_projection.CONTRACT_SET_ID` — recomputing it on
every state write would put this corpus on the durable-write path of every run,
and an installed ASL need not ship its test fixtures at all — so a corpus edit
that moves the identity must move that constant too. The suite fails until it
does, so a stale pin cannot ship.

`tests/test_asl_agent_mux_contract.py` runs the whole corpus: every schema lints
clean, every valid fixture is accepted, every invalid fixture is rejected for
the exact reason and pointer it declares, every file is byte-identical to its
canonical rendering, and the manifest matches the corpus it describes.
