"""Focused portion of tests/workbench_acceptance.py."""

from .workbench_acceptance_release import *

# ---------------------------------------------------------------------------
# The stage-independent half of a report


def check_provenance(host: Mapping[str, Any], role: str, where: str) -> str:
    """One host block: a real machine, of the right kind, named once."""
    require_keys(host, ("kind", "host", "system", "release", "observed_at"), where)
    kind = _string(host, "kind", where)
    if kind != PROVENANCE_ACTUAL_HOST:
        raise EvidenceError(
            f"{_fold(where, 'kind')}: {kind!r} is not {PROVENANCE_ACTUAL_HOST!r}; "
            "acceptance is only ever recorded from the machine it happened on"
        )
    name = _matching(host, "host", _HOSTNAME, where)
    folded = name.lower()
    for marker in FORBIDDEN_HOST_MARKERS:
        if marker in folded:
            raise EvidenceError(
                f"{_fold(where, 'host')}: {name!r} names a fixture rather than a "
                "machine"
            )
    system = _string(host, "system", where)
    if system != HOST_ROLES[role]:
        raise EvidenceError(
            f"{_fold(where, 'system')}: the {role} runs {HOST_ROLES[role]}, "
            f"not {system}"
        )
    return name


def check_common(document: Mapping[str, Any], stage: str, repository: Path) -> None:
    """Everything the three stages answer alike: identity, release, hosts."""
    extra = STAGE_EXTRA_KEY[stage]
    expected = ["schema", "stage", "recorded_at", "release", "hosts", "checks"]
    if extra:
        expected.append(extra)
    require_keys(document, expected, stage)
    check_sanitized(document, stage)

    if document.get("schema") != SCHEMA_VERSION:
        raise EvidenceError(
            f"{stage}.schema: this reader accepts schema {SCHEMA_VERSION}"
        )
    if document.get("stage") != stage:
        raise EvidenceError(
            f"{stage}.stage: the report names {document.get('stage')!r}; a report "
            "read as one stage may not describe another"
        )

    release = _mapping(document, "release", stage)
    require_keys(release, ("revision", "artifact"), f"{stage}.release")
    revision = _matching(release, "revision", _REVISION, f"{stage}.release")
    artifact = _matching(release, "artifact", _DIGEST, f"{stage}.release")

    accepted = accepted_revision(repository)
    check_release_for_stage(repository, revision, accepted, stage)
    rebuilt = rebuild_digest(repository, revision)
    if rebuilt != artifact:
        raise EvidenceError(
            f"{stage}.release.artifact: {revision[:12]} packs to {rebuilt[:12]}, "
            f"not {artifact[:12]}; this evidence is about another release"
        )

    recorded_at = _timestamp(document, "recorded_at", stage)
    committed_at = commit_timestamp(repository, revision)
    if recorded_at < committed_at:
        raise EvidenceError(
            f"{stage}.recorded_at: the report predates the release it accepts"
        )
    if recorded_at > time.time() + MAX_CLOCK_SKEW_SECONDS:
        raise EvidenceError(f"{stage}.recorded_at: the report is dated in the future")

    hosts = _mapping(document, "hosts", stage)
    require_keys(hosts, tuple(HOST_ROLES), f"{stage}.hosts")
    names: dict[str, str] = {}
    for role in HOST_ROLES:
        where = f"{stage}.hosts.{role}"
        host = _mapping(hosts, role, f"{stage}.hosts")
        names[role] = check_provenance(host, role, where)
        running = _matching(host, "release", _DIGEST, where)
        if running != artifact:
            raise EvidenceError(
                f"{where}.release: this host ran {running[:12]}, not the release "
                f"being accepted ({artifact[:12]})"
            )
        observed_at = _timestamp(host, "observed_at", where)
        _check_window(observed_at, committed_at, recorded_at, f"{where}.observed_at")
    if names["mac"] == names["pi"]:
        raise EvidenceError(
            f"{stage}.hosts: both halves name {names['mac']!r}; a two-host "
            "deployment recorded from one machine is not a two-host deployment"
        )

    check_checks(document, stage, committed_at, recorded_at)


def _check_window(value: float, floor: float, ceiling: float, where: str) -> None:
    if value < floor:
        raise EvidenceError(f"{where}: predates the release it is evidence for")
    if value > ceiling + MAX_CLOCK_SKEW_SECONDS:
        raise EvidenceError(f"{where}: is later than the report that carries it")


def check_checks(
    document: Mapping[str, Any], stage: str, floor: float, ceiling: float
) -> None:
    """Every check that stage owes, each passed, each observed in the window."""
    checks = _mapping(document, "checks", stage)
    require_keys(checks, STAGE_CHECKS[stage], f"{stage}.checks")
    for name in STAGE_CHECKS[stage]:
        where = f"{stage}.checks.{name}"
        entry = _mapping(checks, name, f"{stage}.checks")
        require_keys(entry, ("outcome", "observed_at", "host"), where)
        outcome = _string(entry, "outcome", where)
        if outcome != OUTCOME_PASSED:
            raise EvidenceError(f"{where}: recorded as {outcome!r}")
        host = _string(entry, "host", where)
        if host not in (*HOST_ROLES, "both"):
            raise EvidenceError(f"{where}.host: {host!r} is not a deployed host")
        _check_window(
            _timestamp(entry, "observed_at", where),
            floor,
            ceiling,
            f"{where}.observed_at",
        )


# ---------------------------------------------------------------------------

__all__ = ["argparse", "json", "math", "os", "re", "subprocess", "sys", "tempfile", "time", "unittest", "Mapping", "Sequence", "Path", "Any", "lifecycle", "REPO_ROOT", "SCHEMA_VERSION", "STAGE_STAGING", "STAGE_PRODUCTION", "STAGE_RETIREMENT", "STAGES", "EVIDENCE_RELATIVE_PATH", "ENV_EVIDENCE_DIR", "ENV_RELEASE_REVISION", "ENV_PI_CHECKOUT", "PI_REPOSITORY_RELATIVE_PATH", "MAX_DOCUMENT_BYTES", "MAX_STRING_LENGTH", "MAX_SEQUENCE_LENGTH", "MAX_CLOCK_SKEW_SECONDS", "SOAK_MINIMUM_SECONDS", "SOAK_PHASES", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "FORBIDDEN_HOST_MARKERS", "HOST_ROLES", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "PROVENANCE_ACTUAL_HOST", "STAGE_CHECKS", "PHONE_CHECKS", "STAGE_EXTRA_KEY", "EvidenceError", "locate_pi_repository", "evidence_directory", "evidence_path", "read_document", "_fold", "check_sanitized", "_check_sanitized_value", "_check_sanitized_string", "require_keys", "_string", "_matching", "_timestamp", "_mapping", "git", "accepted_revision", "rebuild_digest", "check_release_is_ancestor", "check_release_is_current", "check_release_for_stage", "commit_timestamp", "check_provenance", "check_common", "_check_window", "check_checks"]  # fmt: skip
