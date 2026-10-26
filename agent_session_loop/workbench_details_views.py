# ruff: noqa: F403, F405
import os

from .workbench_details_logs import *
from .workbench_details_logs import _missing
from .workbench_details_models import *
from .workbench_details_models import _JSONDict
from .workbench_details_open import *


def read_logs(
    reader: PlanReader,
    *,
    stream: str | None = None,
    artifact: str | None = None,
    path: str | None = None,
    window: Window | None = None,
) -> _JSONDict:
    """A bounded window over one of this plan's logs.

    The target is chosen by :meth:`asl_workbench.details.DetailScope.log_target`,
    which is the only place that decides whether a request may reach a file:
    a named artifact is resolved against what this plan published, and a
    supplied path is honoured only when this plan's state directory contains
    it.
    """
    resolved = Path(
        reader.scope.log_target(stream=stream, artifact=artifact, path=path)
    )
    if path is not None:
        _refuse_escaping_symlink(reader, resolved)
    payload = read_log_window(resolved, window or Window())
    if payload is None:
        raise _missing(resolved, "log file")
    return {"plan_dir": str(reader.plan_dir), **payload}


def _refuse_escaping_symlink(reader: PlanReader, candidate: Path) -> None:
    """Refuse a contained ``path=`` that a symlink points outside the state.

    The portable half proved the path lexically inside the plan state; only
    this half can follow the links in it.
    """
    try:
        target = candidate.resolve(strict=True)
        state_root = reader.plan_state_dir.resolve(strict=True)
    except OSError:
        raise _missing(candidate, "log file") from None
    if not target.is_relative_to(state_root):
        raise DetailError(
            details.REASON_INVALID_REQUEST,
            "path must be inside the plan state directory",
            details={
                "path": str(candidate),
                "plan_state_dir": reader.scope.plan_state_dir,
            },
        )


def read_last_message(
    reader: PlanReader,
    *,
    max_bytes: int = details.DEFAULT_LAST_MESSAGE_BYTES,
) -> _JSONDict:
    """The harness's last message for this plan's active turn, bounded."""
    try:
        resolved = Path(reader.scope.artifact("last_message_path", readable=True))
    except DetailError as error:
        if error.reason != details.REASON_NOT_FOUND:
            raise
        raise DetailError(
            details.REASON_NOT_FOUND,
            "no active last-message file for plan",
            details={"plan_dir": str(reader.plan_dir)},
        ) from error
    payload = read_bounded_text(resolved, max_bytes=max_bytes)
    if payload is None:
        raise _missing(resolved, "last-message file")
    return {"plan_dir": str(reader.plan_dir), **payload}


def read_result(
    reader: PlanReader,
    *,
    max_bytes: int = details.MAX_READ_BYTES,
) -> _JSONDict:
    """The active turn's ``result.json``, parsed, when it fits ``max_bytes``.

    Bounded like its siblings: a file larger than the bound is not read
    whole, and since a prefix of a JSON document does not parse, the answer
    says it was too large (``truncated``) with ``result: None`` -- the same
    way ``read_final_review`` leaves an oversized ``final-review.json`` out.
    """
    try:
        resolved = Path(reader.scope.artifact("result_path", readable=True))
    except DetailError as error:
        if error.reason != details.REASON_NOT_FOUND:
            raise
        raise DetailError(
            details.REASON_NOT_FOUND,
            "no active result.json for plan",
            details={"plan_dir": str(reader.plan_dir)},
        ) from error
    try:
        with resolved.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            # One byte past the bound tells a file that grew since the stat
            # apart from one that is exactly at it.
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError as error:
        raise _missing(resolved, "result file") from error
    except OSError as error:
        raise DetailError(
            details.REASON_OS_ERROR, str(error), details={"path": str(resolved)}
        ) from error
    if len(raw) > max_bytes:
        return {
            "plan_dir": str(reader.plan_dir),
            "path": str(resolved),
            "size": max(size, len(raw)),
            "truncated": True,
            "result": None,
        }
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text) if text else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DetailError(
            details.REASON_OS_ERROR,
            f"result file is not valid JSON: {error}",
            details={"path": str(resolved)},
        ) from error
    return {
        "plan_dir": str(reader.plan_dir),
        "path": str(resolved),
        "size": len(raw),
        "truncated": False,
        "result": parsed,
    }


# ---------------------------------------------------------------------------
# Progress and final review


def progress_entry_payload(entry: plans_models.ProgressEntry) -> _JSONDict:
    return {
        "session_id": entry.session_id.value,
        "status": entry.status,
        "completed_at": entry.completed_at,
        "files_modified": list(entry.files_modified),
        "key_decisions": entry.key_decisions,
        "discovered_facts": list(entry.discovered_facts),
        "open_questions": list(entry.open_questions),
        "blockers": entry.blockers,
    }


def read_progress(reader: PlanReader) -> _JSONDict:
    """The plan's progress journal, parsed into entries."""
    path = plans_progress.progress_path(reader.plan_dir)
    try:
        entries = plans_progress.read_progress_entries(reader.plan_dir)
    except plans_errors.PlanError as error:
        raise DetailError(
            details.REASON_MALFORMED_PLAN,
            str(error),
            details={"plan_dir": str(reader.plan_dir), "path": str(path)},
        ) from error
    return {
        "plan_dir": str(reader.plan_dir),
        "path": str(path),
        "exists": path.is_file(),
        "entries": [progress_entry_payload(entry) for entry in entries],
    }


def read_final_review(
    reader: PlanReader,
    *,
    max_bytes: int = details.DEFAULT_FINAL_REVIEW_BYTES,
) -> _JSONDict:
    """``final-review.md`` bounded, plus ``final-review.json`` when it parses.

    The markdown falls back to the plan's canonical ``final-review.md`` when
    the summary resolved none, so asking a plan that has not been reviewed
    gets a 404 naming the file it would be rather than a refusal naming an
    artifact vocabulary the operator did not use.
    """
    resolved = reader.scope.artifacts.get("final_review_path")
    path = (
        Path(resolved)
        if resolved is not None
        else reader.plan_dir / workbench_summary_helpers.FINAL_REVIEW_FILE_NAME
    )
    payload = read_bounded_text(path, max_bytes=max_bytes)
    if payload is None:
        raise _missing(path, "final-review file")
    artifact_path = Path(reader.scope.artifact("final_review_artifact_path"))
    structured_review = None
    structured_error = None
    try:
        if artifact_path.is_file() and artifact_path.stat().st_size <= max_bytes:
            decoded = json.loads(artifact_path.read_text(encoding="utf-8"))
            if isinstance(decoded, dict):
                structured_review = decoded
            else:
                structured_error = "final-review.json did not contain an object"
    except (OSError, json.JSONDecodeError) as error:
        structured_error = str(error)
    try:
        workspace = workbench_workspace._workspace_projection(reader.plan_dir)
    except plans_errors.PlanError, OSError, ValueError:
        workspace = {}
    return {
        "plan_dir": str(reader.plan_dir),
        "artifact_path": str(artifact_path),
        "structured_review": structured_review,
        "structured_error": structured_error,
        "review_resume": reader.summary.review_resume,
        "workspace": workspace,
        **workbench_workspace._workspace_payload(workspace, cwd=reader.summary.cwd),
        **payload,
    }


# ---------------------------------------------------------------------------
# Questions and recovery


#: Envelope keys ``qna.questions_payload`` adds for its own command. The
#: Workbench re-emits the rest under its own envelope, so the resolver prompt
#: sees one ``command`` value whichever route it arrived through.
_QNA_ENVELOPE_KEYS = frozenset({"ok", "command", "reason", "paused"})


def read_questions(
    plan_dir: str | Path,
    *,
    question_id: str | None = None,
    state_dir: str | Path | None = None,
) -> _JSONDict:
    """One selected question context, classified without reclaiming.

    ``reclaim=False``: loading the resolver prompt is how an operator finds out
    a run is waiting, and finding out must not be what releases the lock the
    waiting run is holding.
    """
    try:
        facts = intervention.inspect_run(plan_dir, state_dir=state_dir)
        if facts.run_active and facts.context_kind == "waiting":
            selected_question = question_id or facts.question_id
            if selected_question is None:
                raise intervention.InterventionError(
                    intervention.REASON_NO_OPEN_QUESTION,
                    "no open question is waiting for a reply",
                )
            resolved_question_id, context_kind, body = intervention.question_body(
                plan_dir,
                question_id=selected_question,
                state_dir=state_dir,
            )
            intervention.enforce_body_bound(body)
            return {
                "context_kind": context_kind,
                "run_id": facts.run_id,
                "question_id": resolved_question_id,
                "actions": list(intervention.permitted_actions(facts)),
                "action_token": intervention.action_token(facts),
                "last_message": body,
                "rendered_prompt": body,
            }
    except intervention.InterventionError as error:
        if question_id is not None:
            raise DetailError(
                details.REASON_NOT_FOUND,
                error.message,
                status=404,
                details={"plan_dir": str(plan_dir), **error.details},
            ) from error
        # The Q&A reader below owns the established paused-run error contract.
        # A live-question inspection failure must not replace it with a second
        # vocabulary when the plan is actually paused.
        facts = None

    try:
        context = qna.load_qna_context(plan_dir, state_dir=state_dir, reclaim=False)
    except qna.QnAError as error:
        raise DetailError(
            error.reason,
            error.error,
            status=404 if error.reason == qna.REASON_NO_PAUSED_RUN else 400,
            details={"plan_dir": str(plan_dir), **error.details},
        ) from error
    except plans_errors.PlanError as error:
        raise DetailError(
            details.REASON_MALFORMED_PLAN,
            str(error),
            status=400,
            details={"plan_dir": str(plan_dir)},
        ) from error
    payload = qna.questions_payload(context)
    answer = {
        key: value for key, value in payload.items() if key not in _QNA_ENVELOPE_KEYS
    }
    if facts is None:
        try:
            facts = intervention.inspect_run(plan_dir, state_dir=state_dir)
        except intervention.InterventionError:
            facts = None
    if facts is not None:
        answer.update(
            {
                "context_kind": context.context_kind,
                "actions": list(intervention.permitted_actions(facts)),
                "action_token": intervention.action_token(facts),
            }
        )
    return answer


def read_recovery(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> _JSONDict:
    """The recovery card for one plan: evidence, artifacts, and actions.

    ``reclaim=False`` on the recovery builder. The recovery view is the one an
    operator opens precisely because something looks wrong, so a reclaim here
    would repair the corpus at the exact moment they were trying to read what
    state it was in -- and would change the diagnosis a second reader gets.
    Releasing a lock stays ``POST /api/unlock``, which the card itself offers
    as an action.
    """
    canonical = canonical_plan_dir(plan_dir)
    if not canonical.is_dir():
        raise DetailError(
            details.REASON_INVALID_PLAN,
            f"plan directory does not exist: {canonical}",
            details={"plan_dir": str(canonical)},
        )
    try:
        snapshot = plans_snapshot.inspect_plan(canonical)
    except plans_errors.PlanError as error:
        raise DetailError(
            details.REASON_MALFORMED_PLAN,
            str(error),
            details={"plan_dir": str(canonical)},
        ) from error
    discovered = workbench_registry_models.DiscoveredPlan(
        plan_dir=snapshot.plan_dir, exists=True
    )
    recovery = workbench_recovery_build.build_workbench_recovery(
        discovered,
        state_dir=state_dir,
        reclaim=False,
    )
    return {
        "plan_dir": str(snapshot.plan_dir),
        "recovery": recovery.to_payload(),
        "artifacts": [artifact.to_payload() for artifact in recovery.artifacts],
        "run_id": recovery.run_id,
        "session_id": recovery.session_id,
        "turn_index": recovery.turn_index,
        "commit_index": recovery.commit_index,
    }


# ---------------------------------------------------------------------------
# Online search over private detail


def search_plans(
    query: object,
    *,
    state_dir: str | Path | None = None,
    registry: workbench_registry_models.WorkbenchRegistry | None = None,
    plan_roots: Sequence[str | Path] = (),
    include_default_dirs: bool = True,
) -> list[_JSONDict]:
    """Search every visible plan's private detail, online and unretained.

    This is the search the Workbench's browser does today over
    ``searchable_text``, moved behind a service so a remote cockpit can ask for
    it instead of being handed the corpus. Every field it reads -- the plan
    path, the operator's pin label, the pause message, the last-message
    preview -- is on ``asl_workbench.catalog.PRIVATE_FIELDS``, so a hit is an
    answer to one question rather than a row to retain: nothing here is
    reachable from :class:`asl_workbench.catalog.CatalogEntry`, which carries
    its own cache-safe ``search_text``.

    One non-reclaiming catalog scan backs it, so searching leaves the corpus
    exactly as searching the queue does -- unchanged.
    """
    scan = workbench_catalog.scan(
        state_dir=state_dir,
        registry=registry,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    hits = details.search_details(
        (record.detail for record in scan.catalog.records), query
    )
    return [details.search_payload(detail) for detail in hits]
