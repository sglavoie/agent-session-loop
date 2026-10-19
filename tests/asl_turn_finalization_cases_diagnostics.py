from .asl_turn_finalization_support import *


class CommandFailureDetailTests(unittest.TestCase):
    def check(self) -> contracts.CommandCheck:
        return contracts.CommandCheck(
            name="final-review-artifact-consistent",
            argv=("python3", "-c", "pass"),
            timeout_seconds=60,
        )

    def test_the_authenticated_diagnostic_leads_the_detail(self) -> None:
        """A `python3 -c` gate renders kilobytes of argv; the reason goes first.

        Every consumer that bounds an inline string cuts the tail, so an
        argv-first detail is exactly the one that loses the reason.
        """
        detail = verification_checks._command_failure_detail(
            self.check(),
            1,
            {
                "failure_diagnostic": {
                    "check": "final-review-artifact-consistent",
                    "reason": "artifact-missing",
                }
            },
        )
        self.assertEqual(
            "final-review-artifact-consistent: artifact-missing; "
            "command exited 1: python3",
            detail,
        )

    def test_an_unauthenticated_stream_line_stays_out_of_the_detail(self) -> None:
        """The streams are application output wherever the diagnostic is absent.

        `detail` is inlined into the audit bundle, so a failed user command's
        last line must reach it through no route at all -- not on stderr, and
        not on stdout when stderr is silent.
        """
        detail = verification_checks._command_failure_detail(
            self.check(),
            2,
            {
                "stderr": "   \n",
                "stdout": "only stdout said this\n",
                "failure_diagnostic": None,
            },
        )
        self.assertEqual("command exited 2: python3", detail)

    def test_the_authored_argv_stays_out_of_the_detail(self) -> None:
        check = contracts.CommandCheck(
            name="final-review-output-evidence-based",
            argv=("python3", "-c", "x = 1  # " + "y" * 4000),
            timeout_seconds=60,
        )

        detail = verification_checks._command_failure_detail(check, 1, {})

        self.assertEqual("command exited 1: python3", detail)

    def test_diagnostic_lead_is_truncated(self) -> None:
        """A total on the lead, independent of the parser's own vocabulary."""
        detail = verification_checks._command_failure_detail(
            self.check(),
            1,
            {
                "failure_diagnostic": {
                    "check": "final-review-artifact-consistent",
                    "reason": "x" * 5000,
                }
            },
        )
        self.assertLessEqual(
            len(detail) - len("; command exited 1: python3"),
            verification_checks._COMMAND_DIAGNOSTIC_LIMIT,
        )
