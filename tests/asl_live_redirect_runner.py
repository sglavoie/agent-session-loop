from .asl_live_redirect_availability import check_reachable
from .asl_live_redirect_scenarios import (
    _interrupt_and_continue,
    _native_cancellation,
    _unavailable_history,
)
from .asl_live_redirect_support import *


def run_harness_acceptance(
    harness: HarnessName,
    *,
    opted_in: bool,
    keep: bool = False,
) -> HarnessAcceptance:
    """Probe one harness and, when opted in, run the full matrix against it."""
    started = time.monotonic()
    matrix = _Matrix()
    limitations: list[str] = []
    compat = harness_compatibility.check_harness(harness)
    adapter = harnesses.get_adapter(harness)
    capabilities: dict[str, Any] = {
        "captures_conversations": bool(adapter.captures_conversations),
        "resumes_conversations": bool(adapter.resumes_conversations),
        "continues_conversations": harnesses.continues_conversations(harness),
        "native_interrupt": None,
        "transport": None,
        # None means "never asked", which is different from "asked and no".
        "reachable": None,
    }

    def finish() -> HarnessAcceptance:
        return HarnessAcceptance(
            harness=harness,
            executable=compat.executable,
            version=compat.version,
            compatibility=compat.status,
            compatibility_reason=compat.reason,
            capabilities=capabilities,
            results=matrix.results(),
            limitations=limitations,
            seconds=time.monotonic() - started,
        )

    if not opted_in:
        matrix.fill(
            STATUS_SKIPPED,
            f"not opted in; pass --live or set {OPT_IN_ENV_VAR}=1",
        )
        limitations.append(
            f"{harness}: no live turn was run, so nothing in this report is "
            "acceptance evidence for it"
        )
        return finish()
    if compat.executable is None:
        matrix.fill(STATUS_SKIPPED, f"{harness} is not installed on PATH")
        limitations.append(
            f"{harness}: not installed, so no version of it has been verified"
        )
        return finish()
    if compat.blocks:
        matrix.fill(
            STATUS_SKIPPED,
            f"{harness} {compat.version} is {compat.status}: {compat.detail}",
        )
        limitations.append(
            f"{harness}: installed version {compat.version} is known "
            "incompatible and was not exercised"
        )
        return finish()

    print(f"== {harness} {compat.version or 'unknown version'} ({compat.executable})")
    print(f"-- {harness}: reachability")
    reachable, why = check_reachable(harness, keep=keep)
    if not reachable:
        # Skipped, not failed. Nothing was learned about the harness, and a
        # gate that went red here would be reporting the state of an account.
        # Short per row, complete once: repeating a provider's error on all
        # seven rows buries the six other harnesses' results under it.
        matrix.fill(
            STATUS_SKIPPED, f"{harness} could not be exercised (see limitations)"
        )
        limitations.append(f"{harness}: {why}")
        capabilities["reachable"] = False
        print(f"   unreachable: {why}")
        return finish()
    capabilities["reachable"] = True
    sequences: tuple[
        tuple[str, Callable[[HarnessName, _Matrix], dict[str, Any]]], ...
    ] = (
        (
            "interrupt-and-continue",
            lambda name, m: _interrupt_and_continue(name, m, keep=keep),
        ),
        (
            "native-cancellation",
            lambda name, m: _native_cancellation(name, m, keep=keep),
        ),
        (
            "unavailable-history",
            lambda name, m: _unavailable_history(name, m, keep=keep),
        ),
    )
    for label, sequence in sequences:
        print(f"-- {harness}: {label}")
        try:
            observed = sequence(harness, matrix)
        except Exception as error:
            limitations.append(
                f"{harness}: the {label} sequence raised "
                f"{type(error).__name__}: {error}"
            )
            print(f"   sequence failed: {type(error).__name__}: {error}")
            continue
        transport = (observed.get("turn") or observed.get("first_turn") or {}).get(
            "transport"
        )
        if transport and capabilities["transport"] is None:
            capabilities["transport"] = transport
    for result in matrix.results():
        if result.key == SCENARIO_NATIVE_CANCEL:
            capabilities["native_interrupt"] = result.evidence.get("interrupt_answered")
    matrix.fill(STATUS_FAILED, "the sequence that owns this scenario did not report")
    return finish()
