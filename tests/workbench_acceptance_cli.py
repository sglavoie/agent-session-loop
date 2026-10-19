"""Focused portion of tests/workbench_acceptance.py."""

from .workbench_acceptance_stages import *

# ---------------------------------------------------------------------------
# The entry points the deployment contracts name


class _EvidenceCase:
    """Shared body of the three gates. Not a test case on its own.

    One assertion per rejection class rather than one that reads everything,
    because the failure a deployment session gets back is the only instruction
    it has, and "missing production.checks.single-publisher" is a different
    instruction from "this evidence is stale".
    """

    stage = ""

    def _path(self) -> Path:
        return evidence_path(self.stage)

    def _document(self) -> dict[str, Any]:
        return read_document(self._path())

    def test_the_report_exists_and_is_a_sanitized_object(self) -> None:
        document = self._document()
        check_sanitized(document, self.stage)

    def test_the_report_accepts_an_authentic_release_for_this_stage(self) -> None:
        document = self._document()
        release = _mapping(document, "release", self.stage)
        revision = _matching(release, "revision", _REVISION, f"{self.stage}.release")
        artifact = _matching(release, "artifact", _DIGEST, f"{self.stage}.release")
        accepted = accepted_revision(REPO_ROOT)
        check_release_for_stage(REPO_ROOT, revision, accepted, self.stage)
        if self.stage == STAGE_RETIREMENT:
            check_retirement_release(document, self._path(), REPO_ROOT)
        rebuilt = rebuild_digest(REPO_ROOT, revision)
        if rebuilt != artifact:
            raise EvidenceError(
                f"{self.stage}.release.artifact: {revision[:12]} packs to "
                f"{rebuilt[:12]}, not {artifact[:12]}"
            )

    def test_the_report_was_recorded_on_the_actual_hosts(self) -> None:
        document = self._document()
        hosts = _mapping(document, "hosts", self.stage)
        require_keys(hosts, tuple(HOST_ROLES), f"{self.stage}.hosts")
        for role in HOST_ROLES:
            check_provenance(
                _mapping(hosts, role, f"{self.stage}.hosts"),
                role,
                f"{self.stage}.hosts.{role}",
            )

    def test_every_check_this_stage_owes_passed(self) -> None:
        verify(self.stage)


class StagingEvidence(_EvidenceCase, unittest.TestCase):
    """Session 22's gate: the staging rehearsal on the two real hosts."""

    stage = STAGE_STAGING

    def test_the_soak_ran_for_a_full_hour_in_all_three_phases(self) -> None:
        document = self._document()
        release = _mapping(document, "release", self.stage)
        floor = commit_timestamp(REPO_ROOT, str(release["revision"]))
        check_soak(document, floor, _timestamp(document, "recorded_at", self.stage))


class ProductionEvidence(_EvidenceCase, unittest.TestCase):
    """Session 23's gate: the cutover, plus the operator's own confirmation."""

    stage = STAGE_PRODUCTION

    def test_the_operator_declared_the_phone_ready_and_then_confirmed(self) -> None:
        document = self._document()
        release = _mapping(document, "release", self.stage)
        hosts = _mapping(document, "hosts", self.stage)
        names = {
            role: str(_mapping(hosts, role, self.stage)["host"]) for role in HOST_ROLES
        }
        check_phone(
            document,
            str(release["artifact"]),
            names,
            commit_timestamp(REPO_ROOT, str(release["revision"])),
            _timestamp(document, "recorded_at", self.stage),
        )


class RetirementEvidence(_EvidenceCase, unittest.TestCase):
    """Session 24's gate: one frontend, one owner, one deployed application."""

    stage = STAGE_RETIREMENT


# ---------------------------------------------------------------------------
# The skeleton a deployment session fills in


def template(stage: str) -> dict[str, Any]:
    """A report with every field this reader requires, and no values.

    Printed rather than written: the session that deploys is the one that knows
    the hostnames, the digests and the times, and a file this module wrote
    would be a file this module then verified against itself.
    """
    if stage not in STAGES:
        raise EvidenceError(f"unknown acceptance stage: {stage}")
    document: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "stage": stage,
        "recorded_at": 0,
        "release": {"revision": "<40-hex commit>", "artifact": "<64-hex sha256>"},
        "hosts": {
            role: {
                "kind": PROVENANCE_ACTUAL_HOST,
                "host": f"<{role} hostname>",
                "system": system,
                "release": "<64-hex sha256>",
                "observed_at": 0,
            }
            for role, system in HOST_ROLES.items()
        },
        "checks": {
            name: {"outcome": OUTCOME_PASSED, "observed_at": 0, "host": "both"}
            for name in STAGE_CHECKS[stage]
        },
    }
    if stage == STAGE_STAGING:
        document["soak"] = {
            "started_at": 0,
            "ended_at": 0,
            "phases": dict.fromkeys(SOAK_PHASES, 0),
        }
    elif stage == STAGE_PRODUCTION:
        document["phone"] = {
            "readiness": {"requested_at": 0, "granted_at": 0, "by": "owner"},
            "confirmation": {
                "requested_at": 0,
                "answered_at": 0,
                "verdict": VERDICT_ACCEPTED,
                "release": "<64-hex sha256>",
                "kind": PROVENANCE_ACTUAL_HOST,
                "host": "<phone hostname>",
                "by": "owner",
                "checks": dict.fromkeys(PHONE_CHECKS, True),
            },
        }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m tests.workbench_acceptance",
        description="Read or template the deployment acceptance evidence.",
    )
    parser.add_argument("--template", choices=STAGES, help="print an empty report")
    parser.add_argument("--verify", choices=STAGES, help="read one stage's report")
    arguments = parser.parse_args(argv)
    if arguments.template:
        print(json.dumps(template(arguments.template), indent=2, sort_keys=True))
        return 0
    if arguments.verify:
        try:
            verify(arguments.verify)
        except EvidenceError as error:
            print(f"refused: {error}")
            return 1
        print(f"{arguments.verify}: accepted")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover - a convenience entry point
    sys.exit(main())

__all__ = ["argparse", "json", "math", "os", "re", "subprocess", "sys", "tempfile", "time", "unittest", "Mapping", "Sequence", "Path", "Any", "lifecycle", "REPO_ROOT", "SCHEMA_VERSION", "STAGE_STAGING", "STAGE_PRODUCTION", "STAGE_RETIREMENT", "STAGES", "EVIDENCE_RELATIVE_PATH", "ENV_EVIDENCE_DIR", "ENV_RELEASE_REVISION", "ENV_PI_CHECKOUT", "PI_REPOSITORY_RELATIVE_PATH", "MAX_DOCUMENT_BYTES", "MAX_STRING_LENGTH", "MAX_SEQUENCE_LENGTH", "MAX_CLOCK_SKEW_SECONDS", "SOAK_MINIMUM_SECONDS", "SOAK_PHASES", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "FORBIDDEN_HOST_MARKERS", "HOST_ROLES", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "PROVENANCE_ACTUAL_HOST", "STAGE_CHECKS", "PHONE_CHECKS", "STAGE_EXTRA_KEY", "EvidenceError", "locate_pi_repository", "evidence_directory", "evidence_path", "read_document", "_fold", "check_sanitized", "_check_sanitized_value", "_check_sanitized_string", "require_keys", "_string", "_matching", "_timestamp", "_mapping", "git", "accepted_revision", "rebuild_digest", "check_release_is_ancestor", "check_release_is_current", "check_release_for_stage", "commit_timestamp", "check_provenance", "check_common", "_check_window", "check_checks", "check_soak", "check_phone", "check_retirement_release", "verify", "_EvidenceCase", "StagingEvidence", "ProductionEvidence", "RetirementEvidence", "template", "main"]  # fmt: skip
