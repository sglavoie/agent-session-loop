"""The deterministic message a recoverable pause's retained work is committed with.

The settlement that commits the work a *recoverable* pause deliberately
retained needs no harness and no generated message: the pause may be
``timeout`` or ``transport-failed``, which is to say the harness itself may be
what failed. This module owns the text that marks that work as unverified and
the two places the text is written so ``asl commit --message-file`` can be
pointed at it.
"""

from __future__ import annotations

from pathlib import Path

from . import commit_message, durability, run_selection

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
    the harness failing (see :class:`run_commit._CommitGeneration`), and
    because a marking that says the work is *unverified* must read the same
    every time.

    ``session_label`` is :func:`plans_payloads.session_label`'s rendering — ``"session
    3"`` — so the subject and the sentence that opens with it agree on what
    the session is called. ``commit_mode`` is the run's mode, or ``None`` for a
    repair child that owns its own commit; only ``--commit-at-end`` changes
    what the message claims, because that commit defers until after the last
    session and — on the legacy path — stages with the repository-wide ``git
    add -A`` the commit argv builder falls back to. It therefore genuinely
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
    :func:`run_commit._write_commit_artifacts` leaves beside it. So it is
    written here, where the message itself is rendered.

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
