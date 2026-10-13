"""Replay the recorded audit corpus against this working tree's checks.

Almost every deterministic check in :mod:`agent_session_loop.supervisor_audit_checks`
is a pure function of one recorded evidence bundle, so each
``supervisor-audit-input.json`` a run ever wrote is a reusable test case. The
exceptions are declared, not assumed: a check whose ``CheckSpec`` sets
``replayable=False`` reads something outside the bundle and is left out of the
matrix entirely, because a cell that can flip between two sweeps of the same
bytes is not evidence. Sweeping the corpus then answers two questions the
assurance ledger cannot:

* **Is a recorded finding still live?** An issue whose occurrences all replay
  ``pass`` at HEAD was fixed by some later commit, and can be closed on that
  evidence rather than on a fresh sighting that may never arrive.
* **Did a fix move only what it meant to move?** A matrix captured before an
  edit and diffed after it names every cell that changed, so a fix that
  silently flips an unrelated invariant on some unrelated run cannot hide.

Replay reproduces the deterministic lane only. A finding the LLM auditor lane
raised recorded ``pass`` on every deterministic key, so replaying it proves
nothing about that finding either way.

The module imports the package in process and never shells out to the ``asl``
wrapper: that wrapper derives its repository root from its own install
location, so it would report the pinned checkout's behavior no matter which
tree the caller is standing in.

A matrix is keyed by run ID and so can never become a checked-in fixture — the
sanitization gate in ``tests/test_asl_assurance_baseline.py`` rejects run
identifiers outright. Capture it as plan-directory evidence instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, supervisor_audit_checks
from .assurance_models_vocab import AUDIT_INPUT_FILENAME

#: Stored matrices carry a version so a baseline written by an incompatible
#: harness is rejected instead of silently compared against.
MATRIX_SCHEMA_VERSION = 1

#: Recorded bundles live at ``<state root>/<plan>/runs/<run id>/<bundle>``.
BUNDLE_GLOB = f"*/runs/*/{AUDIT_INPUT_FILENAME}"


class BundleUnreadable(Exception):
    """Raised when a recorded bundle cannot be read as JSON."""


@dataclass(frozen=True)
class CorpusReplay:
    """Every recorded bundle judged by the checks in this working tree."""

    invariant_keys: tuple[str, ...]
    matrix: Mapping[str, Mapping[str, str]]
    errors: Mapping[str, str]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": MATRIX_SCHEMA_VERSION,
            "invariant_keys": list(self.invariant_keys),
            "matrix": {run_id: dict(row) for run_id, row in self.matrix.items()},
            "errors": dict(self.errors),
        }


@dataclass(frozen=True)
class StatusChange:
    """One cell of the matrix that reads differently than it used to."""

    run_id: str
    invariant_key: str
    before: str
    after: str


@dataclass(frozen=True)
class MatrixDiff:
    changes: tuple[StatusChange, ...]
    added_runs: tuple[str, ...]
    removed_runs: tuple[str, ...]
    added_keys: tuple[str, ...]
    removed_keys: tuple[str, ...]
    errors: Mapping[str, str]
    unreadable_runs: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        """Whether the checks themselves now judge the corpus differently.

        Runs recorded since the baseline was captured are excluded: the state
        root keeps growing on its own, so corpus growth would otherwise report
        drift on every comparison and make the verdict worthless.
        """
        return bool(self.changes or self.added_keys or self.removed_keys)

    @property
    def incomplete(self) -> bool:
        """Whether a run the baseline judged could not be judged again.

        Distinct from :attr:`has_changes`: nothing moved, because nothing was
        compared. A baseline run that no longer reads is lost evidence, not a
        run retention pruned away, and a clean verdict over what remains is
        not proof that the checks still judge it the same way.
        """
        return bool(self.unreadable_runs)

    def transitions(self) -> dict[str, dict[str, int]]:
        """Count ``before->after`` moves per invariant key."""
        counts: dict[str, dict[str, int]] = {}
        for change in self.changes:
            per_key = counts.setdefault(change.invariant_key, {})
            label = f"{change.before}->{change.after}"
            per_key[label] = per_key.get(label, 0) + 1
        return counts

    def payload(self) -> dict[str, Any]:
        return {
            "has_changes": self.has_changes,
            "incomplete": self.incomplete,
            "errors": dict(self.errors),
            "unreadable_runs": list(self.unreadable_runs),
            "changes": [
                {
                    "run_id": change.run_id,
                    "invariant_key": change.invariant_key,
                    "before": change.before,
                    "after": change.after,
                }
                for change in self.changes
            ],
            "added_runs": list(self.added_runs),
            "removed_runs": list(self.removed_runs),
            "added_keys": list(self.added_keys),
            "removed_keys": list(self.removed_keys),
            "transitions": self.transitions(),
        }


def bundle_paths(state_root: Path) -> list[Path]:
    """Return every recorded audit bundle under ``state_root``, ordered by run."""
    return sorted(state_root.glob(BUNDLE_GLOB), key=lambda path: path.parent.name)


def replayable_invariant_keys() -> tuple[str, ...]:
    """The invariant keys whose verdict is a function of the bundle alone.

    A check that also reads the live filesystem answers about the tree the
    replay is standing in, so its cell can flip between two sweeps that judged
    the same bytes with the same code. Left in, it would let a matrix diff
    report a transition no commit caused, which is exactly the claim a
    disposition note makes when it closes an issue on replay evidence.
    """
    return tuple(
        spec.invariant_key
        for spec in supervisor_audit_checks.DETERMINISTIC_CHECK_SPECS
        if spec.replayable
    )


def replay_bundle(path: Path) -> dict[str, str]:
    """Judge one recorded bundle with this working tree's reproducible checks."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleUnreadable(f"{type(exc).__name__}: {exc}") from exc
    reproducible = set(replayable_invariant_keys())
    return {
        result.invariant_key: result.status
        for result in supervisor_audit_checks.run_deterministic_checks(payload)
        if result.invariant_key in reproducible
    }


def replay_corpus(state_root: Path) -> CorpusReplay:
    """Replay every recorded bundle under ``state_root`` into a status matrix."""
    matrix: dict[str, dict[str, str]] = {}
    errors: dict[str, str] = {}
    for path in bundle_paths(state_root):
        run_id = path.parent.name
        try:
            matrix[run_id] = replay_bundle(path)
        except BundleUnreadable as exc:
            # A live state root is swept while runs are still writing into it,
            # so one half-written bundle must not abandon the whole sweep.
            errors[run_id] = str(exc)
    return CorpusReplay(
        invariant_keys=replayable_invariant_keys(),
        matrix=matrix,
        errors=errors,
    )


def load_matrix(path: Path) -> CorpusReplay:
    """Read a matrix previously written by :func:`replay_corpus`."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version")
    if version != MATRIX_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: stored matrix schema {version!r} is not {MATRIX_SCHEMA_VERSION}"
        )
    return CorpusReplay(
        invariant_keys=tuple(payload["invariant_keys"]),
        matrix={run_id: dict(row) for run_id, row in payload["matrix"].items()},
        errors=dict(payload["errors"]),
    )


def diff_matrices(before: CorpusReplay, after: CorpusReplay) -> MatrixDiff:
    """Name every status that changed between two replays of the same corpus."""
    after_keys = set(after.invariant_keys)
    before_keys = set(before.invariant_keys)
    shared_keys = [key for key in before.invariant_keys if key in after_keys]
    changes = []
    for run_id in sorted(set(before.matrix) & set(after.matrix)):
        old_row = before.matrix[run_id]
        new_row = after.matrix[run_id]
        changes.extend(
            StatusChange(run_id, key, old_row[key], new_row[key])
            for key in shared_keys
            if old_row[key] != new_row[key]
        )
    unreadable = set(before.matrix) & set(after.errors)
    return MatrixDiff(
        changes=tuple(changes),
        added_runs=tuple(sorted(set(after.matrix) - set(before.matrix))),
        removed_runs=tuple(sorted(set(before.matrix) - set(after.matrix) - unreadable)),
        added_keys=tuple(k for k in after.invariant_keys if k not in before_keys),
        removed_keys=tuple(k for k in before.invariant_keys if k not in after_keys),
        errors=dict(after.errors),
        unreadable_runs=tuple(sorted(unreadable)),
    )


def _emit_capture(replay: CorpusReplay, output: str | None, as_json: bool) -> int:
    document = json.dumps(replay.payload(), indent=2, sort_keys=True)
    if output is None:
        print(document)
        return 0
    Path(output).expanduser().write_text(document + "\n", encoding="utf-8")
    if as_json:
        print(
            json.dumps(
                {
                    "output": output,
                    "runs": len(replay.matrix),
                    "errors": len(replay.errors),
                },
                sort_keys=True,
            )
        )
    else:
        print(f"captured {len(replay.matrix)} runs to {output}")
        if replay.errors:
            print(f"{len(replay.errors)} bundle(s) could not be read")
    return 0


def _emit_diff(before: CorpusReplay, after: CorpusReplay, as_json: bool) -> int:
    diff = diff_matrices(before, after)
    if as_json:
        print(json.dumps(diff.payload(), indent=2, sort_keys=True))
    else:
        for change in diff.changes:
            print(
                f"{change.run_id}  {change.invariant_key}  "
                f"{change.before} -> {change.after}"
            )
        for key in diff.added_keys:
            print(f"new invariant key: {key}")
        for key in diff.removed_keys:
            print(f"dropped invariant key: {key}")
        for run_id in diff.unreadable_runs:
            print(f"unreadable baseline run: {run_id}  {diff.errors[run_id]}")
        print(
            f"{len(diff.changes)} cell(s) changed across "
            f"{len(before.matrix)} baseline runs; "
            f"{len(diff.added_runs)} run(s) recorded since capture"
        )
        for key, counts in sorted(diff.transitions().items()):
            rendered = ", ".join(f"{label} {n}" for label, n in sorted(counts.items()))
            print(f"  {key}: {rendered}")
        if diff.incomplete:
            print(
                f"comparison incomplete: {len(diff.unreadable_runs)} "
                "baseline run(s) could not be replayed"
            )
    return 1 if diff.has_changes or diff.incomplete else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay recorded audit bundles against this working tree.",
    )
    parser.add_argument(
        "--state-root",
        help="Operational state root to sweep (default: the configured root)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser(
        "capture", help="Replay the corpus and emit its status matrix"
    )
    capture.add_argument(
        "--output",
        help="Write the matrix here instead of printing it",
    )
    compare = commands.add_parser(
        "diff", help="Replay the corpus and compare it against a stored matrix"
    )
    compare.add_argument("baseline", help="Matrix previously written by `capture`")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Capture or diff the corpus matrix.

    ``diff`` exits 1 when a status moved and when a baseline run could no
    longer be replayed, because neither outcome is evidence of no drift.
    """
    args = build_parser().parse_args(argv)
    state_root = (
        Path(args.state_root).expanduser()
        if args.state_root
        else config.default_state_root()
    )
    replay = replay_corpus(state_root)
    if args.command == "capture":
        return _emit_capture(replay, args.output, args.json)
    return _emit_diff(load_matrix(Path(args.baseline).expanduser()), replay, args.json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
