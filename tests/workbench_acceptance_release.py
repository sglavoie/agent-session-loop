"""Focused portion of tests/workbench_acceptance.py."""

from .workbench_acceptance_sanitize import *

# ---------------------------------------------------------------------------
# Release identity


def git(repository: Path, *arguments: str) -> str:
    """One read-only git question, answered or refused."""
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:  # pragma: no cover - git absent is not a rule
        raise EvidenceError(f"git is unavailable: {error}") from error
    if completed.returncode != 0:
        raise EvidenceError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def accepted_revision(
    repository: Path, environ: Mapping[str, str] | None = None
) -> str:
    """The release under acceptance: ``HEAD``, unless a caller named another."""
    env = os.environ if environ is None else environ
    named = env.get(ENV_RELEASE_REVISION)
    if named:
        if not _REVISION.fullmatch(named):
            raise EvidenceError(f"{ENV_RELEASE_REVISION} is not a full commit revision")
        return named
    return git(repository, "rev-parse", "HEAD")


def rebuild_digest(repository: Path, revision: str) -> str:
    """Pack the named commit the way the installer would, and return its digest.

    This is the whole of the wrong-release check. A digest can be copied from
    another report or typed; bytes rebuilt from a commit cannot be, and the
    packing function is the one an install actually accepts an artifact
    through, so a digest that matches here is a digest that would install.
    """
    with tempfile.TemporaryDirectory(prefix="wb-acceptance-") as raw:
        artifact = Path(raw) / "release.zip"
        try:
            return lifecycle.pack(artifact, revision, repository=repository)
        except lifecycle.LifecycleError as error:
            raise EvidenceError(
                f"the release named by the evidence cannot be rebuilt: {error}"
            ) from error


def check_release_is_ancestor(repository: Path, revision: str, accepted: str) -> None:
    """Require a real commit on the accepted release's history."""
    try:
        git(repository, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    except EvidenceError as error:
        raise EvidenceError(
            f"the evidence accepts {revision[:12]}, which is not a commit in this "
            "repository"
        ) from error
    if revision != accepted:
        merge_base = git(repository, "merge-base", revision, accepted)
        if merge_base != revision:
            raise EvidenceError(
                f"the evidence accepts {revision[:12]}, which is not an ancestor of "
                f"the release under acceptance ({accepted[:12]})"
            )


def check_release_is_current(repository: Path, revision: str, accepted: str) -> None:
    """Refuse a release that is not the one this repository would deploy now.

    Three questions, and they fail differently on purpose. A revision that is
    not a commit here names a release nobody can produce. A revision that is not
    an ancestor of the accepted one is evidence about something else entirely.
    A revision that *is* an ancestor but has package commits after it is
    evidence about a release the tree has since moved past -- stale, which is
    the failure the calendar would have been used to detect and detects worse.

    Existence is asked first so a typed or invented revision is reported as
    what it is, rather than as whatever the ancestry question says about a
    commit that is not there.
    """
    check_release_is_ancestor(repository, revision, accepted)
    changed = git(
        repository,
        "rev-list",
        "--count",
        f"{revision}..{accepted}",
        "--",
        lifecycle.PACKAGE_DIRECTORY,
    )
    if changed != "0":
        raise EvidenceError(
            f"the evidence is stale: {changed} commit(s) changed "
            f"{lifecycle.PACKAGE_DIRECTORY}/ after {revision[:12]}, so what was "
            "deployed is no longer what this repository holds"
        )


def check_release_for_stage(
    repository: Path, revision: str, accepted: str, stage: str
) -> None:
    """Apply the release-currency rule appropriate to one deployment stage.

    A staging rehearsal proves what happened to the release it names. Later
    package work does not invalidate that historical fact, but the commit must
    still belong to the accepted history. Production and retirement instead
    assert what is deployed now, so their evidence must remain current.
    """
    if stage == STAGE_STAGING:
        check_release_is_ancestor(repository, revision, accepted)
    else:
        check_release_is_current(repository, revision, accepted)


def commit_timestamp(repository: Path, revision: str) -> float:
    """When the accepted commit was made, which is the floor for every fact."""
    return float(git(repository, "show", "-s", "--format=%ct", revision))


__all__ = ["argparse", "json", "math", "os", "re", "subprocess", "sys", "tempfile", "time", "unittest", "Mapping", "Sequence", "Path", "Any", "lifecycle", "REPO_ROOT", "SCHEMA_VERSION", "STAGE_STAGING", "STAGE_PRODUCTION", "STAGE_RETIREMENT", "STAGES", "EVIDENCE_RELATIVE_PATH", "ENV_EVIDENCE_DIR", "ENV_RELEASE_REVISION", "ENV_PI_CHECKOUT", "PI_REPOSITORY_RELATIVE_PATH", "MAX_DOCUMENT_BYTES", "MAX_STRING_LENGTH", "MAX_SEQUENCE_LENGTH", "MAX_CLOCK_SKEW_SECONDS", "SOAK_MINIMUM_SECONDS", "SOAK_PHASES", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "FORBIDDEN_HOST_MARKERS", "HOST_ROLES", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "PROVENANCE_ACTUAL_HOST", "STAGE_CHECKS", "PHONE_CHECKS", "STAGE_EXTRA_KEY", "EvidenceError", "locate_pi_repository", "evidence_directory", "evidence_path", "read_document", "_fold", "check_sanitized", "_check_sanitized_value", "_check_sanitized_string", "require_keys", "_string", "_matching", "_timestamp", "_mapping", "git", "accepted_revision", "rebuild_digest", "check_release_is_ancestor", "check_release_is_current", "check_release_for_stage", "commit_timestamp"]  # fmt: skip
