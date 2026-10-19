"""Commit message generation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import harnesses

from . import commit_message_format as format
from . import commit_message_models as models

# ---------------------------------------------------------------------------
# Harness-backed generation
# ---------------------------------------------------------------------------


def resolve_harness(requested: str | None = None) -> str:
    """Pick the harness generation runs on: the flag first, else what is usable.

    A harness named in ``harnesses.json`` wins over one that is merely
    installed, because naming an executable there is the strongest statement of
    intent available; within each group the declared harness order decides.
    Nothing is selected that is not actually on PATH, so a missing binary is
    reported here instead of surfacing later as a launch failure.
    """
    from . import config, harnesses

    if requested is not None:
        return requested

    try:
        configured = config.load_harness_config()
    except config.ConfigError as error:
        raise models.CommitMessageError(
            str(error), reason=models.REASON_NO_HARNESS
        ) from error

    ordered = [name for name in harnesses.HARNESS_NAMES if name in configured]
    ordered += [name for name in harnesses.HARNESS_NAMES if name not in configured]
    for name in ordered:
        try:
            adapter = harnesses.get_adapter(name)
        except harnesses.HarnessError:
            continue
        if adapter.resolve_executable() is not None:
            return name

    raise models.CommitMessageError(
        "no harness executable found on PATH; pass --harness or configure one "
        f"in {config.default_config_path()}",
        reason=models.REASON_NO_HARNESS,
    )


def generation_settings(
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, str | None]:
    """Resolve and describe the harness, model, and effort a turn will use.

    Only the flags are ever handed to the harness: the model and effort
    payloads are reporting metadata, and ``native_model_payload`` deliberately
    names the *unresolved* default rather than a model string that could be
    passed as ``--model``.  Session 4's runner passes its own resolved values
    in as flags, so standalone and in-loop invocations share this one path.
    """
    from . import config

    name = resolve_harness(harness)
    try:
        executable = config.configured_harness_executable(name)
    except config.ConfigError as error:
        raise models.CommitMessageError(
            str(error), reason=models.REASON_NO_HARNESS
        ) from error

    settings: dict[str, str | None] = {
        "harness": name,
        "harness_executable": executable,
    }
    settings.update(config.selected_model_payload(name, model))
    settings.update(config.selected_effort_payload(effort))
    return settings


def _classified_harness_error(
    harness: str,
    result: harnesses.HarnessResult,
) -> models.CommitMessageError:
    """Turn a nonzero harness turn into the shared failure vocabulary.

    ``classify_harness_failure`` already distinguishes quota, authentication,
    invalid-model, transport, and interruption failures from an unrecognized
    exit, and ``retry_hints`` renders whatever reset hint the provider gave as
    an absolute instant, so neither is reinvented here.
    """
    from . import harness_failures, retry_hints

    classification = harness_failures.classify_harness_failure(result)
    detail = f"harness {harness!r} failed ({classification.reason})"
    if classification.evidence:
        detail += f": {classification.evidence}"
    retry_at = retry_hints.normalized_retry_at(classification.retry_at)
    if retry_at is not None:
        detail += f"; retry at {retry_at}"
    return models.CommitMessageError(
        detail,
        reason=models.REASON_GENERATION_FAILED,
        details={"classification": classification.to_dict(), "retry_at": retry_at},
    )


def _generation_timeout_error(
    harness: str,
    result: harnesses.HarnessResult,
    timeout: int | None,
) -> models.CommitMessageError:
    """Turn an expired commit-message turn into the shared failure vocabulary.

    A stall is reported under its own reason rather than folded into
    ``generation-failed``: the two ask for different responses, and only this
    one says the harness never answered at all. The classification is still
    attached, because ``run_harness`` may well have captured a transport error
    before the turn wedged, and ``process_sweep`` travels with it as the
    evidence that the turn's process tree was actually taken down.
    """
    from . import harness_failures

    classification = harness_failures.classify_harness_failure(result)
    bound = "its bound" if timeout is None else f"{timeout}s"
    return models.CommitMessageError(
        f"harness {harness!r} did not produce a commit message within {bound}",
        reason=models.REASON_GENERATION_TIMEOUT,
        details={
            "classification": classification.to_dict(),
            "timeout_seconds": timeout,
            "process_sweep": result.process_sweep,
        },
    )


def generate_commit_message(
    diff_result: models.DiffResult,
    *,
    repo_root: Path,
    harness: str,
    guidance: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: int | None = models.DEFAULT_GENERATION_TIMEOUT_SECONDS,
) -> str:
    """Run one read-only harness turn and return the message it produced.

    The turn runs under ``audit_read_only_access((repo_root,))``: writing a
    commit message only ever needs to read a diff the prompt already carries,
    so no adapter is handed a writable root and every adapter is hardened by
    the profile rather than by per-harness branching here.  ``plan_dir`` is
    ``None`` for the same reason — it is the worker's writable grant, and this
    turn has no plan.

    A nonzero exit is classified rather than reported as a bare exit code, and
    a response that is empty or is an error body is rejected instead of being
    committed: headless, nobody is watching to notice that the "commit message"
    is a stack of JSON.

    ``timeout`` bounds the turn by default. ``run_harness`` kills the turn's
    whole process group and sweeps whatever escaped it before returning, so a
    stalled harness that fanned out into helpers cannot outlive the bound; the
    expiry then leaves here as an ordinary :class:`CommitMessageError`, which
    is what puts a stall on the same soft-failure path as a refused commit.
    Passing ``None`` is an explicit request for an unbounded turn and no
    ``asl commit`` invocation can produce it.
    """
    from . import harnesses

    prompt = format.build_commit_prompt(diff_result, guidance)
    with tempfile.TemporaryDirectory(prefix="asl-commit-") as tmp:
        try:
            result = harnesses.run_harness(
                harness,
                cwd=repo_root,
                plan_dir=None,
                prompt=prompt,
                unsafe_bypass=False,
                last_message_path=Path(tmp) / "commit-last-message.txt",
                timeout=timeout,
                model=model,
                effort=effort,
                access=harnesses.audit_read_only_access((repo_root,)),
            )
        except harnesses.HarnessError as error:
            raise models.CommitMessageError(
                f"harness {harness!r} failed to launch: {error}",
                reason=models.REASON_GENERATION_FAILED,
            ) from error

    if result.timed_out:
        raise _generation_timeout_error(harness, result, timeout)
    if result.returncode != 0:
        raise _classified_harness_error(harness, result)

    response = result.last_message.strip()
    if not response:
        raise models.CommitMessageError(
            f"harness {harness!r} returned an empty commit message",
            reason=models.REASON_EMPTY_MESSAGE,
        )
    if format.looks_like_api_error(response):
        first_line = response.splitlines()[0]
        raise models.CommitMessageError(
            f"harness {harness!r} returned an API error instead of a commit "
            f"message: {first_line[:200]}",
            reason=models.REASON_API_ERROR,
        )
    return response
