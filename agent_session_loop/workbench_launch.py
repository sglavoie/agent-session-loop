"""Workbench plan launch and reply-resume."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import (
    cli_trace,
    harnesses,
    plans_files,
    run_contention,
    workbench_workspace,
)
from .workbench_registry_models import COMMIT_MODES, LaunchProfile


def _workspace_launch_profile(
    plan_dir: Path, profile: LaunchProfile
) -> tuple[LaunchProfile, dict[str, Any]]:
    """Bind a registered Workbench launch to its prepared workspace root."""
    workspace = workbench_workspace._workspace_projection(plan_dir)
    bound_root = workspace.get("workspace_root")
    if not isinstance(bound_root, str) or not bound_root:
        return profile, workspace
    if profile.cwd is not None:
        try:
            requested = plans_files.canonicalize_path(profile.cwd)
            bound = plans_files.canonicalize_path(bound_root)
        except (OSError, ValueError) as error:
            raise LaunchError(
                LAUNCH_REASON_INVALID_PROFILE,
                f"invalid workspace root in launch profile: {error}",
            ) from error
        if requested != bound:
            raise LaunchError(
                LAUNCH_REASON_INVALID_PROFILE,
                "launch cwd does not match the prepared workspace binding; "
                "use `asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE "
                "--rebind-workspace` to change it atomically",
            )
    return replace(profile, cwd=bound_root), workspace


class LaunchError(Exception):
    """Raised when a Workbench launch request cannot be honored.

    ``spawned`` answers the question a durable journal has to ask about every
    failure: was there a child? ``process-failed`` is raised from both sides of
    the spawn, and only the far side has a process that existed, ran, and may
    have written to the plan before it was killed. A caller that records
    "certainly did not run" for that case is recording something it cannot
    know, so the flag travels with the refusal rather than being guessed from
    the reason.
    """

    def __init__(self, reason: str, message: str, *, spawned: bool = False) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.spawned = spawned


LAUNCH_REASON_INVALID_PLAN = "invalid-plan"
LAUNCH_REASON_INVALID_PROFILE = "invalid-profile"
LAUNCH_REASON_EMPTY_REPLY = "empty-reply"
LAUNCH_REASON_PROCESS_FAILED = "process-failed"


@dataclass(frozen=True)
class LaunchResult:
    """Process metadata returned by :func:`launch_plan`.

    The reply text is never stored on this dataclass — only its length when
    a reply was streamed, so callers can confirm the byte count was non-zero
    without holding a copy of the operator answer.
    """

    pid: int
    argv: tuple[str, ...]
    plan_dir: Path
    profile_payload: dict[str, Any]
    reply_provided: bool
    reply_length: int | None
    reply_origin: str | None
    state_dir: Path | None
    cwd: str | None = None
    workspace_root: str | None = None
    registry_digest: str | None = None
    repositories: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "argv": list(self.argv),
            "plan_dir": str(self.plan_dir),
            "profile": dict(self.profile_payload),
            "reply_provided": self.reply_provided,
            "reply_length": self.reply_length,
            "reply_origin": self.reply_origin,
            "state_dir": str(self.state_dir) if self.state_dir is not None else None,
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "registry_digest": self.registry_digest,
            "repositories": [dict(row) for row in self.repositories],
        }


def _validate_launch_plan_dir(plan_dir: str | Path) -> Path:
    try:
        canonical = plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"invalid plan directory {plan_dir!r}: {error}",
        ) from error
    if not canonical.is_dir():
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"plan directory does not exist: {canonical}",
        )
    if not (canonical / "manifest.md").is_file():
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"plan directory is missing manifest.md: {canonical}",
        )
    if not (canonical / "context.md").is_file():
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"plan directory is missing context.md: {canonical}",
        )
    return canonical


def _validate_launch_profile(profile: LaunchProfile | None) -> LaunchProfile:
    if profile is None:
        return LaunchProfile()
    if not isinstance(profile, LaunchProfile):
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "profile must be a LaunchProfile instance",
        )
    if profile.harness is not None and profile.harness not in harnesses.HARNESS_NAMES:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"unsupported harness {profile.harness!r}",
        )
    if (
        profile.review_harness is not None
        and profile.review_harness not in harnesses.HARNESS_NAMES
    ):
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"unsupported review_harness {profile.review_harness!r}",
        )
    if (
        profile.review_harness is not None
        and profile.harness is not None
        and profile.review_harness == profile.harness
    ):
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "review_harness must differ from harness",
        )
    if profile.review_harness_model is not None and profile.review_harness is None:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "review_harness_model requires review_harness",
        )
    if profile.commit_mode is not None and profile.commit_mode not in COMMIT_MODES:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"unsupported commit_mode {profile.commit_mode!r}",
        )
    if profile.timeout is not None and profile.timeout <= 0:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"timeout must be positive: {profile.timeout!r}",
        )
    if profile.max_review_fix_cycles is not None and profile.max_review_fix_cycles < 0:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "max_review_fix_cycles must be zero or a positive integer: "
            f"{profile.max_review_fix_cycles!r}",
        )
    return profile


def build_launch_argv(
    plan_dir: str | Path,
    *,
    profile: LaunchProfile | None = None,
    reply_via_stdin: bool = False,
    json_output: bool = True,
    state_dir: str | Path | None = None,
    python_executable: str | None = None,
    resume: bool = False,
) -> list[str]:
    """Return the argv for ``python -m agent_session_loop run ...``.

    The argv intentionally omits ``--reply``: reply text only ever flows
    through stdin via the matching ``--reply-stdin`` flag, so the operator
    answer never appears on the child process command line.

    ``resume`` adds ``--resume``, which makes the child rehydrate the harness,
    model, effort, and workspace binding this plan's ``run_config`` persisted.
    Explicit profile flags still win, so a caller can resume and override.
    """
    canonical = _validate_launch_plan_dir(plan_dir)
    validated = _validate_launch_profile(profile)
    validated, _workspace = _workspace_launch_profile(canonical, validated)
    interpreter = python_executable or sys.executable
    argv: list[str] = [interpreter, "-m", "agent_session_loop", "run", str(canonical)]
    if resume:
        argv.append("--resume")

    cwd_value = validated.cwd
    if cwd_value:
        argv.extend(["--cwd", cwd_value])
    if validated.harness is not None:
        argv.extend(["--harness", validated.harness])
    if validated.review_harness is not None:
        argv.extend(["--review-harness", validated.review_harness])
    if validated.harness_model is not None:
        argv.extend(["--harness-model", validated.harness_model])
    if validated.review_harness_model is not None:
        argv.extend(["--review-harness-model", validated.review_harness_model])
    if validated.effort is not None:
        argv.extend(["--effort", validated.effort])
    if validated.timeout is not None:
        argv.extend(["--timeout", str(validated.timeout)])
    if validated.max_review_fix_cycles is not None:
        argv.extend(["--max-review-fix-cycles", str(validated.max_review_fix_cycles)])
    if validated.commit_mode == "per-session":
        argv.append("--commit-per-session")
    elif validated.commit_mode == "end":
        argv.append("--commit-at-end")
    if validated.unsafe_bypass is False:
        argv.append("--no-unsafe-bypass")
    elif validated.unsafe_bypass is True:
        argv.append("--unsafe-bypass")
    if validated.allow_not_loop_ready is True:
        argv.append("--allow-not-loop-ready")
    if validated.note is not None:
        argv.extend(["--note", validated.note])
    if state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    if json_output:
        argv.append("--json")
    if reply_via_stdin:
        argv.append("--reply-stdin")
    return argv


def launch_plan(
    plan_dir: str | Path,
    *,
    profile: LaunchProfile | None = None,
    reply_text: str | None = None,
    state_dir: str | Path | None = None,
    json_output: bool = True,
    python_executable: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    popen_factory: Any = subprocess.Popen,
    resume: bool = False,
    isolate_locks: bool = False,
) -> LaunchResult:
    """Spawn a background ``python -m agent_session_loop run`` process.

    ``reply_text`` is streamed to the child's stdin when set so the operator
    answer never appears in argv, environment variables, or persisted state.
    The child still emits its normal ASL state, events, and artifacts.

    ``isolate_locks`` narrows the child's lock authorization to the plan being
    launched: it still takes that plan's lock and still refuses on an overlap,
    but it reclaims no other plan's stale lock. It defaults to false because an
    operator-driven launch is an ordinary start and wants the ordinary hygiene;
    a launcher authorized one plan at a time passes true, and the marker
    travels in the environment because the argv belongs to the resumed run.

    Validation errors raise :class:`LaunchError` before any subprocess is
    spawned, so callers can map them to immediate API errors.
    """
    canonical = _validate_launch_plan_dir(plan_dir)
    validated = _validate_launch_profile(profile)
    validated, workspace = _workspace_launch_profile(canonical, validated)
    reply_via_stdin = reply_text is not None
    reply_length: int | None = None
    if reply_via_stdin:
        assert reply_text is not None
        if not reply_text.strip():
            raise LaunchError(
                LAUNCH_REASON_EMPTY_REPLY,
                "reply text must contain non-whitespace content",
            )
        try:
            reply_text.encode("utf-8")
        except UnicodeEncodeError as error:
            # A lone surrogate cannot reach the child's stdin; refuse it here,
            # before a run exists, rather than after one has been spawned.
            raise LaunchError(
                LAUNCH_REASON_EMPTY_REPLY,
                f"reply text is not encodable as UTF-8: {error}",
            ) from error
        reply_length = len(reply_text)

    argv = build_launch_argv(
        canonical,
        profile=validated,
        reply_via_stdin=reply_via_stdin,
        json_output=json_output,
        state_dir=state_dir,
        python_executable=python_executable,
        resume=resume,
    )

    canonical_state_dir: Path | None = None
    if state_dir is not None:
        canonical_state_dir = Path(state_dir).expanduser().resolve()

    child_cwd = Path(cwd).expanduser().resolve() if cwd is not None else None
    # Every caller-provided entry is preserved; only the origin marker is the
    # launcher's to set, and it is materialized even when no mapping was passed
    # so an inherited environment still attributes the child to the Workbench.
    popen_env = cli_trace.child_env(
        os.environ if env is None else env, cli_trace.ORIGIN_WORKBENCH
    )
    if isolate_locks:
        popen_env = run_contention.isolated_child_env(popen_env)

    stdin_pipe = subprocess.PIPE if reply_via_stdin else subprocess.DEVNULL
    try:
        process = popen_factory(
            argv,
            stdin=stdin_pipe,
            stdout=stdout,
            stderr=stderr,
            env=popen_env,
            cwd=str(child_cwd) if child_cwd is not None else None,
            text=bool(reply_via_stdin),
            # The run outlives whoever started it. A Workbench server stopped
            # with Ctrl-C, a bridge whose connector went away, a terminal
            # closing -- each delivers its signal to a process *group*, and a
            # child in the launcher's group would be killed mid-turn by an
            # event that has nothing to do with the plan. Its own session is
            # what makes an operator's run survive the operator's tooling.
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        raise LaunchError(
            LAUNCH_REASON_PROCESS_FAILED,
            f"failed to spawn agent_session_loop run: {error}",
        ) from error

    if reply_via_stdin:
        assert reply_text is not None
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(reply_text)
            finally:
                process.stdin.close()
        except Exception as error:
            # Any failure to hand over the reply -- a closed pipe, a value the
            # stream cannot encode -- must not leave the child running alone.
            try:
                process.kill()
            finally:
                process.wait()
            raise LaunchError(
                LAUNCH_REASON_PROCESS_FAILED,
                f"failed to stream reply text to child stdin: {error}",
                # A child existed here, and killing it is not the same as never
                # having started it: it may have reached the plan first.
                spawned=True,
            ) from error

    return LaunchResult(
        pid=process.pid,
        argv=tuple(argv),
        plan_dir=canonical,
        profile_payload=validated.to_payload(),
        reply_provided=reply_via_stdin,
        reply_length=reply_length,
        reply_origin="stdin" if reply_via_stdin else None,
        state_dir=canonical_state_dir,
        cwd=validated.cwd,
        workspace_root=workspace.get("workspace_root"),
        registry_digest=workspace.get("registry_digest"),
        repositories=workbench_workspace._repository_rows(
            workspace.get("repositories")
        ),
    )


def resume_plan_with_reply(
    plan_dir: str | Path,
    *,
    reply_text: str,
    profile: LaunchProfile | None = None,
    state_dir: str | Path | None = None,
    json_output: bool = True,
    python_executable: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    popen_factory: Any = subprocess.Popen,
) -> LaunchResult:
    """Launch ``asl run --resume`` for a paused plan, streaming the reply to stdin.

    Resuming without ``--resume`` would start the paused plan under whichever
    defaults the launching process happens to carry rather than the harness,
    model, effort, and workspace binding the original run persisted.
    """
    from . import intervention

    return intervention.resume_paused_run(
        plan_dir,
        reply_text=reply_text,
        profile=profile,
        state_dir=state_dir,
        json_output=json_output,
        python_executable=python_executable,
        env=env,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        popen_factory=popen_factory,
    )
