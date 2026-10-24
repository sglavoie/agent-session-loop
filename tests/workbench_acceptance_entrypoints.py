from .workbench_acceptance_test_support import *


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


__all__ = tuple(name for name in globals() if not name.startswith("__"))
