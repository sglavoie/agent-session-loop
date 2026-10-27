"""Harness CLI version compatibility policy.

ASL launches three third-party command-line harnesses, and each of them ships
on its own release cadence. This module is the one place that says which
versions this build has actually been exercised against, and what happens when
the installed CLI is not one of them.

The policy has exactly three outcomes and they are deliberately asymmetric:

``supported``
    The installed version is inside the pinned window: the latest and the
    previous line this build was smoke-tested against, and everything between
    them.

``unknown``
    Everything else that is not positively known to be broken - a version
    newer than the pinned latest, an older one nobody has retested, an
    executable that would not report a version, or output this module cannot
    parse. It **warns**. Refusing to run on an unrecognised version would make
    every harness release an outage, and a version ASL has not tested is not
    evidence of incompatibility.

``incompatible``
    A version listed in :attr:`HarnessPolicy.blocked` or older than
    :attr:`HarnessPolicy.minimum`. It **blocks**, because a known-broken
    harness produces a run whose evidence cannot be trusted, and a run that
    cannot be trusted is worse than one that never started.

Probing is a no-model-use operation by construction: the only subprocess this
module ever runs is ``<executable> --version``, which prints a build string and
exits. Nothing here sends a prompt, opens a session, or reaches a provider, so
the scheduled compatibility smokes can run on every supported harness release
without incurring model usage.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from . import harnesses, responses
from .harnesses import HARNESS_NAMES, HarnessName

#: Schema version of the :func:`report_payload` document. Consumers (the
#: scheduled CI smoke, `asl harness-compat --json`) branch on this.
SCHEMA_VERSION = 1

#: Outcome vocabulary. ``unknown`` warns; ``incompatible`` blocks.
Status = Literal["supported", "unknown", "incompatible"]

#: What a status does to a run. Kept separate from the status name so callers
#: branch on the consequence rather than on a string they have to interpret.
Severity = Literal["ok", "warn", "block"]

STATUS_SEVERITY: Mapping[Status, Severity] = {
    "supported": "ok",
    "unknown": "warn",
    "incompatible": "block",
}

#: How long a version probe may take before it is abandoned. A harness CLI that
#: cannot print its own version in this window is reported as ``unknown``
#: rather than waited on: preflight runs this on the path to every run.
PROBE_TIMEOUT_SECONDS = 20.0

#: Environment switch that skips probing entirely. Set it to ``0`` on a host
#: where spawning the harness is undesirable; every harness then reports
#: ``unknown`` with reason ``probe-disabled``, which warns and never blocks.
PROBE_ENV_VAR = "ASL_HARNESS_VERSION_PROBE"

#: First dotted numeric run in the CLI's ``--version`` output. Every supported
#: harness embeds its version in a different sentence - ``codex-cli 0.149.1``,
#: ``2.1.231 (Claude Code)``, ``GitHub Copilot CLI 1.0.80.`` - so the shape is
#: matched rather than the surrounding prose.
_VERSION_PATTERN = re.compile(r"(?<![0-9.])([0-9]+(?:\.[0-9]+)+)(?![0-9])")


@dataclass(frozen=True)
class HarnessPolicy:
    """The pinned support window for one harness CLI.

    ``latest`` and ``previous`` are the two lines the scheduled compatibility
    smoke installs and exercises. ``minimum`` is the oldest version this build
    will run at all; anything below it blocks. ``blocked`` names individual
    releases that are inside the window but known broken.
    """

    harness: HarnessName
    version_argv: tuple[str, ...]
    latest: str
    previous: str
    minimum: str
    blocked: tuple[str, ...] = ()
    note: str = ""

    def pinned_versions(self) -> tuple[str, ...]:
        """The versions the scheduled smoke is expected to cover."""
        if self.latest == self.previous:
            return (self.latest,)
        return (self.latest, self.previous)


#: The pinned window per harness, recorded against the versions this program
#: was developed and gated on. Moving any value here is a compatibility policy
#: change and belongs in the same commit as the smoke evidence that justifies
#: it.
POLICY: Mapping[HarnessName, HarnessPolicy] = {
    "codex": HarnessPolicy(
        harness="codex",
        version_argv=("--version",),
        latest="0.154.0",
        previous="0.148.0",
        minimum="0.140.0",
        note="Codex CLI reports `codex-cli <version>`.",
    ),
    "claude": HarnessPolicy(
        harness="claude",
        version_argv=("--version",),
        latest="2.1.236",
        previous="2.0.0",
        minimum="1.0.0",
        note="Claude Code reports `<version> (Claude Code)`.",
    ),
    "copilot": HarnessPolicy(
        harness="copilot",
        version_argv=("--version",),
        latest="1.0.80",
        previous="1.0.79",
        minimum="1.0.0",
        note="GitHub Copilot CLI reports `GitHub Copilot CLI <version>`.",
    ),
}


@dataclass(frozen=True)
class Probe:
    """What ``<executable> --version`` reported, or why nothing was learned."""

    harness: HarnessName
    executable: str | None
    version: str | None
    raw_output: str
    reason: str


@dataclass(frozen=True)
class Compatibility:
    """One harness classified against :data:`POLICY`."""

    harness: HarnessName
    status: Status
    version: str | None
    executable: str | None
    reason: str
    detail: str

    @property
    def severity(self) -> Severity:
        return STATUS_SEVERITY[self.status]

    @property
    def blocks(self) -> bool:
        return self.severity == "block"

    def payload(self) -> dict[str, Any]:
        policy = POLICY[self.harness]
        return {
            "harness": self.harness,
            "status": self.status,
            "severity": self.severity,
            "version": self.version,
            "executable": self.executable,
            "reason": self.reason,
            "detail": self.detail,
            "policy": {
                "latest": policy.latest,
                "previous": policy.previous,
                "minimum": policy.minimum,
                "blocked": list(policy.blocked),
            },
        }


def parse_version(text: str) -> str | None:
    """Return the first dotted numeric version in ``text``, or ``None``."""
    match = _VERSION_PATTERN.search(text)
    return match.group(1) if match else None


def version_key(version: str) -> tuple[int, ...]:
    """Comparable form of a dotted numeric version.

    Non-numeric trailing segments are dropped rather than guessed at, so
    ``1.2.3-beta.1`` orders as ``1.2.3``. A prerelease that ASL must refuse
    belongs in :attr:`HarnessPolicy.blocked` by its exact string.
    """
    parts: list[int] = []
    for segment in version.split("."):
        if segment.isdigit():
            parts.append(int(segment))
            continue
        # A segment that is only partly numeric ends the comparable prefix:
        # `3-beta` contributes 3 and nothing after it is ordered, so
        # `1.2.3-beta.1` and `1.2.3` compare equal rather than the prerelease
        # sorting above its own release.
        leading = segment[: len(segment) - len(segment.lstrip("0123456789"))]
        if leading:
            parts.append(int(leading))
        break
    return tuple(parts)


def _compare(left: str, right: str) -> int:
    """Three-way compare two dotted versions, padding the shorter one."""
    left_key, right_key = version_key(left), version_key(right)
    width = max(len(left_key), len(right_key))
    padded_left = left_key + (0,) * (width - len(left_key))
    padded_right = right_key + (0,) * (width - len(right_key))
    if padded_left < padded_right:
        return -1
    return 1 if padded_left > padded_right else 0


def probing_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether version probing is switched on for this process."""
    source = os.environ if env is None else env
    raw = source.get(PROBE_ENV_VAR, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


#: In-process probe memo keyed by the resolved executable's identity, so one
#: preflight (and one run that re-enters preflight) spawns each harness at most
#: once. Keyed by ``(path, size, mtime_ns)`` rather than by path alone: an
#: upgrade during a long-lived supervisor changes at least one of those.
_probe_cache: dict[tuple[str, int, int], Probe] = {}


def _executable_identity(path: str) -> tuple[str, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (path, stat.st_size, stat.st_mtime_ns)


def probe_executable(
    harness: HarnessName,
    executable: str,
    *,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    use_cache: bool = True,
) -> Probe:
    """Run ``<executable> --version`` and report what it said.

    This never fails: every way of learning nothing (probing switched off, a
    non-zero exit, a timeout, an unreadable executable, unparseable output)
    comes back as a :class:`Probe` with ``version`` unset and a ``reason``
    naming the outcome. Classification turns that into ``unknown``, which warns.
    """
    if not probing_enabled(env):
        return Probe(harness, executable, None, "", "probe-disabled")

    identity = _executable_identity(executable)
    if use_cache and identity is not None and identity in _probe_cache:
        return _probe_cache[identity]

    policy = POLICY[harness]
    argv = [executable, *policy.version_argv]
    try:
        returncode, output = _run_version_command(argv, timeout_seconds)
    except OSError as error:
        probe = Probe(harness, executable, None, str(error), "probe-error")
    else:
        if returncode is None:
            probe = Probe(harness, executable, None, output, "probe-timeout")
        elif returncode != 0:
            probe = Probe(
                harness,
                executable,
                None,
                output,
                f"probe-exit-{returncode}",
            )
        else:
            version = parse_version(output)
            probe = Probe(
                harness,
                executable,
                version,
                output,
                "probed" if version else "unparseable-version",
            )
    if use_cache and identity is not None:
        _probe_cache[identity] = probe
    return probe


def _run_version_command(
    argv: Sequence[str], timeout_seconds: float
) -> tuple[int | None, str]:
    """Run ``argv`` and return its exit status (``None`` on timeout) and output.

    Output goes to temporary files rather than pipes, and the wait is on the
    process itself: a CLI that leaves a background child holding its stdout
    has still answered once it exits. On a timeout the whole process group is
    killed and whatever it printed so far is kept.
    """
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            returncode: int | None = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            returncode = None
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        texts = []
        for stream in (stdout, stderr):
            stream.seek(0)
            # A CLI may print bytes that are not UTF-8; quote them rather than
            # raising, because this probe must never fail.
            texts.append(stream.read().decode("utf-8", errors="replace"))
    return returncode, "\n".join(texts).strip()


def clear_probe_cache() -> None:
    """Forget every memoized probe. Test seam; also safe at runtime."""
    _probe_cache.clear()


def classify(harness: HarnessName, version: str | None) -> Compatibility:
    """Classify one harness version against :data:`POLICY`."""
    policy = POLICY[harness]
    if version is None:
        return Compatibility(
            harness=harness,
            status="unknown",
            version=None,
            executable=None,
            reason="version-unavailable",
            detail=(
                f"{harness} did not report a parseable version; "
                f"tested versions are {' and '.join(policy.pinned_versions())}"
            ),
        )
    if version in policy.blocked:
        return Compatibility(
            harness=harness,
            status="incompatible",
            version=version,
            executable=None,
            reason="blocked-version",
            detail=(
                f"{harness} {version} is a known-incompatible release; "
                f"upgrade to {policy.latest}"
            ),
        )
    if _compare(version, policy.minimum) < 0:
        return Compatibility(
            harness=harness,
            status="incompatible",
            version=version,
            executable=None,
            reason="below-minimum",
            detail=(
                f"{harness} {version} is older than the minimum supported "
                f"{policy.minimum}; upgrade to {policy.latest}"
            ),
        )
    if (
        _compare(version, policy.previous) >= 0
        and _compare(version, policy.latest) <= 0
    ):
        return Compatibility(
            harness=harness,
            status="supported",
            version=version,
            executable=None,
            reason="pinned-window",
            detail=(
                f"{harness} {version} is inside the tested window "
                f"{policy.previous}..{policy.latest}"
            ),
        )
    if _compare(version, policy.latest) > 0:
        return Compatibility(
            harness=harness,
            status="unknown",
            version=version,
            executable=None,
            reason="newer-than-tested",
            detail=(
                f"{harness} {version} is newer than the tested "
                f"{policy.latest}; this pairing has not been smoke-tested"
            ),
        )
    return Compatibility(
        harness=harness,
        status="unknown",
        version=version,
        executable=None,
        reason="older-than-tested",
        detail=(
            f"{harness} {version} is older than the tested {policy.previous} "
            f"but at or above the minimum {policy.minimum}"
        ),
    )


def check_executable(
    harness: HarnessName,
    executable: str,
    *,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    use_cache: bool = True,
) -> Compatibility:
    """Probe ``executable`` and classify what it reported."""
    probe = probe_executable(
        harness,
        executable,
        timeout_seconds=timeout_seconds,
        use_cache=use_cache,
    )
    classified = classify(harness, probe.version)
    reason = classified.reason if probe.version else probe.reason
    detail = classified.detail
    if probe.version is None and probe.raw_output:
        detail = f"{detail} (output: {_summarize(probe.raw_output)})"
    return Compatibility(
        harness=classified.harness,
        status=classified.status,
        version=classified.version,
        executable=executable,
        reason=reason,
        detail=detail,
    )


def _summarize(text: str, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def check_harness(
    harness: HarnessName,
    *,
    resolver: Callable[[str], str | None] | None = None,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    use_cache: bool = True,
) -> Compatibility:
    """Resolve ``harness`` on PATH, probe it, and classify the result.

    An unresolvable executable is ``unknown``, not ``incompatible``: preflight
    already refuses a missing harness with its own red finding, and this module
    must not turn "not installed" into "known broken".
    """
    resolve = resolver if resolver is not None else _default_resolver
    executable = resolve(harness)
    if executable is None:
        return Compatibility(
            harness=harness,
            status="unknown",
            version=None,
            executable=None,
            reason="executable-not-found",
            detail=f"{harness} was not found on PATH",
        )
    return check_executable(
        harness,
        executable,
        timeout_seconds=timeout_seconds,
        use_cache=use_cache,
    )


def _default_resolver(harness: str) -> str | None:
    try:
        adapter = harnesses.get_adapter(harness)
    except harnesses.HarnessError:
        return None
    return adapter.resolve_executable()


def check_all(
    harnesses_to_check: Sequence[HarnessName] | None = None,
    *,
    resolver: Callable[[str], str | None] | None = None,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    use_cache: bool = True,
) -> tuple[Compatibility, ...]:
    """Classify every requested harness once, in :data:`HARNESS_NAMES` order."""
    wanted = set(harnesses_to_check or HARNESS_NAMES)
    requested = tuple(name for name in HARNESS_NAMES if name in wanted)
    return tuple(
        check_harness(
            harness,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            use_cache=use_cache,
        )
        for harness in requested
    )


def report_payload(
    results: Sequence[Compatibility], *, strict: bool = False
) -> dict[str, Any]:
    """The stable JSON document for ``asl harness-compat --json``.

    ``ok`` agrees with :func:`exit_code`: under ``strict`` an unknown harness
    fails the check, so it is not ``ok`` either.
    """
    counts = {"supported": 0, "unknown": 0, "incompatible": 0}
    for result in results:
        counts[result.status] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "harness-compat",
        "ok": exit_code(results, strict=strict) == 0,
        "model_use": False,
        "counts": counts,
        "harnesses": [result.payload() for result in results],
    }


def exit_code(results: Sequence[Compatibility], *, strict: bool) -> int:
    """0 clean, 1 when anything blocks, 3 when ``--strict`` rejects a warning."""
    if any(result.blocks for result in results):
        return 1
    if strict and any(result.status != "supported" for result in results):
        return 3
    return 0


def _format_text(results: Sequence[Compatibility]) -> str:
    marks = {"supported": "ok", "unknown": "warn", "incompatible": "BLOCK"}
    lines = []
    for result in results:
        version = result.version or "unknown"
        lines.append(
            f"{marks[result.status]:>5}  {result.harness:<8} {version:<12} "
            f"{result.reason}: {result.detail}"
        )
    return "\n".join(lines)


def harness_compat_from_args(args: argparse.Namespace) -> int:
    """``asl harness-compat`` entry point."""
    requested: tuple[HarnessName, ...] = tuple(getattr(args, "harness", None) or ())
    results = check_all(requested or None)
    strict = bool(getattr(args, "strict", False))
    if getattr(args, "json", False):
        responses.print_json(report_payload(results, strict=strict))
    else:
        print(_format_text(results))
    return exit_code(results, strict=strict)
