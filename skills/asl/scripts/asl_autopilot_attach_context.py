"""Question and review context attached to a finished supervision result."""

from __future__ import annotations

from asl_autopilot_constants import REVIEW_SUMMARY_MAX_CHARS
from asl_autopilot_launch_policy import _review_resume_payload

from agent_session_loop import review as _review


def _bounded_review_summary(
    text: str, *, max_chars: int = REVIEW_SUMMARY_MAX_CHARS
) -> tuple[str, bool]:
    """Return a bounded review excerpt and whether it was truncated.

    The ``(text, truncated)`` pair lets the caller flag the truncation
    explicitly instead of silently dropping the tail: an untruncated body
    returns ``(body, False)``; an over-length body returns the bounded excerpt
    with a trailing marker and ``True`` so callers can point at the full-review
    escape hatch (`asl review-verdict`).
    """
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped, False
    return stripped[:max_chars].rstrip() + "\n… (truncated)", True


def _read_review_verdict(parsed: dict) -> dict:
    """Read and classify the review artifacts for the supervised run.

    Delegates to :func:`agent_session_loop.review.load_review_verdict` — the
    same loader the runner enforces mid-run — so both report the same verdict
    payload (``review_output_path`` attached; structured ``findings`` and
    ``artifact_version`` merged in when ``final-review.json`` is present). A
    missing or unreadable file yields ``ok: False`` with ``reason:
    "review-missing"`` so callers can distinguish "no review artifact" from a
    grammar violation. On success a bounded ``summary`` of the review body is
    attached with a ``summary_truncated`` flag and a ``full_review`` hint naming
    ``asl review-verdict`` (the authoritative untruncated surface), so a long
    review is never silently clipped.
    """
    verdict = _review.load_review_verdict(parsed["plan_dir"])
    if verdict.get("ok"):
        try:
            with open(verdict["review_output_path"], encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = ""
        summary, summary_truncated = _bounded_review_summary(text)
        verdict["summary"] = summary
        verdict["summary_truncated"] = summary_truncated
        verdict["full_review"] = (
            f"run `asl review-verdict {parsed['plan_dir']}` for the full, "
            "untruncated review verdict, findings, and remediation steps"
        )
    return verdict


def _attach_question_context(result: dict, parsed: dict, questioner) -> dict:
    """Turn a ``paused`` supervision result into ``needs_agent_reply``.

    Fetches the paused-run question context via ``asl questions --json`` and,
    when it loads, re-tags the result as ``needs_agent_reply`` with the resolver
    prompt, last message, and artifact paths attached so the host agent can
    infer a reply (or ask the user). If the context cannot be loaded the paused
    result is left as-is with the failure recorded under ``questions_error``.
    """
    questions = questioner(parsed)
    if isinstance(questions, dict) and questions.get("ok"):
        result["reason"] = "needs_agent_reply"
        result["questions"] = questions
    else:
        result["questions_error"] = questions
    return result


def _attach_review_context(result: dict, parsed: dict, reader) -> dict:
    """Re-tag a completed run as ``review_findings`` when review found issues.

    After a run that used ``--review-harness``, ``final-review.md`` holds the
    verdict. The runner already drove its bounded repair/rereview cycles
    before completing, so remaining findings mean automatic repair stopped
    (all-clear never came); this re-tags the completed result as
    ``review_findings`` (``ok: False``, mirroring the ``needs_agent_reply``
    handshake) and attaches the final-review path, status line, severity
    counts, and a bounded summary under ``review`` so the host agent can
    surface them to the user. An all-clear review keeps the result
    ``complete`` but still attaches the clean verdict. A missing or malformed
    review file also leaves the result ``complete`` and records the problem
    under ``review_error`` — the runner already pauses on a malformed verdict
    mid-run, so reaching a clean ``complete`` with an unreadable file is not
    expected.
    """
    if "review_resume" not in result:
        review_resume = _review_resume_payload(result.get("run")) or (
            _review_resume_payload(result.get("status"))
        )
        if review_resume is not None:
            result["review_resume"] = review_resume

    verdict = reader(parsed)
    if not verdict.get("ok"):
        result["review_error"] = verdict
        return result
    if verdict.get("outcome") == "findings":
        result["ok"] = False
        result["reason"] = "review_findings"
    result["review"] = verdict
    return result
