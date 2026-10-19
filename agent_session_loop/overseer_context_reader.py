"""Overseer context reader."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import (
    events,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    state,
)
from . import (
    overseer_context_models as models,
)
from . import (
    overseer_context_projections as projections,
)

# -- assembly --------------------------------------------------------------


#: What an absent family means, per family. Written out rather than generated
#: because "the run has not committed yet" and "the run has no turns yet" are
#: different things for an operator to read.
_ABSENT_DETAIL: dict[models.SourceKind, str] = {
    "status": "the run has published no state record",
    "questions": "the plan has no mailbox",
    "run_config": "the run has published no state record",
    "verification": "no turn of this run has recorded a verification",
    "worker_changes": "this run has recorded neither a commit nor a turn",
    "supervision": "the plan has no mailbox",
}


class _Reader:
    """One plan's records, read at most once each and never written.

    Every path it opens is derived from the plan's own state directory and a
    locator the ledger stored, and :meth:`_resolve` refuses a locator that
    escapes it -- a row is data, and data that names ``../../`` somewhere else
    is not a reference this boundary follows.
    """

    def __init__(
        self,
        *,
        plan_dir: str,
        run_id: str,
        state_dir: str | Path | None,
        read_mailbox: models.ReadMailbox,
    ) -> None:
        self.plan_dir = plan_dir
        self.run_id = run_id
        self.state_dir = state_dir
        self.plan_state = state.plan_state_dir(plan_dir, state_dir)
        self._read_mailbox = read_mailbox
        self._records: dict[str, dict[str, Any] | None] = {}
        self._view: mailbox_snapshot.MailboxView | None = None
        self._view_read = False

    # -- locating ----------------------------------------------------------

    def locate(self, kind: models.SourceKind) -> str | None:
        """Where this run currently keeps one family, relative to its state dir."""
        if kind in ("status", "run_config"):
            path = self.plan_state / events.STATE_FILE_NAME
            return events.STATE_FILE_NAME if path.is_file() else None
        if kind in ("questions", "supervision"):
            path = self.plan_state / mailbox_constants.MAILBOX_FILE_NAME
            return mailbox_constants.MAILBOX_FILE_NAME if path.is_file() else None
        if kind == "verification":
            return self._latest_turn(requiring="verification")
        return self._latest_commit() or self._latest_turn(requiring="status_changes")

    def _latest_turn(self, *, requiring: str) -> str | None:
        """The newest turn artifact carrying ``requiring``, newest first.

        Newest first rather than newest only: a turn that paused before it
        verified writes no verification block, and reporting "no verification"
        for a run that verified three turns ago would describe the artifact
        layout rather than the run.
        """
        for directory in reversed(self._artifacts(models.TURN_PREFIX)):
            locator = self._relative(directory / state.RESULT_FILE_NAME)
            record = self.record(locator)
            if record is not None and record.get(requiring):
                return locator
        return None

    def _latest_commit(self) -> str | None:
        """The newest commit artifact of this run, if it has committed."""
        for directory in reversed(self._artifacts(models.COMMIT_PREFIX)):
            locator = self._relative(directory / state.RESULT_FILE_NAME)
            if self.record(locator) is not None:
                return locator
        return None

    def _artifacts(self, prefix: str) -> list[Path]:
        """This run's artifact directories under one prefix, oldest first.

        Sorted by name, which orders them by the zero-padded index the runner
        writes, so "newest" needs no timestamps and no ``stat`` call.
        """
        run_dir = self.plan_state / state.RUNS_DIR_NAME / self.run_id
        try:
            entries = [
                entry
                for entry in run_dir.iterdir()
                if entry.name.startswith(prefix) and entry.is_dir()
            ]
        except OSError:
            return []
        return sorted(entries, key=lambda entry: entry.name)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.plan_state).as_posix()

    def _resolve(self, locator: str) -> Path | None:
        """The path a locator names, or ``None`` if it names somewhere else."""
        if not locator:
            return None
        candidate = Path(locator)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        return self.plan_state / candidate

    # -- reading -----------------------------------------------------------

    def fingerprint(self, locator: str) -> tuple[str | None, int]:
        """The digest and size of one source, or ``None`` when unreadable."""
        path = self._resolve(locator)
        if path is None:
            return None, 0
        try:
            data = path.read_bytes()
        except OSError:
            return None, 0
        return f"sha256:{hashlib.sha256(data).hexdigest()}", len(data)

    def record(self, locator: str) -> dict[str, Any] | None:
        """One JSON object this plan published, read at most once."""
        if locator in self._records:
            return self._records[locator]
        parsed: dict[str, Any] | None = None
        path = self._resolve(locator)
        if path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                loaded = None
            if isinstance(loaded, dict):
                parsed = loaded
        self._records[locator] = parsed
        return parsed

    def view(self) -> mailbox_snapshot.MailboxView | None:
        """The plan's mailbox, read once under a shared lock and never written."""
        if not self._view_read:
            self._view_read = True
            try:
                self._view = self._read_mailbox(self.plan_dir, state_dir=self.state_dir)
            except mailbox_models.MailboxError:
                self._view = None
        return self._view

    # -- projecting --------------------------------------------------------

    def project(
        self,
        kind: models.SourceKind,
        locator: str,
        *,
        max_records: int,
        max_summary_chars: int,
    ) -> models._Projection | None:
        """Describe one family from its source, short and long, or ``None``."""
        if kind in ("questions", "supervision"):
            view = self.view()
            if view is None:
                return None
            if kind == "questions":
                return projections._project_questions(
                    view,
                    run_id=self.run_id,
                    max_records=max_records,
                    max_summary_chars=max_summary_chars,
                )
            return projections._project_supervision(
                view, max_records=max_records, max_summary_chars=max_summary_chars
            )
        record = self.record(locator)
        if record is None:
            return None
        if kind == "status":
            return projections._project_status(
                record, max_summary_chars=max_summary_chars
            )
        if kind == "run_config":
            return projections._project_run_config(
                record, max_records=max_records, max_summary_chars=max_summary_chars
            )
        if kind == "verification":
            return projections._project_verification(
                record, max_records=max_records, max_summary_chars=max_summary_chars
            )
        return projections._project_worker_changes(
            record, max_records=max_records, max_summary_chars=max_summary_chars
        )
