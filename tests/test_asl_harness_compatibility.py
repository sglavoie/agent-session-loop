"""Tests for the pinned harness support window in `harness_compatibility`.

The subject here is an asymmetry, so the tests are organised around it: an
unrecognised harness version must never stop a run, and a known-incompatible
one must always stop it. Everything else — parsing three different vendors'
``--version`` sentences, memoising the probe, degrading every failed probe to
``unknown`` — exists to keep that asymmetry from being decided by accident.

No test here spawns a real harness. Probes run against shell scripts written
into a temporary directory, which is also what makes the "no model use" claim
checkable: the only argv this module ever builds is ``<exe> --version``.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import events, pause_policy
from agent_session_loop import harness_compatibility as compat
from tests import asl_live_redirect_acceptance as live
from tests import run_suite


def _write_fake_cli(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _version_script(text: str) -> str:
    return f"#!/bin/sh\nprintf '%s\\n' {text!r}\n"


class VersionParsingTests(unittest.TestCase):
    def test_parses_each_vendor_version_sentence(self) -> None:
        # The three real shapes, captured from the installed CLIs. Each vendor
        # puts the number in a different place, which is why the parser matches
        # the dotted-number shape rather than the surrounding prose.
        self.assertEqual("0.149.1", compat.parse_version("codex-cli 0.149.1"))
        self.assertEqual("2.1.231", compat.parse_version("2.1.231 (Claude Code)"))
        self.assertEqual(
            "1.0.80",
            compat.parse_version(
                "GitHub Copilot CLI 1.0.80.\nRun 'copilot update' to check for updates."
            ),
        )

    def test_rejects_output_with_no_dotted_version(self) -> None:
        self.assertIsNone(compat.parse_version(""))
        self.assertIsNone(compat.parse_version("version 7"))
        self.assertIsNone(compat.parse_version("no version here"))

    def test_version_key_orders_numerically_not_lexically(self) -> None:
        self.assertLess(compat.version_key("1.0.9"), compat.version_key("1.0.10"))
        self.assertLess(compat.version_key("0.99.0"), compat.version_key("1.0.0"))

    def test_version_key_truncates_at_a_non_numeric_segment(self) -> None:
        # A prerelease orders as its release. Refusing a specific prerelease is
        # the blocked-list's job, not the comparator's.
        self.assertEqual((1, 2, 3), compat.version_key("1.2.3-beta.1"))
        self.assertEqual((1, 2), compat.version_key("1.2.x.9"))


class ClassificationTests(unittest.TestCase):
    def test_pinned_latest_and_previous_are_supported(self) -> None:
        for harness, policy in compat.POLICY.items():
            for version in policy.pinned_versions():
                result = compat.classify(harness, version)
                self.assertEqual("supported", result.status, f"{harness} {version}")
                self.assertEqual("ok", result.severity)
                self.assertFalse(result.blocks)

    def test_version_between_previous_and_latest_is_supported(self) -> None:
        result = compat.classify("claude", "2.1.0")
        self.assertEqual("supported", result.status)

    def test_newer_than_tested_warns_and_never_blocks(self) -> None:
        result = compat.classify("codex", "99.0.0")
        self.assertEqual("unknown", result.status)
        self.assertEqual("warn", result.severity)
        self.assertEqual("newer-than-tested", result.reason)
        self.assertFalse(result.blocks)

    def test_older_than_tested_but_above_minimum_warns(self) -> None:
        result = compat.classify("codex", "0.141.0")
        self.assertEqual("unknown", result.status)
        self.assertEqual("older-than-tested", result.reason)
        self.assertFalse(result.blocks)

    def test_below_minimum_blocks(self) -> None:
        result = compat.classify("codex", "0.100.0")
        self.assertEqual("incompatible", result.status)
        self.assertEqual("block", result.severity)
        self.assertEqual("below-minimum", result.reason)
        self.assertTrue(result.blocks)

    def test_explicitly_blocked_version_blocks_even_inside_the_window(self) -> None:
        policy = compat.POLICY["claude"]
        blocked = compat.HarnessPolicy(
            harness="claude",
            version_argv=policy.version_argv,
            latest=policy.latest,
            previous=policy.previous,
            minimum=policy.minimum,
            blocked=("2.1.0",),
        )
        with unittest.mock.patch.dict(compat.POLICY, {"claude": blocked}, clear=False):
            result = compat.classify("claude", "2.1.0")
        self.assertEqual("incompatible", result.status)
        self.assertEqual("blocked-version", result.reason)

    def test_absent_version_warns_rather_than_blocking(self) -> None:
        result = compat.classify("copilot", None)
        self.assertEqual("unknown", result.status)
        self.assertEqual("version-unavailable", result.reason)
        self.assertFalse(result.blocks)

    def test_every_policy_window_is_internally_ordered(self) -> None:
        for harness, policy in compat.POLICY.items():
            with self.subTest(harness=harness):
                self.assertLessEqual(
                    compat.version_key(policy.minimum),
                    compat.version_key(policy.previous),
                )
                self.assertLessEqual(
                    compat.version_key(policy.previous),
                    compat.version_key(policy.latest),
                )
                self.assertEqual(harness, policy.harness)

    def test_policy_covers_exactly_the_supported_harness_names(self) -> None:
        from agent_session_loop.harnesses import HARNESS_NAMES

        self.assertEqual(set(HARNESS_NAMES), set(compat.POLICY))


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        compat.clear_probe_cache()
        self.addCleanup(compat.clear_probe_cache)
        self.addCleanup(self._tmp.cleanup)

    def test_probe_reads_a_real_version_string_from_a_fake_cli(self) -> None:
        exe = _write_fake_cli(
            self.tmp_path, "codex", _version_script("codex-cli 0.149.1")
        )
        probe = compat.probe_executable("codex", str(exe), use_cache=False)
        self.assertEqual("0.149.1", probe.version)
        self.assertEqual("probed", probe.reason)

    def test_probe_only_ever_asks_for_the_version(self) -> None:
        # The no-model-use guarantee is exactly this: the argv is the harness
        # plus `--version` and nothing else. A CLI that recorded a prompt would
        # record it here.
        argv_log = self.tmp_path / "argv.log"
        exe = _write_fake_cli(
            self.tmp_path,
            "claude",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {str(argv_log)!r}\n'
            'printf "2.1.231 (Claude Code)\\n"\n',
        )
        compat.probe_executable("claude", str(exe), use_cache=False)
        self.assertEqual(["--version"], argv_log.read_text().split())

    def test_nonzero_exit_degrades_to_unknown(self) -> None:
        exe = _write_fake_cli(self.tmp_path, "codex", "#!/bin/sh\nexit 7\n")
        result = compat.check_executable("codex", str(exe), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertEqual("probe-exit-7", result.reason)

    def test_unparseable_output_degrades_to_unknown_and_quotes_it(self) -> None:
        exe = _write_fake_cli(self.tmp_path, "codex", _version_script("who knows"))
        result = compat.check_executable("codex", str(exe), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertEqual("unparseable-version", result.reason)
        self.assertIn("who knows", result.detail)

    def test_timeout_degrades_to_unknown(self) -> None:
        exe = _write_fake_cli(self.tmp_path, "codex", "#!/bin/sh\nsleep 30\n")
        result = compat.check_executable(
            "codex", str(exe), timeout_seconds=0.5, use_cache=False
        )
        self.assertEqual("unknown", result.status)
        self.assertEqual("probe-timeout", result.reason)

    def test_unrunnable_executable_degrades_to_unknown(self) -> None:
        missing = self.tmp_path / "absent"
        result = compat.check_executable("codex", str(missing), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertFalse(result.blocks)

    def test_disabled_probe_reports_unknown_without_spawning(self) -> None:
        marker = self.tmp_path / "spawned"
        exe = _write_fake_cli(
            self.tmp_path,
            "codex",
            f"#!/bin/sh\n: > {str(marker)!r}\nprintf 'codex-cli 0.149.1\\n'\n",
        )
        with unittest.mock.patch.dict(
            os.environ, {compat.PROBE_ENV_VAR: "0"}, clear=False
        ):
            result = compat.check_executable("codex", str(exe), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertEqual("probe-disabled", result.reason)
        self.assertFalse(marker.exists())

    def test_probe_is_memoized_per_executable_identity(self) -> None:
        counter = self.tmp_path / "runs"
        exe = _write_fake_cli(
            self.tmp_path,
            "codex",
            f"#!/bin/sh\nprintf 'x' >> {str(counter)!r}\n"
            "printf 'codex-cli 0.149.1\\n'\n",
        )
        for _ in range(3):
            compat.probe_executable("codex", str(exe))
        self.assertEqual(1, len(counter.read_text()))

    def test_rewriting_the_executable_invalidates_the_memo(self) -> None:
        exe = _write_fake_cli(
            self.tmp_path, "codex", _version_script("codex-cli 0.148.0")
        )
        first = compat.probe_executable("codex", str(exe))
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.149.1"))
        second = compat.probe_executable("codex", str(exe))
        self.assertEqual("0.148.0", first.version)
        self.assertEqual("0.149.1", second.version)


class ResolutionTests(unittest.TestCase):
    def test_unresolvable_harness_is_unknown_not_incompatible(self) -> None:
        # preflight already refuses a missing harness with its own red finding.
        # If this module also called it incompatible, "not installed" and
        # "known broken" would be indistinguishable in the release evidence.
        result = compat.check_harness("codex", resolver=lambda _name: None)
        self.assertEqual("unknown", result.status)
        self.assertEqual("executable-not-found", result.reason)
        self.assertFalse(result.blocks)

    def test_check_all_reports_every_harness_in_a_stable_order(self) -> None:
        from agent_session_loop.harnesses import HARNESS_NAMES

        results = compat.check_all(resolver=lambda _name: None)
        self.assertEqual(list(HARNESS_NAMES), [r.harness for r in results])


class ReportTests(unittest.TestCase):
    def _results(self, *statuses: str) -> tuple[compat.Compatibility, ...]:
        versions = {
            "supported": compat.POLICY["codex"].latest,
            "unknown": "99.0.0",
            "incompatible": "0.1.0",
        }
        return tuple(compat.classify("codex", versions[status]) for status in statuses)

    def test_payload_declares_no_model_use_and_tallies_statuses(self) -> None:
        payload = compat.report_payload(
            self._results("supported", "unknown", "incompatible")
        )
        self.assertEqual(compat.SCHEMA_VERSION, payload["schema_version"])
        self.assertIs(False, payload["model_use"])
        self.assertIs(False, payload["ok"])
        self.assertEqual(
            {"supported": 1, "unknown": 1, "incompatible": 1}, payload["counts"]
        )

    def test_payload_is_json_serializable_and_carries_the_policy(self) -> None:
        payload = compat.report_payload(self._results("supported"))
        decoded = json.loads(json.dumps(payload))
        entry = decoded["harnesses"][0]
        self.assertEqual(compat.POLICY["codex"].latest, entry["policy"]["latest"])
        self.assertEqual(compat.POLICY["codex"].minimum, entry["policy"]["minimum"])

    def test_exit_code_blocks_only_on_incompatible_by_default(self) -> None:
        self.assertEqual(0, compat.exit_code(self._results("supported"), strict=False))
        self.assertEqual(0, compat.exit_code(self._results("unknown"), strict=False))
        self.assertEqual(
            1, compat.exit_code(self._results("incompatible"), strict=False)
        )

    def test_strict_rejects_unknown_with_its_own_status(self) -> None:
        self.assertEqual(0, compat.exit_code(self._results("supported"), strict=True))
        self.assertEqual(3, compat.exit_code(self._results("unknown"), strict=True))
        # A blocker outranks the strict warning so the code always names the
        # worst outcome, never the one that happened to be checked last.
        self.assertEqual(
            1,
            compat.exit_code(self._results("unknown", "incompatible"), strict=True),
        )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        compat.clear_probe_cache()
        self.addCleanup(compat.clear_probe_cache)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, **overrides: object) -> tuple[int, str]:
        args = argparse.Namespace(harness=None, strict=False, json=True)
        for key, value in overrides.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = compat.harness_compat_from_args(args)
        return code, buffer.getvalue()

    def test_json_output_is_a_single_schema_stamped_document(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.149.1"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            code, out = self._run(harness=["codex"])
        payload = json.loads(out)
        self.assertEqual(0, code)
        self.assertEqual(compat.SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(1, len(payload["harnesses"]))
        self.assertEqual("supported", payload["harnesses"][0]["status"])

    def test_known_incompatible_installation_exits_one(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.1.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            code, out = self._run(harness=["codex"])
        self.assertEqual(1, code)
        self.assertIs(False, json.loads(out)["ok"])

    def test_unknown_installation_exits_zero_but_three_under_strict(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 99.0.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            lenient, out = self._run(harness=["codex"])
            compat.clear_probe_cache()
            strict, _ = self._run(harness=["codex"], strict=True)
        self.assertEqual(0, lenient)
        self.assertEqual(3, strict)
        # Warned, not hidden: the document still says the pairing is untested.
        self.assertIs(True, json.loads(out)["ok"])
        self.assertEqual("unknown", json.loads(out)["harnesses"][0]["status"])

    def test_text_output_names_the_consequence_of_each_status(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.1.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            code, out = self._run(harness=["codex"], json=False)
        self.assertEqual(1, code)
        self.assertIn("BLOCK", out)
        self.assertIn("below-minimum", out)


class _ExplodingAdapter:
    """A real adapter with its `run` replaced by a tripwire.

    The live acceptance runner's central promise is that it costs nothing until
    someone opts in. The only way to check a promise about something *not*
    happening is to make it loud when it does. Everything except `run` is
    delegated to the genuine adapter so version resolution still behaves
    exactly as it does in production.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def run(self, **kwargs: object) -> object:
        raise AssertionError(
            "the live acceptance runner launched a harness turn it was not "
            "authorised to launch"
        )


def _acceptance(
    harness: str,
    *,
    version: str | None,
    statuses: dict[str, str],
) -> live.HarnessAcceptance:
    return live.HarnessAcceptance(
        harness=harness,  # type: ignore[arg-type]
        executable=f"/nowhere/{harness}",
        version=version,
        compatibility="supported",
        compatibility_reason="pinned",
        capabilities={},
        results=[
            live.ScenarioResult(key=key, status=status, detail="synthetic")
            for key, status in statuses.items()
        ],
        limitations=[],
        seconds=1.0,
    )


class LiveAcceptanceOptInTests(unittest.TestCase):
    """`tests/asl_live_redirect_acceptance.py` must be inert until paid for.

    The live runner is the one module in this tree allowed to spend provider
    quota, so its default-off behaviour is not a convenience -- it is the reason
    the scheduled compatibility lane and this session's own gate can invoke it
    at all. These tests hold that line without ever launching a harness.
    """

    def setUp(self) -> None:
        compat.clear_probe_cache()
        self.addCleanup(compat.clear_probe_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        real = live.harnesses.get_adapter
        self.adapter_patch = unittest.mock.patch.object(
            live.harnesses,
            "get_adapter",
            lambda name: _ExplodingAdapter(real(name)),
        )
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)

    def _main(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with (
            unittest.mock.patch.dict(
                os.environ, {"PATH": str(self.tmp_path)}, clear=False
            ),
            unittest.mock.patch.dict(
                os.environ, {live.OPT_IN_ENV_VAR: ""}, clear=False
            ),
            redirect_stdout(buffer),
        ):
            code = live.main(argv)
        return code, buffer.getvalue()

    def test_opt_in_needs_an_explicit_switch(self) -> None:
        self.assertFalse(live.live_opt_in({}))
        self.assertFalse(live.live_opt_in({live.OPT_IN_ENV_VAR: ""}))
        self.assertFalse(live.live_opt_in({live.OPT_IN_ENV_VAR: "0"}))
        self.assertFalse(live.live_opt_in({live.OPT_IN_ENV_VAR: "maybe"}))
        self.assertTrue(live.live_opt_in({live.OPT_IN_ENV_VAR: "1"}))
        self.assertTrue(live.live_opt_in({live.OPT_IN_ENV_VAR: " YES "}))
        # The flag is the interactive half and needs no environment at all.
        self.assertTrue(live.live_opt_in({}, flag=True))

    def test_the_default_run_launches_nothing_and_succeeds(self) -> None:
        code, out = self._main(["--all-harnesses", "--json"])
        self.assertEqual(0, code)
        report = json.loads(out)
        self.assertIs(False, report["opted_in"])
        self.assertIs(False, report["model_use"])
        self.assertIs(True, report["ok"])
        self.assertEqual(0, report["counts"]["passed"])
        self.assertEqual(0, report["counts"]["failed"])
        self.assertEqual(
            len(live.SCENARIOS) * len(live.HARNESS_NAMES),
            report["counts"]["skipped"],
        )

    def test_every_scenario_is_reported_for_every_harness(self) -> None:
        # A missing row and a passing row are indistinguishable in a report that
        # lists only what it got to, so the matrix has to be complete.
        _code, out = self._main(["--all-harnesses", "--json"])
        report = json.loads(out)
        self.assertEqual(
            list(live.HARNESS_NAMES),
            [entry["harness"] for entry in report["harnesses"]],
        )
        for entry in report["harnesses"]:
            self.assertEqual(
                list(live.SCENARIO_KEYS),
                [row["scenario"] for row in entry["scenarios"]],
            )

    def test_skipping_says_which_switch_would_have_run_it(self) -> None:
        _code, out = self._main(["--all-harnesses"])
        self.assertIn("live acceptance is opt-in and was not enabled", out)
        self.assertIn("--live", out)
        self.assertIn(live.OPT_IN_ENV_VAR, out)

    def test_an_uninstalled_harness_is_skipped_rather_than_failed(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            acceptance = live.run_harness_acceptance("codex", opted_in=True)
        self.assertEqual(len(live.SCENARIOS), acceptance.skipped)
        self.assertEqual(0, acceptance.failed)
        self.assertTrue(
            any("not installed" in note for note in acceptance.limitations),
            acceptance.limitations,
        )

    def test_a_blocked_version_is_never_exercised(self) -> None:
        # _ExplodingAdapter is still installed: reaching the matrix at all with
        # a below-minimum binary would raise rather than quietly pass.
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.1.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            acceptance = live.run_harness_acceptance("codex", opted_in=True)
        self.assertEqual("incompatible", acceptance.compatibility)
        self.assertEqual(len(live.SCENARIOS), acceptance.skipped)
        self.assertTrue(
            any("known" in note for note in acceptance.limitations),
            acceptance.limitations,
        )


class LiveAcceptanceReportTests(unittest.TestCase):
    """The report must not let installation pass for verification."""

    def test_a_version_is_verified_only_when_a_scenario_passed(self) -> None:
        report = live.build_report(
            [
                _acceptance(
                    "codex",
                    version="0.149.1",
                    statuses={live.SCENARIO_STOPS: live.STATUS_PASSED},
                ),
                _acceptance(
                    "claude",
                    version="2.1.231",
                    statuses={live.SCENARIO_STOPS: live.STATUS_SKIPPED},
                ),
            ],
            opted_in=True,
            requested=["codex", "claude"],
            seconds=1.0,
        )
        # Installed and probed, but nothing ran against it: inventory, not proof.
        self.assertEqual("0.149.1", report["verified_versions"]["codex"])
        self.assertIsNone(report["verified_versions"]["claude"])

    def test_the_report_disclaims_the_versions_it_did_not_run(self) -> None:
        report = live.build_report(
            [
                _acceptance(
                    "codex",
                    version="0.149.1",
                    statuses={live.SCENARIO_STOPS: live.STATUS_PASSED},
                )
            ],
            opted_in=True,
            requested=["codex"],
            seconds=1.0,
        )
        self.assertIn("support policy, not", report["verification_note"])
        self.assertIn("no other version is claimed", report["verification_note"])
        # The policy window travels with the evidence so the two cannot be
        # conflated by a reader who only has the report.
        window = report["harnesses"][0]["policy_window"]
        self.assertEqual(compat.POLICY["codex"].latest, window["latest"])
        self.assertIn(report["verification_note"], live.format_report(report))

    def test_any_failure_makes_the_whole_report_fail(self) -> None:
        report = live.build_report(
            [
                _acceptance(
                    "codex",
                    version="0.149.1",
                    statuses={
                        live.SCENARIO_STOPS: live.STATUS_PASSED,
                        live.SCENARIO_FORCED: live.STATUS_FAILED,
                    },
                )
            ],
            opted_in=True,
            requested=["codex"],
            seconds=1.0,
        )
        self.assertIs(False, report["ok"])
        self.assertEqual(1, report["counts"]["failed"])

    def test_unreported_scenarios_fail_rather_than_vanish(self) -> None:
        matrix = live._Matrix()
        matrix.verdict(live.SCENARIO_STOPS, True, "stopped")
        matrix.fill(live.STATUS_FAILED, "the owning sequence did not report")
        results = {result.key: result for result in matrix.results()}
        self.assertEqual(len(live.SCENARIOS), len(results))
        # An already-recorded verdict is never overwritten by the sweep.
        self.assertEqual(live.STATUS_PASSED, results[live.SCENARIO_STOPS].status)
        self.assertEqual(live.STATUS_FAILED, results[live.SCENARIO_CONTEXT].status)


class LiveAcceptancePromptTests(unittest.TestCase):
    """The synthetic prompts have to be able to report failure."""

    def test_the_context_check_cannot_be_answered_from_disk(self) -> None:
        prompt = live.first_turn_prompt("PASSPHRASE-ABC", "NONCE-1")
        self.assertIn("PASSPHRASE-ABC", prompt)
        self.assertIn("Do not write the passphrase into any file", prompt)
        correction = live.correction_text()
        self.assertIn("do not search the filesystem", correction.lower())
        self.assertIn("it was never written to disk", correction)

    def test_the_worker_is_given_an_honest_way_to_fail(self) -> None:
        # Without this, a worker that lost the conversation would be pushed to
        # invent a passphrase, and the scenario would measure guessing.
        self.assertIn("SECRET=UNKNOWN", live.correction_text())

    def test_the_resumed_prompt_carries_the_journal_block_verbatim(self) -> None:
        prompt = live.resume_turn_prompt(["FIRST LINE", "SECOND LINE"])
        self.assertIn("FIRST LINE\nSECOND LINE", prompt)

    def test_a_provider_refusal_is_not_a_harness_finding(self) -> None:
        # Found by the first live run against copilot: an exhausted monthly
        # quota comes back as exit status 0 with the refusal in the *final
        # message*, which every field a runner normally reads calls a success.
        # Uncaught, it became six confident findings about protocol behaviour
        # in a harness that never ran a single tool.
        quota = "Error: You have exceeded your monthly quota (Request ID: D366)"
        self.assertEqual("exceeded your monthly quota", live.provider_refusal(quota))
        self.assertEqual("unauthorized", live.provider_refusal("401 Unauthorized"))
        self.assertIsNone(live.provider_refusal("READY"))

    def test_an_unreachable_harness_is_skipped_not_failed(self) -> None:
        # Skipped rather than failed, because nothing was learned: a gate that
        # went red here would be reporting the state of somebody's account.
        acceptance = live.HarnessAcceptance(
            harness="copilot",  # type: ignore[arg-type]
            executable="/opt/homebrew/bin/copilot",
            version="1.0.83",
            compatibility="unknown",
            compatibility_reason="newer-than-tested",
            capabilities={"reachable": False},
            results=[
                live.ScenarioResult(key=key, status=live.STATUS_SKIPPED, detail="quota")
                for key in live.SCENARIO_KEYS
            ],
            limitations=["copilot: the provider refused the turn"],
            seconds=1.0,
        )
        report = live.build_report(
            [acceptance], opted_in=True, requested=["copilot"], seconds=1.0
        )
        self.assertIs(True, report["ok"])
        self.assertEqual(0, report["counts"]["failed"])
        # And it is still not claimed verified.
        self.assertIsNone(report["verified_versions"]["copilot"])
        self.assertIn("copilot: the provider refused", report["limitations"][0])

    def test_the_readiness_turn_asks_for_no_tools(self) -> None:
        prompt = live.ready_prompt()
        self.assertIn(live.READY_TOKEN, prompt)
        self.assertIn("do not write any files", prompt.lower())

    def test_a_setup_error_cannot_pass_for_a_refused_conversation(self) -> None:
        # Found by the first live run: `plan_dir` was missing, the adapter
        # raised, and the blanket handler scored it a pass -- which made the row
        # easier to satisfy the more broken the environment was.
        unknown = "0f0e0d0c-0b0a-0908-0706-050403020100"
        self.assertFalse(
            live._mentions_conversation(
                "codex worker turns require a plan_dir", unknown
            )
        )
        self.assertTrue(
            live._mentions_conversation(f"no conversation {unknown}", unknown)
        )
        self.assertTrue(
            live._mentions_conversation("could not resume that thread", unknown)
        )

    def test_the_receipt_parser_tolerates_worker_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resumed.txt"
            path.write_text(
                'secret = PASSPHRASE-ABC\nATTEMPT="run/19/2"\nOPERATION=`op-1`\n',
                encoding="utf-8",
            )
            values = live._read_lines(path)
        self.assertEqual("PASSPHRASE-ABC", values["SECRET"])
        self.assertEqual("run/19/2", values["ATTEMPT"])
        self.assertEqual("op-1", values["OPERATION"])
        self.assertEqual({}, live._read_lines(Path(tmp) / "absent.txt"))


class LiveAcceptancePlacementTests(unittest.TestCase):
    """Where the runner lives is part of what makes it safe."""

    def test_it_is_invisible_to_normal_discovery(self) -> None:
        name = Path(live.__file__).name
        self.assertFalse(name.startswith("test_"), name)
        loader = unittest.defaultTestLoader
        self.assertFalse(fnmatch.fnmatch(name, loader.testMethodPrefix + "*.py"))
        self.assertFalse(fnmatch.fnmatch(name, "test*.py"))

    def test_it_is_in_no_shard_the_suite_driver_runs(self) -> None:
        module = "tests.asl_live_redirect_acceptance"
        self.assertNotIn(module, run_suite.FAST_MODULES)
        self.assertNotIn(module, run_suite.PORTABLE_MODULES)
        self.assertNotIn(module, run_suite.INTEGRATION_MODULES)
        self.assertFalse(
            [suite for suite in run_suite.SCRIPT_SUITES if "live_redirect" in suite]
        )

    def test_its_deterministic_tests_are_in_a_registered_module(self) -> None:
        # This module is where they live, so it has to be in the fast lane or
        # nothing checks the opt-in guarantee on an ordinary commit.
        self.assertIn(__spec__.name, run_suite.FAST_MODULES)


class LiveAcceptancePolicyTests(unittest.TestCase):
    """The half of `unavailable-history` that no live turn can show.

    The live matrix can prove the real binary refuses a conversation it never
    saw. It cannot prove what ASL then does about it, because that decision is
    made by the run loop and not by the harness -- so the runner's report points
    here, and here is where the claim is actually pinned.
    """

    def test_missing_history_stops_for_a_person_instead_of_retrying(self) -> None:
        decision = pause_policy.classify_pause(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
        )
        self.assertIs(True, decision["requires_user"])
        self.assertIs(False, decision["recoverable"])
        self.assertEqual(0, decision["max_retries"])
        # `inspect`, not `reply`: a mailbox answer cannot restore lost context,
        # and authorising a fresh conversation discards it.
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, decision["user_action"])
        self.assertIn(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
            pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS,
        )


if __name__ == "__main__":
    unittest.main()
