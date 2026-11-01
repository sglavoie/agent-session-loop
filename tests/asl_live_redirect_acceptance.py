"""Opt-in live redirect acceptance, composed from focused scenario modules."""

from .asl_live_redirect_availability import _read_lines, check_reachable
from .asl_live_redirect_prompts import *
from .asl_live_redirect_prompts import _mentions_conversation
from .asl_live_redirect_reporting import build_parser, build_report, format_report, main
from .asl_live_redirect_runner import run_harness_acceptance
from .asl_live_redirect_scenarios import *
from .asl_live_redirect_support import *
from .asl_live_redirect_workspace import *

if __name__ == "__main__":
    # tests.__init__ shields ordinary tests from paid CLIs with PATH stubs.
    # This explicit acceptance entry point must probe and launch the real CLI.
    # Keep custom PATH fixtures intact; remove only our repository's stubs.
    from tests import HARNESS_STUBS

    os.environ["PATH"] = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if Path(entry).resolve() != Path(HARNESS_STUBS).resolve()
    )
    raise SystemExit(main())
