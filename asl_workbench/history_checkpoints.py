"""History checkpoints."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from . import history_primitives as primitives

# -- Checkpoints ------------------------------------------------------------


@dataclass(frozen=True)
class OpenRun:
    """The facts a ``run_started`` carries that its terminal record does not.

    ASL's compact ``run_finished`` payload drops the plan directory, the
    harness, and the configured model, so a terminal record alone cannot name
    the plan it belongs to. These ride the checkpoint because the start that
    carried them may have been consumed by a pass that ran days ago.
    """

    run_id: str
    plan_dir: str | None = None
    harness: str | None = None
    model: str | None = None
    started_at: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "plan_dir": self.plan_dir,
            "harness": self.harness,
            "model": self.model,
            "started_at": self.started_at,
        }

    @classmethod
    def from_payload(cls, document: object) -> OpenRun | None:
        if not isinstance(document, Mapping):
            return None
        run_id = primitives._nonempty(document.get("run_id"))
        if run_id is None:
            return None
        return cls(
            run_id=run_id,
            plan_dir=primitives._nonempty(document.get("plan_dir")),
            harness=primitives._nonempty(document.get("harness")),
            model=primitives._nonempty(document.get("model")),
            started_at=primitives.epoch(document.get("started_at")),
        )


@dataclass(frozen=True)
class OpenQuestion:
    """A live mailbox wait whose closing record has not been read yet.

    Carried so a question that was asked and never cleared -- because the run
    died holding it -- is recorded as unresolved history rather than vanishing.
    """

    question_id: str
    run_id: str | None = None
    session_id: str | None = None
    asked_at: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "asked_at": self.asked_at,
        }

    @classmethod
    def from_payload(cls, document: object) -> OpenQuestion | None:
        if not isinstance(document, Mapping):
            return None
        question_id = primitives._nonempty(document.get("question_id"))
        if question_id is None:
            return None
        return cls(
            question_id=question_id,
            run_id=primitives._nonempty(document.get("run_id")),
            session_id=primitives._session_id(document.get("session_id")),
            asked_at=primitives.epoch(document.get("asked_at")),
        )


@dataclass(frozen=True)
class SourceCheckpoint:
    """How far one event file has been consumed, and what the file was.

    ``device``/``inode`` and ``head_digest`` are what make resuming safe.
    Together they answer "is this the same file I was reading", and the three
    ways the answer can be no -- replaced, truncated, rewritten in place -- all
    resolve to re-reading from the start, which duplicate suppression absorbs.

    ``head_length`` is recorded alongside the digest, and the pair is the whole
    trick: the digest covers a *fixed* prefix rather than "the first few
    kilobytes", so an ordinary append leaves it unchanged. Digesting whatever
    happens to be at the head today would make every append look like a rewrite
    on a file still shorter than the sample, and the reader would restart on
    every pass.
    """

    source_key: str
    device: int = -1
    inode: int = -1
    offset: int = 0
    head_digest: str = ""
    head_length: int = 0
    records: int = 0
    plan_dir: str | None = None
    open_runs: tuple[OpenRun, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    delivered: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "source_key": self.source_key,
            "device": self.device,
            "inode": self.inode,
            "offset": self.offset,
            "head_digest": self.head_digest,
            "head_length": self.head_length,
            "records": self.records,
            "plan_dir": self.plan_dir,
            "open_runs": [run.to_payload() for run in self.open_runs],
            "open_questions": [
                question.to_payload() for question in self.open_questions
            ],
            "delivered": list(self.delivered),
        }

    @classmethod
    def from_payload(cls, document: object) -> SourceCheckpoint | None:
        if not isinstance(document, Mapping):
            return None
        key = primitives._nonempty(document.get("source_key"))
        if key is None:
            return None
        runs = document.get("open_runs")
        open_runs = (
            tuple(
                run
                for run in (OpenRun.from_payload(entry) for entry in runs)
                if run is not None
            )
            if isinstance(runs, list)
            else ()
        )
        questions = document.get("open_questions")
        open_questions = (
            tuple(
                question
                for question in (
                    OpenQuestion.from_payload(entry) for entry in questions
                )
                if question is not None
            )
            if isinstance(questions, list)
            else ()
        )
        delivered = document.get("delivered")
        digest = document.get("head_digest")
        return cls(
            source_key=key,
            device=primitives._integer(document.get("device"), -1),
            inode=primitives._integer(document.get("inode"), -1),
            offset=max(0, primitives._integer(document.get("offset"), 0)),
            head_digest=digest if isinstance(digest, str) else "",
            head_length=max(0, primitives._integer(document.get("head_length"), 0)),
            records=max(0, primitives._integer(document.get("records"), 0)),
            plan_dir=primitives._nonempty(document.get("plan_dir")),
            open_runs=open_runs[-primitives.MAX_OPEN_RUNS :],
            open_questions=open_questions[-primitives.MAX_OPEN_QUESTIONS :],
            delivered=(
                tuple(entry for entry in delivered if isinstance(entry, str))[
                    -primitives.MAX_DELIVERED_IDS :
                ]
                if isinstance(delivered, list)
                else ()
            ),
        )

    def remember(self, run: OpenRun) -> SourceCheckpoint:
        """Record one run's start facts, replacing any earlier entry for it."""
        kept = tuple(entry for entry in self.open_runs if entry.run_id != run.run_id)
        return replace(self, open_runs=(*kept, run)[-primitives.MAX_OPEN_RUNS :])

    def find(self, run_id: str | None) -> OpenRun | None:
        """The remembered start for ``run_id``, joined by id and never by order."""
        if run_id is None:
            return None
        for entry in reversed(self.open_runs):
            if entry.run_id == run_id:
                return entry
        return None

    def remember_question(self, question: OpenQuestion) -> SourceCheckpoint:
        """Record one live wait, replacing any earlier entry for it."""
        kept = tuple(
            entry
            for entry in self.open_questions
            if entry.question_id != question.question_id
        )
        return replace(
            self,
            open_questions=(*kept, question)[-primitives.MAX_OPEN_QUESTIONS :],
        )

    def find_question(self, question_id: str | None) -> OpenQuestion | None:
        if question_id is None:
            return None
        for entry in reversed(self.open_questions):
            if entry.question_id == question_id:
                return entry
        return None

    def forget_question(self, question_id: str) -> SourceCheckpoint:
        kept = tuple(
            entry for entry in self.open_questions if entry.question_id != question_id
        )
        return replace(self, open_questions=kept)

    def questions_for_run(self, run_id: str | None) -> tuple[OpenQuestion, ...]:
        """Every still-open wait attributed to ``run_id``."""
        if run_id is None:
            return ()
        return tuple(entry for entry in self.open_questions if entry.run_id == run_id)

    def record_delivered(self, entry_ids: Iterable[str]) -> SourceCheckpoint:
        known = set(self.delivered)
        fresh = [entry for entry in entry_ids if entry not in known]
        if not fresh:
            return self
        return replace(
            self,
            delivered=(*self.delivered, *fresh)[-primitives.MAX_DELIVERED_IDS :],
        )


def checkpoint_document(
    checkpoints: Sequence[SourceCheckpoint],
) -> dict[str, object]:
    """The durable JSON document one set of checkpoints serialises to."""
    return {
        "version": primitives.CHECKPOINT_SCHEMA_VERSION,
        "sources": [
            checkpoint.to_payload()
            for checkpoint in sorted(checkpoints, key=lambda item: item.source_key)
        ],
    }


def checkpoints_from_document(document: object) -> dict[str, SourceCheckpoint]:
    """Read a checkpoint document, discarding anything it cannot vouch for.

    A document from an unknown schema, or one that is not a mapping, or one
    whose ``sources`` is not a list, yields no checkpoints. Starting the window
    again costs a re-read that duplicate suppression absorbs; guessing at a
    corrupted offset costs a run's ending.
    """
    if not isinstance(document, Mapping):
        return {}
    if document.get("version") != primitives.CHECKPOINT_SCHEMA_VERSION:
        return {}
    sources = document.get("sources")
    if not isinstance(sources, list):
        return {}
    loaded: dict[str, SourceCheckpoint] = {}
    for entry in sources:
        checkpoint = SourceCheckpoint.from_payload(entry)
        if checkpoint is not None:
            loaded[checkpoint.source_key] = checkpoint
    return loaded


def restart_required(
    checkpoint: SourceCheckpoint,
    *,
    device: int,
    inode: int,
    size: int,
    head: bytes = b"",
) -> bool:
    """Whether this file has to be read from the beginning again.

    Three separate things make a resume unsafe, and each is checked because each
    happens: the file was replaced (new inode), it was truncated or compacted to
    shorter than the offset, or it was rewritten in place at the same or greater
    length. Only the last needs the file's contents, and it is decided by
    re-digesting exactly the prefix the checkpoint digested -- a fixed byte range
    an append cannot disturb.

    ``head`` is the file's first :attr:`SourceCheckpoint.head_length` bytes. The
    caller reads them rather than this function, because this half of the
    program does not open files; the read is bounded by
    :data:`HEAD_SAMPLE_BYTES`, so it costs one four-kilobyte read per source per
    pass whether or not the answer turns out to need it.
    """
    if checkpoint.offset <= 0:
        return False
    if (checkpoint.device, checkpoint.inode) != (device, inode):
        return True
    if size < checkpoint.offset:
        return True
    if not checkpoint.head_digest or checkpoint.head_length <= 0:
        return False
    if len(head) < checkpoint.head_length:
        return True
    return (
        primitives.head_digest(head[: checkpoint.head_length]) != checkpoint.head_digest
    )
