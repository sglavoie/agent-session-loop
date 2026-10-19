from .asl_live_redirect_prompts import _excerpt, provider_refusal, ready_prompt
from .asl_live_redirect_support import *
from .asl_live_redirect_workspace import _LiveTurn, disposable_workspace


def check_reachable(harness: HarnessName, *, keep: bool = False) -> tuple[bool, str]:
    """Whether ``harness`` can complete a trivial turn, and why not if it cannot.

    This gate exists because "the provider would not serve this turn" and "the
    harness mishandled the turn" are different findings with different owners,
    and only one of them is this module's business. Without it an exhausted
    quota or a signed-out CLI is reported as a pile of protocol defects in a
    harness that never ran a single tool.
    """
    with disposable_workspace(f"{harness}-ready", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)
        try:
            run = turn.run(ready_prompt(), watch=None, timeout=READY_TIMEOUT_SECONDS)
        except harnesses.HarnessError as error:
            return False, f"the harness refused a trivial turn: {error}"
        message = run.result.last_message or ""
        combined = f"{message}\n{run.result.stderr or ''}"
        if run.result.timed_out:
            return False, (
                f"a trivial turn did not finish within {READY_TIMEOUT_SECONDS}s"
            )
        if run.result.returncode != 0:
            return False, (
                f"a trivial turn exited {run.result.returncode}: {_excerpt(combined)}"
            )
        marker = provider_refusal(combined)
        if marker is not None:
            return False, (
                f"the provider refused the turn ({marker}): {_excerpt(message)}"
            )
        if READY_TOKEN.lower() not in combined.lower():
            return False, (
                f"a trivial turn succeeded without answering it: {_excerpt(message)}"
            )
        return True, ""


def _read_lines(path: Path) -> dict[str, str]:
    """Parse the resumed worker's ``KEY=value`` receipt file."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().upper()] = value.strip().strip("`\"'")
    return values
