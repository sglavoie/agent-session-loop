"""How a run turns an accepted turn into a commit.

This is the *auto-commit* boundary: the nested ``asl commit`` a run re-enters
this checkout to spawn once a session's work is accepted, and every durable
trace that subprocess leaves behind. What a run *says it ran* — the invocation
payload and the per-role model projection — lives in
:mod:`agent_session_loop.run_commit_invocation`; the bounded subprocess itself
(spawn, process-group kill, descendant sweep) lives in
:mod:`agent_session_loop.run_commit_process`; the retained-work message lives
in :mod:`agent_session_loop.run_commit_paused`. Each is read at its owner.

Auto-commit has a second, narrower caller: the settlement that commits the
work a *recoverable* pause deliberately retained, so the relaunch meets a
clean tree instead of the startup commit gate. That path needs no harness and
no generated message, and two halves make it so — the ``message_file``
selection on :class:`_CommitGeneration`, owned here, which turns the nested
command into a verbatim commit, and
:func:`run_commit_paused._paused_work_commit_message`, the deterministic text
that marks the work as unverified.

The module also owns :class:`_LiveTurn`, the loop's live turn holder. It is
here rather than beside the turn boundary because the only thing it adds to
``turn_execution_live.LiveTurn`` is the run's own serialized persistence, and a
holder that binds a run's collaborators belongs with the loop's records rather
than with the boundary it specializes.

The durable records this boundary owns are the commit artifact directory and
its three files — ``stdout.log``, ``stderr.log``, and ``result.json`` — laid
out by :func:`_commit_paths` under ``commit-<index>-<suffix>/`` and written by
:func:`_write_commit_artifacts` through the atomic writers in
:mod:`agent_session_loop.durability`. Nothing else here opens a file: the
commit-started and commit-finished payloads are built and returned, and the
loop that called for the commit is what appends them to the event log and the
state record under the run's own lock. That split is deliberate — this module
runs a subprocess that can take minutes, and it must not be holding the run's
write mutex while it does.

Five invariants survive the move:

* **The commit re-enters this checkout, never PATH.**
  :data:`run_commit_process.CHECKOUT_ROOT` is resolved from its file at import
  and prepended to ``PYTHONPATH`` for the child, and the argv is
  ``sys.executable -m agent_session_loop``. A run therefore commits with the
  ``asl commit`` it ships with rather than with whatever another install put
  on PATH — and because the resolution is ``parent.parent``, that module has
  to sit directly under the package directory, which its test asserts rather
  than assumes.
* **A commit turn is always bounded.** ``--timeout`` is on the argv for every
  generation, including a default :class:`_CommitGeneration`, because an
  unbounded message harness holds the whole session loop behind it. The run's
  own ``--timeout`` is deliberately not forwarded: it bounds an implementation
  turn and is frequently unset.
* **The outer bound is a backstop the nested one cannot outlive.**
  :func:`run_commit_process._run_bounded_asl_commit` adds
  :data:`COMMIT_SUBPROCESS_GRACE_SECONDS`
  to the generation bound and enforces it with a process-group kill, a bounded
  drain, and a descendant sweep. Expiry is reported as exit 124 — the same code
  a timed-out harness turn uses — so it reads as a nonzero commit exit
  everywhere downstream and lands on the normal soft-failure path rather than
  raising.
* **Descendants are snapshotted before the kill, never after.** A descendant
  that called ``setsid`` never receives the group signal and is reparented to
  pid 1 once the group dies, after which no descendant walk can reach it. The
  snapshot taken while the child is still alive is the sweep's only route to
  such a process, and the child is reaped before the sweep so a pid the sweep
  might signal cannot be one the bounded runner is still holding.
* **Success is a claim about the repository, not about the exit code.**
  :func:`_commit_status_result` reads the head before and after and the working
  tree's cleanliness afterwards, so a zero exit that moved no head, or moved
  one and left the tree dirty, is a named failure rather than a success.

The module never imports the runner. Everything it needs is either a leaf
module it reads at the owner — ``clock``, the ``plans_*`` modules, ``durability``,
``process_tree`` — or a boundary that was extracted before it: the git shell in
``review_guard``, the harness selection in ``run_selection``, the workspace
projection in ``run_prompts``, and the run's write handle in
``run_persistence``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    clock,
    commit_groups,
    commit_message,
    durability,
    plans_files,
    plans_payloads,
    plans_session_ids,
    review_guard,
    run_commit_process,
    run_inputs,
    run_persistence,
    run_selection,
    turn_execution_live,
)
from .run_errors import RunError

COMMIT_SUBCOMMAND_ARGV: tuple[str, ...] = ("commit", "--yes", "--no-verify")
#: Grace the run allows a bounded ``asl commit`` beyond the ``--timeout`` it was
#: handed. The nested command is expected to enforce its own bound and exit with
#: a classified failure; this outer bound only covers the case where it cannot —
#: a wedged interpreter, or a harness descendant that escaped the nested kill and
#: still holds the captured pipes open. It is a backstop, so it is deliberately
#: generous enough that it never fires ahead of the bound it is backing.
COMMIT_SUBPROCESS_GRACE_SECONDS = 120


class _LiveTurn(turn_execution_live.LiveTurn):
    """The live turn as the run loop builds it.

    Binds the run's own serialized persistence, which the boundary requires
    and a caller outside the loop may not have. Every behavior is inherited.
    """

    def __init__(
        self,
        *,
        persistence: run_persistence._RunPersistence | None = None,
        **fields: Any,
    ) -> None:
        super().__init__(
            persistence=run_persistence._run_persistence(persistence), **fields
        )


def _commit_paths(
    run_dir: Path,
    *,
    commit_index: int,
    session_id: str | None,
    repository_alias: str | None = None,
) -> dict[str, Path]:
    if commit_index <= 0:
        raise RunError("commit_index must be a positive integer")
    if session_id is not None:
        session_id = plans_session_ids.parse_session_id(session_id).value
    suffix = (
        plans_payloads.session_path_label(session_id)
        if session_id is not None
        else "end"
    )
    if repository_alias is not None:
        suffix = f"{suffix}-{run_inputs._repository_path_label(repository_alias)}"
    artifact_dir = run_dir / f"commit-{commit_index:04d}-{suffix}"
    return {
        "artifact_dir": artifact_dir,
        "stdout": artifact_dir / "stdout.log",
        "stderr": artifact_dir / "stderr.log",
        "result": artifact_dir / "result.json",
    }


def _commit_status_result(
    *,
    returncode: int,
    head_before: str | None,
    head_after: str | None,
    clean_after: bool | None,
) -> str:
    if returncode != 0:
        return "nonzero-exit"
    if head_after is None:
        return "missing-head"
    if head_before == head_after:
        return "unchanged-head"
    if clean_after is None:
        return "status-unavailable"
    if not clean_after:
        return "dirty-after-commit"
    return "success"


def _write_commit_artifacts(
    *,
    paths: dict[str, Path],
    stdout: str,
    stderr: str,
    payload: dict[str, Any],
) -> None:
    durability.write_text(paths["stdout"], stdout)
    durability.write_text(paths["stderr"], stderr)
    durability.write_json_file(paths["result"], payload, durable=True)


@dataclass(frozen=True)
class _CommitGeneration:
    """Harness selection forwarded to one ``asl commit`` subprocess.

    The run's *declared* implementation flags travel here, never its reported
    model/effort payload: ``config.native_model_payload`` can answer
    ``"native CLI default"``, which names no model any harness would accept.

    ``timeout`` is the bound the nested command puts on its own generation
    turn. It is always set: an unbounded commit turn stalls the session loop
    behind it, which is exactly the failure the run cannot absorb.

    ``message_file`` takes the nested command out of generation entirely: it
    commits that file's contents verbatim and spawns no harness at all. The
    mode exists for the recovery paths — settling the work a *recoverable*
    pause retained — where spending another bounded harness turn is exactly
    wrong: the pause may be ``timeout`` or ``transport-failed``, which is to
    say the harness itself may be the thing that failed. It also makes the
    marking deterministic, since a model cannot reword a message it never
    wrote. It is ``None`` for every commit a run makes on its accepted path.
    """

    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout: int = commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS
    message_file: Path | None = None


def _commit_generation(args: argparse.Namespace) -> _CommitGeneration:
    """Resolve the commit-message harness selection for one run.

    The run's own ``--timeout`` is deliberately *not* forwarded: it bounds a
    session's implementation turn, is frequently unset, and would hand an
    unbounded commit turn to every run that leaves it out.
    """
    harness = getattr(args, "harness", None)
    if harness is None:
        return _CommitGeneration()
    return _CommitGeneration(
        harness=harness,
        model=run_selection._requested_model(args, harness=harness),
        effort=run_selection._requested_effort(args, harness=harness),
    )


def _asl_commit_argv(
    generation: _CommitGeneration,
    staging_paths: Sequence[str] = (),
) -> list[str]:
    """Build the in-tree commit argv for one repository target.

    Re-entering this interpreter with ``-m agent_session_loop`` removes the
    PATH lookup entirely, so a run commits with the ``asl commit`` its own
    checkout ships rather than with whatever another install put on PATH.
    ``staging_paths`` arrive qualified as ``"<repository>:<path>"`` and only
    the path half means anything to git; an empty list leaves ``asl commit``
    on its ``git add -A`` default, which is what a legacy single-repository
    target wants.

    ``--timeout`` is always on the argv, including for a default
    ``_CommitGeneration``: the bound is what keeps a stalled message harness
    from holding the whole session loop, so it must not depend on the run
    having named a harness.

    ``--message-file`` appears only when the generation carries one, so a
    default ``_CommitGeneration`` still asks for a generated message. It
    composes with everything else this builder emits: ``asl commit`` refuses
    it only alongside ``--from-id`` and ``--prepare-only``, neither of which
    a run ever passes, and the message-file path stages through the same
    ``--pathspec`` entries as the generated one.
    """
    argv = [
        sys.executable,
        "-m",
        "agent_session_loop",
        *COMMIT_SUBCOMMAND_ARGV,
        "--timeout",
        str(generation.timeout),
    ]
    if generation.harness is not None:
        argv.extend(["--harness", generation.harness])
    if generation.model is not None:
        argv.extend(["--model", generation.model])
    if generation.effort is not None:
        argv.extend(["--effort", generation.effort])
    if generation.message_file is not None:
        argv.extend(["--message-file", str(generation.message_file)])
    for path in commit_message.dequalify_paths(list(staging_paths)):
        argv.extend(["--pathspec", path])
    return argv


def _run_asl_commit(
    *,
    cwd: Path,
    run_dir: Path,
    commit_index: int,
    session_id: str | None,
    group_id: str | None = None,
    repository_key: str = commit_groups.LEGACY_REPOSITORY_KEY,
    repository_alias: str | None = None,
    repository_path: str = ".",
    qualified_paths: Sequence[str] = (),
    staging_paths: Sequence[str] = (),
    generation: _CommitGeneration | None = None,
) -> dict[str, Any]:
    canonical_cwd = plans_files.canonicalize_path(cwd)
    paths = _commit_paths(
        run_dir,
        commit_index=commit_index,
        session_id=session_id,
        repository_alias=repository_alias,
    )
    resolved_generation = generation or _CommitGeneration()
    argv = _asl_commit_argv(resolved_generation, staging_paths)
    subprocess_timeout = resolved_generation.timeout + COMMIT_SUBPROCESS_GRACE_SECONDS
    head_before_payload = review_guard._read_git_head(canonical_cwd)
    started_at = clock.utc_now()
    exec_error = None
    timed_out = False
    process_sweep: dict[str, Any] | None = None
    try:
        result, timed_out, process_sweep = run_commit_process._run_bounded_asl_commit(
            argv,
            cwd=canonical_cwd,
            timeout=subprocess_timeout,
        )
    except FileNotFoundError as error:
        exec_error = str(error)
        result = subprocess.CompletedProcess(
            argv,
            127,
            stdout="",
            stderr=exec_error,
        )
    except OSError as error:
        exec_error = str(error)
        result = subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=exec_error,
        )
    finished_at = clock.utc_now()
    head_after_payload = review_guard._read_git_head(canonical_cwd)
    head_before = head_before_payload["head"]
    head_after = head_after_payload["head"]
    try:
        clean_after: bool | None = review_guard._git_status_is_clean(canonical_cwd)
    except RunError as error:
        clean_after = None
        status_error: str | None = str(error)
    else:
        status_error = None
    status_result = _commit_status_result(
        returncode=result.returncode,
        head_before=head_before,
        head_after=head_after,
        clean_after=clean_after,
    )
    session_identity = (
        plans_payloads.session_identity_payload(session_id)
        if session_id is not None
        else {"session": None, "session_id": None}
    )
    payload = {
        "commit_index": commit_index,
        "group_id": group_id,
        "repository_key": repository_key,
        "repo": repository_alias,
        "repository_alias": repository_alias,
        "repository_path": repository_path,
        "qualified_paths": list(qualified_paths),
        "staging_paths": list(staging_paths),
        "scope": "session" if session_id is not None else "end",
        **session_identity,
        "artifact_dir": str(paths["artifact_dir"]),
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "result_path": str(paths["result"]),
        "argv": argv,
        "cwd": str(canonical_cwd),
        "returncode": result.returncode,
        "success": status_result == "success",
        "status_result": status_result,
        "head_before": head_before,
        "head_after": head_after,
        "head_before_result": head_before_payload,
        "head_after_result": head_after_payload,
        "clean_after": clean_after,
        "commit_sha": head_after if status_result == "success" else None,
        "started_at": clock.isoformat(started_at),
        "finished_at": clock.isoformat(finished_at),
        "duration_seconds": clock.duration_seconds(started_at, finished_at),
        "stdout_chars": len(result.stdout),
        "stderr_chars": len(result.stderr),
        "generation_timeout_seconds": resolved_generation.timeout,
        "subprocess_timeout_seconds": subprocess_timeout,
        "timed_out": timed_out,
    }
    if process_sweep is not None:
        payload["process_sweep"] = process_sweep
    if exec_error is not None:
        payload["exec_error"] = exec_error
    if status_error is not None:
        payload["status_error"] = status_error
    _write_commit_artifacts(
        paths=paths,
        stdout=result.stdout,
        stderr=result.stderr,
        payload=payload,
    )
    return payload


def _commit_started_payload(
    *,
    run_id: str,
    cwd: Path,
    run_dir: Path,
    commit_index: int,
    session_id: str | None,
    group_id: str | None = None,
    repository_key: str = commit_groups.LEGACY_REPOSITORY_KEY,
    repository_alias: str | None = None,
    repository_path: str = ".",
    qualified_paths: Sequence[str] = (),
    staging_paths: Sequence[str] = (),
    generation: _CommitGeneration | None = None,
) -> dict[str, Any]:
    paths = _commit_paths(
        run_dir,
        commit_index=commit_index,
        session_id=session_id,
        repository_alias=repository_alias,
    )
    session_identity = (
        plans_payloads.session_identity_payload(session_id)
        if session_id is not None
        else {"session": None, "session_id": None}
    )
    return {
        "run_id": run_id,
        "commit_index": commit_index,
        "group_id": group_id,
        "repository_key": repository_key,
        "repo": repository_alias,
        "repository_alias": repository_alias,
        "repository_path": repository_path,
        "qualified_paths": list(qualified_paths),
        "staging_paths": list(staging_paths),
        "scope": "session" if session_id is not None else "end",
        **session_identity,
        "artifact_dir": str(paths["artifact_dir"]),
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "result_path": str(paths["result"]),
        "argv": _asl_commit_argv(generation or _CommitGeneration(), staging_paths),
        "cwd": str(cwd),
    }
