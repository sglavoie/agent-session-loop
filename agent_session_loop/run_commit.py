"""How a run turns an accepted turn into a commit, and what it says it ran.

Two things a run publishes about work that has already happened live here. The
first is the *invocation payload*: the durable statement of what a turn was
launched as — its harness, its argv or protocol transport and turn plan, the
model and effort it asked for, and the workspace it was allowed to touch —
together with the per-role model projection that folds each turn's observation
into what the run can honestly claim it ran. The second is *auto-commit*: the
nested ``asl commit`` a run re-enters this checkout to spawn once a session's
work is accepted, and every durable trace that subprocess leaves behind.

Auto-commit has a second, narrower caller: the settlement that commits the
work a *recoverable* pause deliberately retained, so the relaunch meets a
clean tree instead of the startup commit gate. That path needs no harness and
no generated message, and this module owns both halves of what makes it so —
the ``message_file`` selection on :class:`_CommitGeneration`, which turns the
nested command into a verbatim commit, and :func:`_paused_work_commit_message`,
the deterministic text that marks the work as unverified.

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

* **The commit re-enters this checkout, never PATH.** :data:`CHECKOUT_ROOT` is
  resolved from this file at import and prepended to ``PYTHONPATH`` for the
  child, and the argv is ``sys.executable -m agent_session_loop``. A run
  therefore commits with the ``asl commit`` it ships with rather than with
  whatever another install put on PATH — and because the resolution is
  ``parent.parent``, this module has to sit directly under the package
  directory, which its test asserts rather than assumes.
* **A commit turn is always bounded.** ``--timeout`` is on the argv for every
  generation, including a default :class:`_CommitGeneration`, because an
  unbounded message harness holds the whole session loop behind it. The run's
  own ``--timeout`` is deliberately not forwarded: it bounds an implementation
  turn and is frequently unset.
* **The outer bound is a backstop the nested one cannot outlive.**
  :func:`_run_bounded_asl_commit` adds :data:`COMMIT_SUBPROCESS_GRACE_SECONDS`
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
  might signal cannot be one this module is still holding.
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
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    clock,
    commit_groups,
    commit_message,
    config,
    durability,
    harnesses,
    plans_files,
    plans_payloads,
    plans_session_ids,
    process_tree,
    review_guard,
    run_inputs,
    run_persistence,
    run_prompts,
    run_selection,
    turn_execution_live,
    verification,
)
from .run_errors import RunError

#: The checkout this module was imported from. Auto-commit re-enters that same
#: checkout as a subprocess rather than resolving a name on PATH, so a run
#: always commits with the ``asl commit`` it ships with.
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
COMMIT_SUBCOMMAND_ARGV: tuple[str, ...] = ("commit", "--yes", "--no-verify")
#: Grace the run allows a bounded ``asl commit`` beyond the ``--timeout`` it was
#: handed. The nested command is expected to enforce its own bound and exit with
#: a classified failure; this outer bound only covers the case where it cannot —
#: a wedged interpreter, or a harness descendant that escaped the nested kill and
#: still holds the captured pipes open. It is a backstop, so it is deliberately
#: generous enough that it never fires ahead of the bound it is backing.
COMMIT_SUBPROCESS_GRACE_SECONDS = 120
#: How long a killed commit subprocess is given to flush what it had already
#: written. A descendant that left the process group survives the kill and
#: holds the pipes open, so this drain is bounded too; past it the streams are
#: abandoned rather than waited on.
_COMMIT_DRAIN_SECONDS = 5


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


def _build_invocation_payload(
    invocation: harnesses.HarnessInvocation,
    *,
    model_effort: dict[str, str | None] | None = None,
    verification_context: verification.VerificationContext | None = None,
) -> dict[str, Any]:
    if model_effort is None:
        model_effort = config.model_effort_payload(
            invocation.name,
            model=invocation.model,
            effort=invocation.effort,
        )
    workspace_fields = (
        {}
        if verification_context is None
        else run_prompts._workspace_payload_fields(verification_context)
    )
    return {
        "name": invocation.name,
        "argv": invocation.argv,
        # Under a protocol transport ``argv`` names only the binary, so it is
        # no longer a full account of the turn on its own. ``transport`` says
        # which of the two this is, and ``turn_plan`` carries what argv used to:
        # the access profile, the writable roots, the model, and the effort.
        "transport": invocation.transport,
        "turn_plan": (
            invocation.turn_plan.to_payload()
            if invocation.turn_plan is not None
            else None
        ),
        "cwd": str(invocation.cwd),
        "last_message_path": (
            str(invocation.last_message_path)
            if invocation.last_message_path is not None
            else None
        ),
        **model_effort,
        **workspace_fields,
    }


def _resolved_model_record(record: dict[str, Any]) -> dict[str, Any]:
    """Decide one role's model from what it requested and what it observed."""

    observed = record["observed_models"]
    # A requested model is the run's own answer; an observation cannot outrank
    # it. Only an unresolved request is open for an observation to settle, and
    # only when every turn of the role agreed on the same model — disagreement
    # means the role has no single answer, not that the last one wins.
    resolved = (
        record["requested_model_source"] == config.MODEL_SOURCE_UNRESOLVED
        and len(observed) == 1
    )
    return {
        **record,
        "model": observed[0] if resolved else record["requested_model"],
        "model_source": (
            config.MODEL_SOURCE_HARNESS_REPORTED
            if resolved
            else record["requested_model_source"]
        ),
    }


def _merged_model_projection(
    projection: Any, turn: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Fold one turn into the run's durable per-role model projection.

    `model` records what the run asked for, which stays true of the request
    forever and says nothing about what ran; a run that requested nothing keeps
    the harness default under unresolved provenance. This projection is where
    the observation is allowed to settle that question.

    Roles are kept apart by session kind and harness because a review harness
    need not be the implementation harness, and one projection over both would
    attribute one harness's model to the other. The projection accumulates
    rather than being recomputed from the turn list, so trimming old turns
    cannot erase what the run already observed.
    """

    records: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(projection, list):
        for entry in projection:
            if not isinstance(entry, Mapping):
                continue
            session_kind = entry.get("session_kind")
            harness = entry.get("harness")
            if not isinstance(session_kind, str) or not isinstance(harness, str):
                continue
            observed = entry.get("observed_models")
            records[(session_kind, harness)] = {
                "session_kind": session_kind,
                "harness": harness,
                "requested_model": entry.get("requested_model"),
                "requested_model_source": entry.get("requested_model_source"),
                "observed_models": (
                    [item for item in observed if isinstance(item, str)]
                    if isinstance(observed, list)
                    else []
                ),
                "turns": turns if isinstance(turns := entry.get("turns"), int) else 0,
            }

    session_kind = turn.get("session_kind")
    harness = turn.get("harness")
    if isinstance(session_kind, str) and isinstance(harness, str):
        record = records.setdefault(
            (session_kind, harness),
            {
                "session_kind": session_kind,
                "harness": harness,
                "requested_model": turn.get("model"),
                "requested_model_source": turn.get("model_source"),
                "observed_models": [],
                "turns": 0,
            },
        )
        record["turns"] += 1
        observed_model = turn.get("observed_model")
        if (
            isinstance(observed_model, str)
            and observed_model
            and observed_model not in record["observed_models"]
        ):
            record["observed_models"] = sorted(
                [*record["observed_models"], observed_model]
            )

    return [_resolved_model_record(record) for record in records.values()]


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
    durability.write_json_file(paths["result"], payload)


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


#: The one recoverable pause reason that carries a repair budget. The other
#: three — ``transport-failed``, ``timeout`` and ``no-status-change`` — send
#: the same session back unchanged, so a retained-work commit made for them has
#: no attempt to number and says nothing about one.
_REPAIR_BUDGET_PAUSE_REASON = "verify-failed"


def _paused_work_commit_message(
    *,
    session_label: str,
    pause_reason: str,
    commit_mode: str | None,
    repair_attempt: int | None,
    repair_max_attempts: int | None,
    run_id: str,
) -> str:
    """Render the message a recoverable pause's retained work is committed with.

    This is the message the settlement hands to ``asl commit --message-file``.
    It is built here rather than generated because the pause it marks may be
    the harness failing (see :class:`_CommitGeneration`), and because a marking
    that says the work is *unverified* must read the same every time.

    ``session_label`` is :func:`plans_payloads.session_label`'s rendering — ``"session
    3"`` — so the subject and the sentence that opens with it agree on what
    the session is called. ``commit_mode`` is the run's mode, or ``None`` for a
    repair child that owns its own commit; only ``--commit-at-end`` changes
    what the message claims, because that commit defers until after the last
    session and — on the legacy path — stages with the repository-wide ``git
    add -A`` this module's argv builder falls back to. It therefore genuinely
    spans work earlier sessions left behind, and must not be described as this
    turn's alone.

    The subject is bare-type Conventional Commits (``chore:``, no scope) to
    match this repository's log, and satisfies
    :data:`commit_message.COMMIT_TITLE_PATTERN`; the whole message is returned
    already normalized by :func:`commit_message.format_commit_message`, so what
    is written to the message file is what git would keep.
    """
    at_end = commit_mode == run_selection.COMMIT_MODE_END
    if at_end:
        subject = f"chore: retain unverified work accumulated through {session_label}"
        retained = (
            f"{_sentence_case(session_label)} is back to `pending`; the at-end "
            "commit spans everything the run has not committed yet, so this "
            "records the retained work together with whatever earlier sessions "
            "left in the tree, and the next attempt starts from a clean worktree."
        )
    else:
        subject = f"chore: retain unverified work from {session_label}"
        retained = (
            f"{_sentence_case(session_label)} is back to `pending`; this commit "
            "records what the turn left behind so the next attempt starts from a "
            "clean worktree."
        )

    sentences = [
        f"The run paused with reason `{pause_reason}` before this work was verified.",
        retained,
    ]
    if (
        pause_reason == _REPAIR_BUDGET_PAUSE_REASON
        and repair_attempt is not None
        and repair_max_attempts is not None
    ):
        sentences.append(f"Repair attempt {repair_attempt} of {repair_max_attempts}.")
    sentences.append(f"Run: {run_id}.")

    return commit_message.format_commit_message(f"{subject}\n\n{' '.join(sentences)}")


def _sentence_case(text: str) -> str:
    """Capitalize a label's first character, leaving the rest of it alone."""
    return text[:1].upper() + text[1:]


#: The file name :func:`_write_paused_work_message` writes under the paused
#: turn's artifact directory. The message is an artifact of the run, never of
#: the workspace: written into the tree it would become part of the very dirt
#: the commit it describes is settling, and ``--pathspec`` staging would not
#: reliably exclude it.
PAUSED_WORK_MESSAGE_FILE_NAME = "paused-work-commit-message.txt"


def _write_paused_work_message(artifact_dir: Path, message: str) -> Path:
    """Write ``message`` where ``asl commit --message-file`` can be pointed at it.

    The commit ledger resolves the targets and decides that a pause owes a
    commit, but it opens no file of its own — every durable record it owns goes
    through the run's persistence handle. The message file is neither a state
    write nor an event: it is the nested command's input, on the same footing as
    the ``stdout.log``/``stderr.log``/``result.json`` triple
    :func:`_write_commit_artifacts` leaves beside it. So it is written here,
    where the ``--message-file`` argv is built and the message itself is
    rendered.

    It is deliberately left behind afterwards. The commit it produced says the
    work is unverified; the file is the verbatim evidence of what was claimed,
    next to the artifacts of the commit that claimed it.
    """
    path = artifact_dir / PAUSED_WORK_MESSAGE_FILE_NAME
    durability.write_text(path, message)
    return path


#: Where a run rewrites the message file for a ``session-paused`` commit group
#: another run left owed. The original file lives under the *paused* turn's
#: artifact directory, which belongs to a run this process may never have seen,
#: so recovery writes its own copy rather than reaching into another run's
#: directory for a path that may no longer exist.
RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME = "commit-group-recovery"


def _write_recovered_paused_work_message(
    run_dir: Path, *, group_id: str, message: str
) -> Path:
    """Rewrite a recovered paused-work group's message under this run's dir.

    ``message`` is the text the group itself carries, so what recovery commits
    is the same marking the first attempt would have committed rather than a
    fresh rendering of it — this only decides *where* that text lives for the
    ``--message-file`` argv. The group id names the directory because a run may
    recover more than one group and each carries its own message, and because a
    path derived from the group's identity is one a reader can match back to the
    failure record that named it.
    """
    return _write_paused_work_message(
        run_dir / RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME / group_id, message
    )


def _asl_commit_env() -> dict[str, str]:
    """Make this checkout importable to the commit subprocess."""
    env = dict(os.environ)
    root = str(CHECKOUT_ROOT)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    return env


def _snapshot_commit_descendants(root_pid: int) -> tuple[Mapping[str, Any], ...]:
    """Record ``root_pid``'s descendants while the commit subprocess is alive.

    Killing the group reparents a descendant that started a session of its own
    to pid 1, after which no descendant walk can reach it. The snapshot is the
    sweep's only route to such a process, so it must be taken before the kill
    (mirrors :func:`harnesses._snapshot_descendants`).

    It names only what was running at this instant. A child forked after it is
    covered instead by :func:`process_tree.sweep_survivors` walking the live
    ancestry of everything this snapshot did name.
    """
    if not process_tree.sweep_enabled():
        return ()
    table = process_tree.sample_process_table()
    if table is None:
        return ()
    return tuple(table[pid] for pid in process_tree.descendant_pids(root_pid, table))


def _sweep_commit_descendants(
    process: subprocess.Popen,
    prior_snapshot: tuple[Mapping[str, Any], ...],
) -> dict[str, Any] | None:
    """Kill whatever outlived the bounded commit, or ``None`` when off.

    ``expected_pgid`` is the child's PID because the commit spawns with
    ``start_new_session=True``. The caller reaps the child before this runs, so
    the sweep excludes it rather than signalling a PID that may be reused; what
    it reaches is the descendant the process-group kill could not — one that
    called ``setsid`` and therefore never received the group signal, together
    with anything that descendant has forked since ``prior_snapshot`` was
    taken.
    """
    if not process_tree.sweep_enabled():
        return None
    return process_tree.sweep_survivors(
        root_pid=process.pid,
        expected_pgid=process.pid,
        prior_snapshot=prior_snapshot,
    )


def _run_bounded_asl_commit(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
) -> tuple[subprocess.CompletedProcess[str], bool, dict[str, Any] | None]:
    """Run one ``asl commit`` under a wall-clock bound it cannot outlive.

    The nested command already bounds its own harness turn and exits with a
    classified failure, so this bound normally never fires. What it covers is
    the case the nested bound cannot reach: a wedged interpreter, or a harness
    descendant that escaped the nested kill and still holds the captured pipes
    open — the exact shape that turns one stalled commit into a stalled run.

    ``start_new_session=True`` makes the child's process group ID its own PID,
    so the expiry can kill the whole tree at once (mirrors
    :func:`harnesses._kill_process_group`). The group kill alone is not enough:
    the harness the nested command starts runs in a session of its own, so its
    tree never receives that signal. Expiry therefore snapshots the descendants
    before killing and sweeps the survivors after, the same two-step
    :meth:`HarnessAdapter.run` uses for a timed-out turn.

    A second, short bound covers the drain between the two: a descendant that
    left the group holds the captured pipes open, and waiting on it forever
    would reintroduce the stall the kill just ended. The child is reaped before
    the sweep so a pid the sweep might signal cannot be one this function is
    still holding. Expiry is reported as exit 124 — the same code
    ``run_harness`` uses for a timed-out turn — so it reads as a nonzero commit
    exit everywhere downstream and lands on the normal soft-failure path.
    """
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=_asl_commit_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        prior_snapshot = _snapshot_commit_descendants(process.pid)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            # The group is already empty, or the child left it for one of its
            # own; the direct child is still reachable either way.
            with contextlib.suppress(OSError):
                process.kill()
        try:
            stdout, stderr = process.communicate(timeout=_COMMIT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            # The streams are held open by something that outlived the kill;
            # abandon them, but still reap the signalled child so the sweep
            # below sees its pid free rather than treating the group as a
            # stranger's.
            stdout, stderr = "", ""
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_COMMIT_DRAIN_SECONDS)
        sweep = _sweep_commit_descendants(process, prior_snapshot)
        stderr = (
            f"{stderr}\nasl commit exceeded its {timeout}s bound and was "
            "terminated with its process group"
        ).lstrip()
        return subprocess.CompletedProcess(list(argv), 124, stdout, stderr), True, sweep
    return (
        subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr),
        False,
        None,
    )


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
        result, timed_out, process_sweep = _run_bounded_asl_commit(
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
        status_error = str(error)
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
