"""Prove the deployment gate refuses what it claims to refuse.

``tests/workbench_acceptance.py`` is the one module in this tree whose verdict
depends on a file a human-supervised session writes rather than on a fixture, so
it is also the one module no other suite can exercise: until session 22 has
deployed, the report it reads does not exist. That is exactly the condition
under which a gate quietly stops gating, which is why its rules are proved here
instead -- against synthetic repositories and hand-built documents, where every
rejection can be made to fire on purpose.

The shape follows ``tests/test_asl_harness_compatibility.py``, which does the
same job for ``tests/asl_live_redirect_acceptance.py``: a non-discovered module
that only runs when the thing it accepts exists, and a discovered test beside it
that keeps the module honest in the meantime.

The synthetic repository is a real ``git`` repository holding a minimal
``asl_workbench`` package, because the wrong-release rule is enforced by
rebuilding the artifact with :func:`asl_workbench.lifecycle.pack` and the stale
rule by reading commit history. Neither can be proven against a stub: a fake
digest would agree with a fake packer.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from unittest import mock

from tests import run_suite, workbench_acceptance_contract
from tests import workbench_acceptance as acceptance

#: A minimal package that satisfies the artifact's required members and the
#: portable boundary: stdlib-free module bodies, one static asset, nothing else.
PACKAGE_SOURCES: Mapping[str, str] = {
    f"{name}.py": f'"""Synthetic {name}."""\n\n\ndef {name}_entry() -> None:\n'
    "    return None\n"
    for name in ("lifecycle", "runtime", "server", "http", "store_workbench")
} | {"__init__.py": '"""A synthetic Workbench package."""\n\nVERSION = "0"\n'}


@dataclass(frozen=True)
class Fixture:
    """One synthetic repository, its four commits, and their two digests.

    The commit graph is the whole point of the staleness rules, so it is built
    once and named:

    ``package`` → ``docs`` → ``repackaged`` is the trunk, with ``repackaged``
    at ``HEAD``, and ``branch`` hangs off ``package`` without ever being
    merged. That gives a fresh release (``repackaged``, which is ``HEAD``), a
    stale one (``package``, with a package commit after it), an ancestor whose
    package never changed (``package`` accepted at ``docs``), and a revision
    that is not an ancestor of anything under acceptance (``branch``).
    """

    root: Path
    package: str
    docs: str
    repackaged: str
    branch: str
    digest_package: str
    digest_repackaged: str
    committed_at: int
    now: int


_FIXTURE: Fixture | None = None
_WORKSPACE: tempfile.TemporaryDirectory[str] | None = None


def _git(root: Path, *arguments: str, at: int | None = None) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Acceptance Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@invalid",
            "GIT_COMMITTER_NAME": "Acceptance Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@invalid",
            "GIT_CONFIG_GLOBAL": str(root / ".gitconfig-absent"),
            "GIT_CONFIG_SYSTEM": str(root / ".gitconfig-absent"),
        }
    )
    if at is not None:
        stamp = f"{at} +0000"
        environment["GIT_AUTHOR_DATE"] = stamp
        environment["GIT_COMMITTER_DATE"] = stamp
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(root: Path, message: str, at: int) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message, at=at)
    return _git(root, "rev-parse", "HEAD")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def setUpModule() -> None:
    """Build the synthetic repository once; every case reads the same graph."""
    global _FIXTURE, _WORKSPACE
    _WORKSPACE = tempfile.TemporaryDirectory(prefix="wb-acceptance-fixture-")
    root = Path(_WORKSPACE.name) / "scripts"
    package = root / "asl_workbench"
    for name, source in PACKAGE_SOURCES.items():
        _write(package / name, source)
    _write(package / "static" / "index.html", "<!doctype html>\n<title>wb</title>\n")
    _write(root / "README.md", "first\n")

    now = int(time.time())
    _git(root, "init", "-b", "main")
    first = _commit(root, "package", now - 10800)
    _write(root / "README.md", "second\n")
    second = _commit(root, "docs", now - 9000)
    _write(
        package / "store_workbench.py",
        PACKAGE_SOURCES["store_workbench.py"] + "\nLIMIT = 1\n",
    )
    third = _commit(root, "repackage", now - 7200)

    _git(root, "checkout", "-q", "-b", "side", first)
    _write(root / "README.md", "side\n")
    side = _commit(root, "side docs", now - 8000)
    _git(root, "checkout", "-q", "main")

    _FIXTURE = Fixture(
        root=root,
        package=first,
        docs=second,
        repackaged=third,
        branch=side,
        digest_package=acceptance.rebuild_digest(root, first),
        digest_repackaged=acceptance.rebuild_digest(root, third),
        committed_at=int(_git(root, "show", "-s", "--format=%ct", third)),
        now=now,
    )


def tearDownModule() -> None:
    global _FIXTURE, _WORKSPACE
    _FIXTURE = None
    if _WORKSPACE is not None:
        _WORKSPACE.cleanup()
        _WORKSPACE = None


def fixture() -> Fixture:
    assert _FIXTURE is not None, "the synthetic repository was not built"
    return _FIXTURE


# ---------------------------------------------------------------------------
# Document builders. Each starts from a report that is accepted, so a case that
# expects a refusal changes exactly one thing and the message names that thing.


def valid_document(stage: str) -> dict[str, Any]:
    """A report for ``stage`` that the gate accepts against the fixture."""
    fix = fixture()
    recorded_at = fix.now - 60
    observed_at = fix.now - 3600
    document: dict[str, Any] = {
        "schema": acceptance.SCHEMA_VERSION,
        "stage": stage,
        "recorded_at": recorded_at,
        "release": {
            "revision": fix.repackaged,
            "artifact": fix.digest_repackaged,
        },
        "hosts": {
            "mac": {
                "kind": "actual-host",
                "host": "studio-mini",
                "system": "Darwin",
                "release": fix.digest_repackaged,
                "observed_at": observed_at,
            },
            "pi": {
                "kind": "actual-host",
                "host": "shed-pi",
                "system": "Linux",
                "release": fix.digest_repackaged,
                "observed_at": observed_at,
            },
        },
        "checks": {
            name: {"outcome": "passed", "observed_at": observed_at, "host": "both"}
            for name in acceptance.STAGE_CHECKS[stage]
        },
    }
    if stage == acceptance.STAGE_STAGING:
        document["soak"] = {
            "started_at": fix.now - 7000,
            "ended_at": fix.now - 3000,
            "phases": {"active": 2000, "idle": 1500, "reconnect": 400},
        }
    elif stage == acceptance.STAGE_PRODUCTION:
        document["phone"] = {
            "readiness": {
                "requested_at": fix.now - 6000,
                "granted_at": fix.now - 5900,
                "by": "owner",
            },
            "confirmation": {
                "requested_at": fix.now - 5000,
                "answered_at": fix.now - 4900,
                "verdict": "accepted",
                "release": fix.digest_repackaged,
                "kind": "actual-host",
                "host": "operator-phone",
                "by": "owner",
                "checks": dict.fromkeys(acceptance.PHONE_CHECKS, True),
            },
        }
    return document


def _walk(document: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    parts = path.split(".")
    cursor: Any = document
    for part in parts[:-1]:
        cursor = cursor[part]
    return cursor, parts[-1]


def edited(stage: str, path: str, value: Any) -> dict[str, Any]:
    """A valid report with one dotted field replaced."""
    document = valid_document(stage)
    parent, leaf = _walk(document, path)
    parent[leaf] = value
    return document


def without(stage: str, path: str) -> dict[str, Any]:
    """A valid report with one dotted field removed."""
    document = valid_document(stage)
    parent, leaf = _walk(document, path)
    del parent[leaf]
    return document


class AcceptanceCase(unittest.TestCase):
    """Shared plumbing: write a document, read it through the real gate."""

    def written(self, stage: str, document: Mapping[str, Any]) -> Path:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / f"{stage}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        if stage == acceptance.STAGE_RETIREMENT:
            production = valid_document(acceptance.STAGE_PRODUCTION)
            production["release"] = dict(document["release"])
            for host in production["hosts"].values():
                host["release"] = document["release"]["artifact"]
            production["phone"]["confirmation"]["release"] = document["release"][
                "artifact"
            ]
            (directory / "production.json").write_text(
                json.dumps(production), encoding="utf-8"
            )
        return path

    def accepts(self, stage: str, document: Mapping[str, Any]) -> None:
        acceptance.verify(
            stage,
            repository=fixture().root,
            path=self.written(stage, document),
        )

    def refuses(self, stage: str, document: Mapping[str, Any], because: str) -> None:
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                stage,
                repository=fixture().root,
                path=self.written(stage, document),
            )
        self.assertIn(because, str(caught.exception))


class AcceptedEvidence(AcceptanceCase):
    """The positive case, so every refusal below is about the change it made."""

    def test_each_stage_has_a_report_the_gate_accepts(self) -> None:
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                self.accepts(stage, valid_document(stage))

    def test_an_earlier_release_whose_package_never_changed_is_still_current(
        self,
    ) -> None:
        # Staleness is about the package, not about the calendar: a commit
        # behind HEAD that only moved documentation deployed the same software.
        fix = fixture()
        document = valid_document(acceptance.STAGE_RETIREMENT)
        document["release"] = {
            "revision": fix.package,
            "artifact": fix.digest_package,
        }
        for host in document["hosts"].values():
            host["release"] = fix.digest_package
        with mock.patch.dict(os.environ, {acceptance.ENV_RELEASE_REVISION: fix.docs}):
            self.accepts(acceptance.STAGE_RETIREMENT, document)

    def test_a_staging_rehearsal_remains_valid_after_package_changes(self) -> None:
        # Staging records a completed rehearsal, not the current production
        # deployment. A later package commit cannot undo what was exercised.
        fix = fixture()
        document = valid_document(acceptance.STAGE_STAGING)
        document["release"] = {
            "revision": fix.package,
            "artifact": fix.digest_package,
        }
        for host in document["hosts"].values():
            host["release"] = fix.digest_package
        self.accepts(acceptance.STAGE_STAGING, document)

    def test_the_template_carries_every_field_the_gate_requires(self) -> None:
        # What a deployment session is handed has to be what the gate reads, or
        # the session finds out field by field from failures.
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(
                    set(acceptance.template(stage)), set(valid_document(stage))
                )
                self.assertEqual(
                    set(acceptance.template(stage)["checks"]),
                    set(acceptance.STAGE_CHECKS[stage]),
                )


class MissingEvidence(AcceptanceCase):
    """ "We could not check" must never be spelled the way "it passed" is."""

    def test_a_report_that_was_never_written_is_refused(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                with self.assertRaises(acceptance.EvidenceError) as caught:
                    acceptance.verify(
                        stage,
                        repository=fixture().root,
                        path=directory / f"{stage}.json",
                    )
                self.assertIn("has not been recorded", str(caught.exception))

    def test_a_report_that_is_not_a_json_object_is_refused(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for body, because in (
            ("not json at all", "not readable as JSON"),
            ("[]", "not a JSON object"),
            ('"staging"', "not a JSON object"),
        ):
            with self.subTest(body=body):
                path = directory / "staging.json"
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(acceptance.EvidenceError) as caught:
                    acceptance.verify(
                        acceptance.STAGE_STAGING,
                        repository=fixture().root,
                        path=path,
                    )
                self.assertIn(because, str(caught.exception))

    def test_a_report_larger_than_the_bound_is_refused(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "staging.json"
        path.write_text("0" * (acceptance.MAX_DOCUMENT_BYTES + 1), encoding="utf-8")
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                acceptance.STAGE_STAGING, repository=fixture().root, path=path
            )
        self.assertIn("exceeds", str(caught.exception))

    def test_an_unreachable_pi_checkout_is_a_refusal_rather_than_a_skip(self) -> None:
        with (
            mock.patch.object(
                workbench_acceptance_contract, "locate_pi_repository", return_value=None
            ),
            self.assertRaises(acceptance.EvidenceError) as caught,
        ):
            acceptance.evidence_directory({})
        self.assertIn("no Pi checkout is reachable", str(caught.exception))

    def test_the_gate_reads_the_registered_pi_location(self) -> None:
        # The path is named by the plan, so it is asserted rather than assumed.
        pi = Path("/tmp/pi-checkout")
        with mock.patch.object(
            workbench_acceptance_contract, "locate_pi_repository", return_value=pi
        ):
            path = acceptance.evidence_path(acceptance.STAGE_PRODUCTION, {})
        self.assertEqual(
            path,
            pi / "services/pi-monitoring/docs/workbench/evidence/production.json",
        )


class WrongReleaseEvidence(AcceptanceCase):
    """A report is about one release, and the artifact is what says which."""

    def test_a_digest_that_the_named_commit_does_not_pack_to_is_refused(self) -> None:
        fix = fixture()
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(acceptance.STAGE_STAGING, "release.artifact", fix.digest_package),
            "this evidence is about another release",
        )

    def test_a_fabricated_digest_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "release.artifact", "0" * 64),
            "this evidence is about another release",
        )

    def test_a_revision_that_is_not_a_commit_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "release.revision", "f" * 40),
            "not a commit in this repository",
        )

    def test_a_revision_that_is_not_in_the_expected_form_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "release.revision", "HEAD"),
            "not in the expected form",
        )

    def test_a_host_running_another_release_is_refused(self) -> None:
        # The failure this rule exists for: a report that accepts one release
        # while one of the two machines is still running another.
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "hosts.pi.release",
                fixture().digest_package,
            ),
            "not the release being accepted",
        )

    def test_a_staging_host_running_another_release_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(
                acceptance.STAGE_STAGING,
                "hosts.pi.release",
                fixture().digest_package,
            ),
            "not the release being accepted",
        )

    def test_retirement_must_name_the_accepted_production_deployment(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        retirement = valid_document(acceptance.STAGE_RETIREMENT)
        production = valid_document(acceptance.STAGE_PRODUCTION)
        production["release"] = {
            "revision": fixture().package,
            "artifact": fixture().digest_package,
        }
        for host in production["hosts"].values():
            host["release"] = fixture().digest_package
        retirement_path = directory / "retirement.json"
        retirement_path.write_text(json.dumps(retirement), encoding="utf-8")
        (directory / "production.json").write_text(
            json.dumps(production), encoding="utf-8"
        )
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                acceptance.STAGE_RETIREMENT,
                repository=fixture().root,
                path=retirement_path,
            )
        self.assertIn("accepted production deployment", str(caught.exception))

    def test_retirement_rechecks_the_production_phone_acceptance(self) -> None:
        retirement = valid_document(acceptance.STAGE_RETIREMENT)
        path = self.written(acceptance.STAGE_RETIREMENT, retirement)
        production_path = path.with_name("production.json")
        production = json.loads(production_path.read_text(encoding="utf-8"))
        production["phone"]["confirmation"]["verdict"] = "pending"
        production_path.write_text(json.dumps(production), encoding="utf-8")
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                acceptance.STAGE_RETIREMENT,
                repository=fixture().root,
                path=path,
            )
        self.assertIn("acceptance stays pending", str(caught.exception))


class StaleEvidence(AcceptanceCase):
    """Evidence about software this repository has since moved past."""

    def test_a_package_commit_after_the_release_makes_live_deployments_stale(
        self,
    ) -> None:
        fix = fixture()
        for stage in (acceptance.STAGE_PRODUCTION, acceptance.STAGE_RETIREMENT):
            with self.subTest(stage=stage):
                document = valid_document(stage)
                document["release"] = {
                    "revision": fix.package,
                    "artifact": fix.digest_package,
                }
                for host in document["hosts"].values():
                    host["release"] = fix.digest_package
                self.refuses(stage, document, "the evidence is stale")

    def test_a_release_that_was_never_merged_is_refused(self) -> None:
        fix = fixture()
        document = valid_document(acceptance.STAGE_STAGING)
        document["release"] = {
            "revision": fix.branch,
            "artifact": fix.digest_package,
        }
        for host in document["hosts"].values():
            host["release"] = fix.digest_package
        self.refuses(acceptance.STAGE_STAGING, document, "is not an ancestor")

    def test_a_report_that_predates_the_release_it_accepts_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(
                acceptance.STAGE_STAGING,
                "recorded_at",
                fixture().committed_at - 1,
            ),
            "predates the release it accepts",
        )

    def test_a_report_dated_in_the_future_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT,
                "recorded_at",
                fixture().now + acceptance.MAX_CLOCK_SKEW_SECONDS + 600,
            ),
            "dated in the future",
        )

    def test_an_observation_later_than_the_report_carrying_it_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT,
                "hosts.mac.observed_at",
                fixture().now + 3600,
            ),
            "later than the report",
        )

    def test_the_release_under_acceptance_defaults_to_head(self) -> None:
        fix = fixture()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(acceptance.ENV_RELEASE_REVISION, None)
            self.assertEqual(acceptance.accepted_revision(fix.root), fix.repackaged)


class SyntheticEvidence(AcceptanceCase):
    """A suite on one laptop cannot produce a report about two real machines."""

    def test_provenance_that_is_not_the_actual_host_is_refused(self) -> None:
        for role in ("mac", "pi"):
            with self.subTest(role=role):
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    edited(
                        acceptance.STAGE_RETIREMENT, f"hosts.{role}.kind", "simulated"
                    ),
                    "is only ever recorded from the machine it happened on",
                )

    def test_a_hostname_that_names_a_fixture_is_refused(self) -> None:
        for name in ("localhost", "test-mac", "fake-pi", "mock-host", "pi.example"):
            with self.subTest(name=name):
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    edited(acceptance.STAGE_RETIREMENT, "hosts.mac.host", name),
                    "names a fixture rather than a machine",
                )

    def test_a_host_running_the_wrong_operating_system_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.mac.system", "Linux"),
            "the mac runs Darwin",
        )
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.pi.system", "Darwin"),
            "the pi runs Linux",
        )

    def test_one_machine_recorded_as_two_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.pi.host", "studio-mini"),
            "recorded from one machine is not a two-host deployment",
        )

    def test_a_missing_host_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            without(acceptance.STAGE_RETIREMENT, "hosts.pi"),
            "missing pi",
        )


class SanitizedEvidence(AcceptanceCase):
    """The reports are pushed, so the reader is also a privacy gate."""

    def test_an_unrecognized_key_is_refused_rather_than_ignored(self) -> None:
        for path, value in (
            ("operator_note", "a note"),
            ("hosts.mac.plan_root", "somewhere"),
            ("release.branch", "main"),
        ):
            with self.subTest(path=path):
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    edited(acceptance.STAGE_RETIREMENT, path, value),
                    "unrecognized",
                )

    def test_a_string_carrying_a_path_or_an_identity_is_refused(self) -> None:
        for value in (
            "Users-someone-plans",
            "shed-pi.local\nsecond line",
            "someone-at-somewhere",
        ):
            with self.subTest(value=value):
                document = valid_document(acceptance.STAGE_RETIREMENT)
                # Placed in a key the schema does allow, so what fires is the
                # content rule rather than the allowlist.
                document["hosts"]["mac"]["host"] = value.replace("-at-", "@").replace(
                    "Users-", "/Users/"
                )
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    document,
                    "a sanitized report carries no",
                )

    def test_an_overlong_string_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT,
                "hosts.mac.system",
                "D" * (acceptance.MAX_STRING_LENGTH + 1),
            ),
            f"longer than {acceptance.MAX_STRING_LENGTH}",
        )

    def test_a_value_of_a_type_the_schema_never_uses_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.mac.observed_at", None),
            "NoneType is not allowed in a report",
        )

    def test_a_negative_number_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "recorded_at", -1),
            "not a usable number",
        )

    def test_an_unbounded_list_is_refused(self) -> None:
        document = valid_document(acceptance.STAGE_RETIREMENT)
        document["hosts"]["mac"]["system"] = ["Darwin"] * (
            acceptance.MAX_SEQUENCE_LENGTH + 1
        )
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            document,
            f"more than {acceptance.MAX_SEQUENCE_LENGTH} entries",
        )

    def test_a_report_that_names_another_stage_is_refused(self) -> None:
        document = valid_document(acceptance.STAGE_PRODUCTION)
        document["stage"] = acceptance.STAGE_STAGING
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            document,
            "a report read as one stage may not describe another",
        )

    def test_a_report_in_another_schema_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT, "schema", acceptance.SCHEMA_VERSION + 1
            ),
            f"accepts schema {acceptance.SCHEMA_VERSION}",
        )


class StageChecks(AcceptanceCase):
    """Each stage owes the checks its own completion contract names."""

    def test_a_check_recorded_as_anything_but_passed_is_refused(self) -> None:
        for stage in acceptance.STAGES:
            name = acceptance.STAGE_CHECKS[stage][0]
            with self.subTest(stage=stage):
                self.refuses(
                    stage,
                    edited(stage, f"checks.{name}.outcome", "skipped"),
                    f"checks.{name}: recorded as 'skipped'",
                )

    def test_an_omitted_check_is_refused_rather_than_read_as_a_shorter_list(
        self,
    ) -> None:
        for stage in acceptance.STAGES:
            name = acceptance.STAGE_CHECKS[stage][-1]
            with self.subTest(stage=stage):
                self.refuses(stage, without(stage, f"checks.{name}"), f"missing {name}")

    def test_a_check_attributed_to_no_deployed_host_is_refused(self) -> None:
        name = acceptance.STAGE_CHECKS[acceptance.STAGE_RETIREMENT][0]
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, f"checks.{name}.host", "laptop"),
            "is not a deployed host",
        )

    def test_a_check_observed_before_the_release_existed_is_refused(self) -> None:
        name = acceptance.STAGE_CHECKS[acceptance.STAGE_STAGING][0]
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(
                acceptance.STAGE_STAGING,
                f"checks.{name}.observed_at",
                fixture().committed_at - 60,
            ),
            "predates the release it is evidence for",
        )

    def test_each_stage_owes_the_checks_its_own_contract_names(self) -> None:
        # A stage cannot be accepted by another stage's evidence simply because
        # the two documents look alike.
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                others: set[str] = set()
                for other in acceptance.STAGES:
                    if other != stage:
                        others |= set(acceptance.STAGE_CHECKS[other])
                self.assertTrue(set(acceptance.STAGE_CHECKS[stage]) - others)


class StagingSoak(AcceptanceCase):
    """Staging owes a full hour, in three named phases."""

    def test_a_soak_shorter_than_an_hour_is_refused(self) -> None:
        fix = fixture()
        document = valid_document(acceptance.STAGE_STAGING)
        document["soak"]["ended_at"] = document["soak"]["started_at"] + 1800
        document["soak"]["phases"] = {"active": 900, "idle": 600, "reconnect": 300}
        self.assertLess(document["soak"]["ended_at"], fix.now)
        self.refuses(acceptance.STAGE_STAGING, document, "short of the 3600s soak")

    def test_a_phase_that_never_happened_is_refused(self) -> None:
        for phase in acceptance.SOAK_PHASES:
            with self.subTest(phase=phase):
                self.refuses(
                    acceptance.STAGE_STAGING,
                    edited(acceptance.STAGE_STAGING, f"soak.phases.{phase}", 0),
                    f"phases.{phase}: never happened",
                )

    def test_an_omitted_phase_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            without(acceptance.STAGE_STAGING, "soak.phases.reconnect"),
            "missing reconnect",
        )

    def test_phases_that_add_up_to_more_than_the_soak_are_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(acceptance.STAGE_STAGING, "soak.phases.active", 9000),
            "add up to more than the soak lasted",
        )

    def test_a_soak_the_other_stages_do_not_carry(self) -> None:
        # The block is staging's, so the other two stages refuse it as a key
        # they do not recognize rather than silently carrying it.
        document = valid_document(acceptance.STAGE_PRODUCTION)
        document["soak"] = valid_document(acceptance.STAGE_STAGING)["soak"]
        self.refuses(acceptance.STAGE_PRODUCTION, document, "unrecognized soak")


class ProductionConfirmation(AcceptanceCase):
    """The operator's own answer, in the order they were asked for it."""

    def test_an_unanswered_confirmation_keeps_acceptance_pending(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.verdict", "pending"
            ),
            "acceptance stays pending until the operator answers",
        )

    def test_a_refused_confirmation_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.verdict", "declined"
            ),
            "acceptance stays pending",
        )

    def test_a_missing_phone_block_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            without(acceptance.STAGE_PRODUCTION, "phone"),
            "missing phone",
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            without(acceptance.STAGE_PRODUCTION, "phone.readiness"),
            "missing readiness",
        )

    def test_readiness_granted_before_it_was_asked_for_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "phone.readiness.granted_at",
                fixture().now - 6100,
            ),
            "granted before it was asked for",
        )

    def test_a_confirmation_asked_before_readiness_was_granted_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "phone.confirmation.requested_at",
                fixture().now - 5950,
            ),
            "before the operator declared the phone ready",
        )

    def test_a_confirmation_answered_before_it_was_asked_is_refused(self) -> None:
        document = valid_document(acceptance.STAGE_PRODUCTION)
        document["phone"]["confirmation"]["answered_at"] = (
            document["phone"]["confirmation"]["requested_at"] - 10
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION, document, "answered before it was asked"
        )

    def test_a_confirmation_of_another_release_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "phone.confirmation.release",
                fixture().digest_package,
            ),
            "not the release being accepted",
        )

    def test_a_confirmation_that_did_not_come_from_the_phone_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(acceptance.STAGE_PRODUCTION, "phone.confirmation.kind", "recorded"),
            "only ever recorded from the phone",
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.host", "studio-mini"
            ),
            "confirmed from the Mac or the Pi is not a phone acceptance",
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.host", "test-phone"
            ),
            "names a fixture rather than a phone",
        )

    def test_every_phone_check_must_be_confirmed(self) -> None:
        for name in acceptance.PHONE_CHECKS:
            with self.subTest(name=name):
                self.refuses(
                    acceptance.STAGE_PRODUCTION,
                    edited(
                        acceptance.STAGE_PRODUCTION,
                        f"phone.confirmation.checks.{name}",
                        False,
                    ),
                    f"checks.{name}: not confirmed",
                )
                self.refuses(
                    acceptance.STAGE_PRODUCTION,
                    without(
                        acceptance.STAGE_PRODUCTION,
                        f"phone.confirmation.checks.{name}",
                    ),
                    f"missing {name}",
                )

    def test_only_production_carries_a_phone_confirmation(self) -> None:
        # Staging and retirement are not the sessions that ask the operator, so
        # a phone block in either is a field the reader does not recognize.
        for stage in (acceptance.STAGE_STAGING, acceptance.STAGE_RETIREMENT):
            with self.subTest(stage=stage):
                document = valid_document(stage)
                document["phone"] = valid_document(acceptance.STAGE_PRODUCTION)["phone"]
                self.refuses(stage, document, "unrecognized phone")


class EntryPoints(unittest.TestCase):
    """What the three deployment contracts actually invoke."""

    def entry_points(self) -> Sequence[tuple[str, type[unittest.TestCase]]]:
        return (
            (acceptance.STAGE_STAGING, acceptance.StagingEvidence),
            (acceptance.STAGE_PRODUCTION, acceptance.ProductionEvidence),
            (acceptance.STAGE_RETIREMENT, acceptance.RetirementEvidence),
        )

    def test_each_stage_has_a_unittest_entry_point_named_by_its_contract(self) -> None:
        for stage, case in self.entry_points():
            with self.subTest(stage=stage):
                self.assertTrue(issubclass(case, unittest.TestCase))
                self.assertEqual(getattr(case, "stage", None), stage)
                loaded = unittest.defaultTestLoader.loadTestsFromTestCase(case)
                self.assertTrue(loaded.countTestCases())

    def test_missing_evidence_fails_the_entry_point_rather_than_skipping_it(
        self,
    ) -> None:
        # The property that matters for the deployment contracts: pointed at a
        # directory with no report in it, each entry point fails. A skip would
        # let `python3 -m unittest -q ...` exit zero on no evidence at all.
        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.dict(os.environ, {acceptance.ENV_EVIDENCE_DIR: str(empty)}):
            for stage, case in self.entry_points():
                with self.subTest(stage=stage):
                    result = unittest.TestResult()
                    unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
                    self.assertEqual(result.errors, [])
                    self.assertEqual(result.skipped, [])
                    self.assertTrue(result.failures)
                    for _, trace in result.failures:
                        self.assertIn("has not been recorded", trace)

    def test_the_gate_is_invisible_to_discovery_until_evidence_exists(self) -> None:
        # Named so `unittest discover` cannot reach it: there is nothing to read
        # until a deployment session has run, and a gate that failed the whole
        # suite for that reason is a gate somebody would turn off. The same
        # arrangement as `tests/asl_live_redirect_acceptance.py`.
        self.assertFalse(fnmatch("workbench_acceptance.py", "test*.py"))
        module = "tests.workbench_acceptance"
        for listed in (
            run_suite.FAST_MODULES,
            run_suite.PORTABLE_MODULES,
            run_suite.INTEGRATION_MODULES,
        ):
            self.assertNotIn(module, listed)

    def test_this_test_is_registered_where_it_runs(self) -> None:
        # It spawns `git` and packs real artifacts, so it belongs in the
        # integration lane rather than the fast one.
        self.assertIn("tests.test_workbench_acceptance", run_suite.INTEGRATION_MODULES)
        self.assertNotIn("tests.test_workbench_acceptance", run_suite.PORTABLE_MODULES)


class CommandLine(unittest.TestCase):
    """The template a deployment session reads the schema out of."""

    def run_cli(self, arguments: Sequence[str]) -> tuple[int, str]:
        # Captured rather than printed: this suite's own output is read by a
        # person looking for failures, and a schema dump in the middle of it is
        # noise they have to scroll past.
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = acceptance.main(list(arguments))
        return status, stream.getvalue()

    def test_the_template_prints_as_json_the_gate_would_read(self) -> None:
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                status, printed = self.run_cli(["--template", stage])
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(printed), acceptance.template(stage))

    def test_an_unknown_stage_is_refused(self) -> None:
        with self.assertRaises(acceptance.EvidenceError):
            acceptance.template("rehearsal")
        with self.assertRaises(acceptance.EvidenceError):
            acceptance.evidence_path("rehearsal", {})

    def test_verifying_a_stage_with_no_evidence_reports_a_refusal(self) -> None:
        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.dict(os.environ, {acceptance.ENV_EVIDENCE_DIR: str(empty)}):
            status, printed = self.run_cli(["--verify", "staging"])
        self.assertEqual(status, 1)
        self.assertIn("refused:", printed)

    def test_the_bare_invocation_explains_itself(self) -> None:
        status, printed = self.run_cli([])
        self.assertEqual(status, 2)
        self.assertIn("--template", printed)


if __name__ == "__main__":
    unittest.main()
