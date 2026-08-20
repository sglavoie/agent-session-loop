# Bridge protocol 1 wire fixtures

Canonical NDJSON frames for `asl bridge serve --stdio --protocol 1`. This
directory is the source of truth; Agent Mux carries a byte-identical copy at
`internal/aslbridge/testdata/protocol_v1/` and both repositories decode these
frames in their own language.

`manifest.json` pins the protocol vocabulary (capabilities, error codes,
operation statuses, Workbench focus keys, context kinds, frame ceiling, poll
interval) plus a SHA-256 per frame file and a combined `digest`. The combined
digest is `sha256` over `"<name>\n<sha256 hex>\n"` for every non-manifest
`*.json` file in ascending name order. Both repositories recompute it, so a
one-sided edit fails on the side that drifted.

When the protocol changes:

1. Edit the frames and `manifest.json` here.
2. Run `just test-fast` in this repository to confirm the fixtures still match
   what `agent_session_loop/bridge.py` produces.
3. Copy every `*.json` file into the Agent Mux testdata directory unchanged.
4. Run `just asl-integration` in Agent Mux against the installed bridge.

Frames are illustrative wire shapes, not derived records: `plan_dir`,
`row_id`, and `action_token` values are fixed fixture strings rather than
values recomputed from a live corpus, and the snapshot's `projection`
durations describe a corpus too large to walk inside the declared interval
rather than a measurement taken here.
