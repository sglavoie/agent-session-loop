"""Focused portion of tests/workbench_acceptance.py."""

from .workbench_acceptance_common import *

# The stage-specific halves


def check_soak(document: Mapping[str, Any], floor: float, ceiling: float) -> None:
    """Staging's hour: long enough, in all three phases, inside the window."""
    where = f"{STAGE_STAGING}.soak"
    soak = _mapping(document, "soak", STAGE_STAGING)
    require_keys(soak, ("started_at", "ended_at", "phases"), where)
    started_at = _timestamp(soak, "started_at", where)
    ended_at = _timestamp(soak, "ended_at", where)
    _check_window(started_at, floor, ceiling, f"{where}.started_at")
    _check_window(ended_at, floor, ceiling, f"{where}.ended_at")
    if ended_at - started_at < SOAK_MINIMUM_SECONDS:
        raise EvidenceError(
            f"{where}: {ended_at - started_at:.0f}s is short of the "
            f"{SOAK_MINIMUM_SECONDS:.0f}s soak the stage owes"
        )
    phases = _mapping(soak, "phases", where)
    require_keys(phases, SOAK_PHASES, f"{where}.phases")
    for phase in SOAK_PHASES:
        seconds = phases.get(phase)
        if isinstance(seconds, bool) or not isinstance(seconds, int | float):
            raise EvidenceError(f"{where}.phases.{phase}: seconds are required")
        if seconds <= 0:
            raise EvidenceError(f"{where}.phases.{phase}: never happened")
    total = sum(float(phases[phase]) for phase in SOAK_PHASES)
    if total > (ended_at - started_at) + MAX_CLOCK_SKEW_SECONDS:
        raise EvidenceError(
            f"{where}.phases: the phases add up to more than the soak lasted"
        )


def check_phone(
    document: Mapping[str, Any],
    artifact: str,
    hosts: Mapping[str, str],
    floor: float,
    ceiling: float,
) -> None:
    """Production's human half: readiness first, then a confirmed answer.

    The order is the rule that matters. A confirmation that predates the
    readiness it was supposedly granted by is a confirmation of something the
    operator was never asked about, and an unanswered request is a ``pending``
    verdict rather than a missing field -- the plan asks for acceptance to stay
    pending in that case, and a refusal here is what keeps it pending.
    """
    where = f"{STAGE_PRODUCTION}.phone"
    phone = _mapping(document, "phone", STAGE_PRODUCTION)
    require_keys(phone, ("readiness", "confirmation"), where)

    readiness = _mapping(phone, "readiness", where)
    ready_where = f"{where}.readiness"
    require_keys(readiness, ("requested_at", "granted_at", "by"), ready_where)
    requested = _timestamp(readiness, "requested_at", ready_where)
    granted = _timestamp(readiness, "granted_at", ready_where)
    _check_window(requested, floor, ceiling, f"{ready_where}.requested_at")
    _check_window(granted, floor, ceiling, f"{ready_where}.granted_at")
    if granted < requested:
        raise EvidenceError(f"{ready_where}: granted before it was asked for")
    _matching(readiness, "by", _NAME, ready_where)

    confirmation = _mapping(phone, "confirmation", where)
    confirm_where = f"{where}.confirmation"
    require_keys(
        confirmation,
        (
            "requested_at",
            "answered_at",
            "verdict",
            "release",
            "kind",
            "host",
            "by",
            "checks",
        ),
        confirm_where,
    )
    kind = _string(confirmation, "kind", confirm_where)
    if kind != PROVENANCE_ACTUAL_HOST:
        raise EvidenceError(
            f"{confirm_where}.kind: {kind!r} is not {PROVENANCE_ACTUAL_HOST!r}; a "
            "phone acceptance is only ever recorded from the phone"
        )
    device = _matching(confirmation, "host", _HOSTNAME, confirm_where)
    folded = device.lower()
    for marker in FORBIDDEN_HOST_MARKERS:
        if marker in folded:
            raise EvidenceError(
                f"{confirm_where}.host: {device!r} names a fixture rather than a phone"
            )
    if device in hosts.values():
        raise EvidenceError(
            f"{confirm_where}.host: {device!r} is a deployed host; a phone "
            "acceptance confirmed from the Mac or the Pi is not a phone acceptance"
        )
    _matching(confirmation, "by", _NAME, confirm_where)

    verdict = _string(confirmation, "verdict", confirm_where)
    if verdict != VERDICT_ACCEPTED:
        raise EvidenceError(
            f"{confirm_where}.verdict: {verdict!r}; acceptance stays pending until "
            "the operator answers"
        )
    release = _matching(confirmation, "release", _DIGEST, confirm_where)
    if release != artifact:
        raise EvidenceError(
            f"{confirm_where}.release: the operator confirmed {release[:12]}, not "
            f"the release being accepted ({artifact[:12]})"
        )

    asked = _timestamp(confirmation, "requested_at", confirm_where)
    answered = _timestamp(confirmation, "answered_at", confirm_where)
    _check_window(asked, floor, ceiling, f"{confirm_where}.requested_at")
    _check_window(answered, floor, ceiling, f"{confirm_where}.answered_at")
    if asked < granted:
        raise EvidenceError(
            f"{confirm_where}.requested_at: the confirmation was asked for before "
            "the operator declared the phone ready"
        )
    if answered < asked:
        raise EvidenceError(
            f"{confirm_where}.answered_at: answered before it was asked"
        )

    checks = _mapping(confirmation, "checks", confirm_where)
    require_keys(checks, PHONE_CHECKS, f"{confirm_where}.checks")
    for name in PHONE_CHECKS:
        if checks.get(name) is not True:
            raise EvidenceError(f"{confirm_where}.checks.{name}: not confirmed")


def check_retirement_release(
    document: Mapping[str, Any], report_path: Path, repository: Path
) -> None:
    """Bind retirement to the deployment and phone evidence production proved."""
    production = read_document(report_path.with_name(f"{STAGE_PRODUCTION}.json"))
    require_keys(
        production,
        ("schema", "stage", "recorded_at", "release", "hosts", "checks", "phone"),
        STAGE_PRODUCTION,
    )
    check_sanitized(production, STAGE_PRODUCTION)
    if production.get("schema") != SCHEMA_VERSION:
        raise EvidenceError(
            "production.schema: retirement requires the accepted production schema"
        )
    if production.get("stage") != STAGE_PRODUCTION:
        raise EvidenceError(
            "production.stage: retirement requires the accepted production report"
        )
    retirement_release = _mapping(document, "release", STAGE_RETIREMENT)
    production_release = _mapping(production, "release", STAGE_PRODUCTION)
    require_keys(production_release, ("revision", "artifact"), "production.release")
    for field in ("revision", "artifact"):
        if retirement_release.get(field) != production_release.get(field):
            raise EvidenceError(
                f"retirement.release.{field}: does not match the accepted "
                "production deployment"
            )
    revision = _matching(
        production_release, "revision", _REVISION, "production.release"
    )
    artifact = _matching(production_release, "artifact", _DIGEST, "production.release")
    floor = commit_timestamp(repository, revision)
    ceiling = _timestamp(production, "recorded_at", STAGE_PRODUCTION)
    hosts = _mapping(production, "hosts", STAGE_PRODUCTION)
    require_keys(hosts, tuple(HOST_ROLES), "production.hosts")
    names: dict[str, str] = {}
    for role in HOST_ROLES:
        where = f"production.hosts.{role}"
        host = _mapping(hosts, role, "production.hosts")
        names[role] = check_provenance(host, role, where)
        running = _matching(host, "release", _DIGEST, where)
        if running != artifact:
            raise EvidenceError(
                f"{where}.release: does not match the accepted production deployment"
            )
        _check_window(
            _timestamp(host, "observed_at", where),
            floor,
            ceiling,
            f"{where}.observed_at",
        )
    if names["mac"] == names["pi"]:
        raise EvidenceError("production.hosts: one machine cannot prove two hosts")
    check_checks(production, STAGE_PRODUCTION, floor, ceiling)
    check_phone(production, artifact, names, floor, ceiling)


# ---------------------------------------------------------------------------
# The whole of one stage


def verify(
    stage: str, *, repository: Path | None = None, path: Path | None = None
) -> dict[str, Any]:
    """Read and accept one stage's evidence, or raise saying what is wrong."""
    root = repository or REPO_ROOT
    report_path = path or evidence_path(stage)
    document = read_document(report_path)
    check_common(document, stage, root)
    release = _mapping(document, "release", stage)
    revision = str(release["revision"])
    artifact = str(release["artifact"])
    floor = commit_timestamp(root, revision)
    ceiling = _timestamp(document, "recorded_at", stage)
    if stage == STAGE_STAGING:
        check_soak(document, floor, ceiling)
    elif stage == STAGE_PRODUCTION:
        hosts = _mapping(document, "hosts", stage)
        names = {role: str(_mapping(hosts, role, stage)["host"]) for role in HOST_ROLES}
        check_phone(document, artifact, names, floor, ceiling)
    elif stage == STAGE_RETIREMENT:
        check_retirement_release(document, report_path, root)
    return document


__all__ = ["argparse", "json", "math", "os", "re", "subprocess", "sys", "tempfile", "time", "unittest", "Mapping", "Sequence", "Path", "Any", "lifecycle", "REPO_ROOT", "SCHEMA_VERSION", "STAGE_STAGING", "STAGE_PRODUCTION", "STAGE_RETIREMENT", "STAGES", "EVIDENCE_RELATIVE_PATH", "ENV_EVIDENCE_DIR", "ENV_RELEASE_REVISION", "ENV_PI_CHECKOUT", "PI_REPOSITORY_RELATIVE_PATH", "MAX_DOCUMENT_BYTES", "MAX_STRING_LENGTH", "MAX_SEQUENCE_LENGTH", "MAX_CLOCK_SKEW_SECONDS", "SOAK_MINIMUM_SECONDS", "SOAK_PHASES", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "FORBIDDEN_HOST_MARKERS", "HOST_ROLES", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "PROVENANCE_ACTUAL_HOST", "STAGE_CHECKS", "PHONE_CHECKS", "STAGE_EXTRA_KEY", "EvidenceError", "locate_pi_repository", "evidence_directory", "evidence_path", "read_document", "_fold", "check_sanitized", "_check_sanitized_value", "_check_sanitized_string", "require_keys", "_string", "_matching", "_timestamp", "_mapping", "git", "accepted_revision", "rebuild_digest", "check_release_is_ancestor", "check_release_is_current", "check_release_for_stage", "commit_timestamp", "check_provenance", "check_common", "_check_window", "check_checks", "check_soak", "check_phone", "check_retirement_release", "verify"]  # fmt: skip
