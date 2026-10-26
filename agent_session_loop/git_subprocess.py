"""The one place the package builds and spawns a ``git`` subprocess.

Every caller wants the same invocation -- captured output, ``check=False``, the
exit status left for the caller to read -- and differs only in what it does
when the process cannot be spawned at all. That difference is the caller's
contract, so it stays a parameter rather than a copy:

* :func:`run_git` lets :class:`OSError` (and the :class:`ValueError` an argument
  holding a NUL byte raises) propagate, for a caller that maps the failure to
  ``None`` or a value of its own.
* :func:`run_git_or_raise` converts it into the caller's error class, so a
  boundary that promises "no raw ``OSError`` escapes" keeps that promise with
  its own exception type.
* :func:`run_git_reporting` folds it into a synthesized
  :class:`subprocess.CompletedProcess`, for a caller that treats a failed spawn
  as one more non-zero exit.

Two invocation shapes exist and are not interchangeable: ``git -C <cwd>``
reports a missing directory as git's own non-zero exit, while ``chdir=True``
spawns git *in* ``cwd`` and a missing directory raises before git runs. Each
caller keeps the shape it had.

Every invocation is bounded by ``timeout`` (:data:`GIT_TIMEOUT_SECONDS` unless
the caller passes its own, or ``None`` for none). A git that outlives it -- a
hung credential helper, a wedged network mount, a hook that never returns --
is killed and reported as :class:`GitTimeoutError`, an :class:`OSError`, so it
takes exactly the path a git that could not be spawned takes in each of the
three shapes above rather than hanging the run that asked.

This module imports nothing from the package, so any module may depend on it.
It holds no test override either: :mod:`agent_session_loop.repository_evidence`
layers its own context-variable seam over :func:`run_git_or_raise`.
"""

from __future__ import annotations

import errno
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, overload

__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "GitTimeoutError",
    "run_git",
    "run_git_or_raise",
    "run_git_reporting",
]

#: The default ceiling on one git invocation. Generous on purpose: it exists to
#: turn a git that will never return into an error, not to police a slow one.
GIT_TIMEOUT_SECONDS = 300.0


class GitTimeoutError(OSError):
    """A git invocation ran past its timeout and was killed.

    An :class:`OSError` (``errno.ETIMEDOUT``) so every caller that already
    treats "git could not be run" as a handled outcome handles this one too.
    """


def _argv(cwd: Path | str, args: Sequence[str], *, chdir: bool) -> list[str]:
    if chdir:
        return ["git", *args]
    return ["git", "-C", str(cwd), *args]


@overload
def run_git(
    cwd: Path | str,
    args: Sequence[str],
    *,
    decode: Literal[True] = True,
    chdir: bool = False,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run_git(
    cwd: Path | str,
    args: Sequence[str],
    *,
    decode: Literal[False],
    chdir: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]: ...


def run_git(
    cwd: Path | str,
    args: Sequence[str],
    *,
    decode: bool = True,
    chdir: bool = False,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run ``git`` once with captured output; a failed spawn propagates.

    ``decode=False`` keeps stdout and stderr as bytes, because text mode
    translates ``\\r\\n`` and a lone ``\\r`` to ``\\n``. ``env`` overlays the
    ambient environment rather than replacing it. A git still running after
    ``timeout`` seconds is killed and raises :class:`GitTimeoutError`.

    Decoded output is UTF-8 with ``surrogateescape``: a filename that is not
    UTF-8 round-trips through the surrogates instead of raising a
    :class:`UnicodeDecodeError` that no ``RunError`` handler would catch.
    """
    try:
        return subprocess.run(
            _argv(cwd, args, chdir=chdir),
            cwd=cwd if chdir else None,
            text=decode,
            encoding="utf-8" if decode else None,
            errors="surrogateescape" if decode else None,
            input=stdin_text,
            capture_output=True,
            check=False,
            env=None if env is None else {**os.environ, **env},
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError(
            errno.ETIMEDOUT,
            f"git {' '.join(args)} did not finish within {timeout:g}s",
        ) from exc


@overload
def run_git_or_raise(
    cwd: Path | str,
    args: Sequence[str],
    *,
    error: Callable[[str], Exception],
    decode: Literal[True] = True,
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run_git_or_raise(
    cwd: Path | str,
    args: Sequence[str],
    *,
    error: Callable[[str], Exception],
    decode: Literal[False],
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]: ...


def run_git_or_raise(
    cwd: Path | str,
    args: Sequence[str],
    *,
    error: Callable[[str], Exception],
    decode: bool = True,
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run ``git -C cwd``; a failed spawn or a timeout raises ``error(message)``."""
    try:
        if decode:
            return run_git(cwd, args, timeout=timeout)
        return run_git(cwd, args, decode=False, timeout=timeout)
    except FileNotFoundError as exc:
        raise error("git executable not found on PATH") from exc
    except OSError as exc:
        raise error(f"could not run git {' '.join(args)}: {exc}") from exc


def run_git_reporting(
    cwd: Path | str,
    args: Sequence[str],
    *,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
    missing_returncode: int = 127,
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``git`` in ``cwd``; a failed spawn becomes a non-zero result.

    A missing executable (or directory) reports ``missing_returncode``, any
    other :class:`OSError` -- a timeout included -- reports ``1``; stderr
    carries the error text.
    """
    try:
        return run_git(
            cwd, args, chdir=True, stdin_text=stdin_text, env=env, timeout=timeout
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            ["git", *args], missing_returncode, stdout="", stderr=str(exc)
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            ["git", *args], 1, stdout="", stderr=str(exc)
        )
