# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import test_asl_typing_ratchet_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_typing_ratchet_shared import *

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
#: The package trees every module of which must be on one of the two override
#: tables. ``asl_workbench`` joins the walk because it is a package this
#: repository maintains and ships, so a portable module added tomorrow has to
#: pick a table rather than inherit the untyped baseline.
PACKAGE_ROOTS = (
    REPO_ROOT / "agent_session_loop",
    REPO_ROOT / "asl_workbench",
)

#: The repository-wide suppressions as of the session that added this ratchet.
#: This set is a ceiling: a code may leave it once the tree is clean enough to
#: drop the suppression, but nothing may join it. A new module that would need
#: a tenth code is a module that needs annotations instead.
BASELINE_DISABLED_CODES = frozenset(
    {
        "arg-type",
        "union-attr",
        "index",
        "assignment",
        "var-annotated",
        "misc",
        "call-overload",
        "operator",
        "override",
        "annotation-unchecked",
    }
)

#: The codes a ratcheted module opts back in to. All three are ways a wrong
#: value reaches a durable record instead of raising at the call site.
RATCHETED_CODES = frozenset({"arg-type", "union-attr", "index"})

#: The two override tables. An overrides table has no name key -- mypy rejects
#: one as an unrecognized option -- so each is named by an anchor module that
#: only that table may list. The anchors are load-bearing: swap a table's
#: contract and the lookup below stops finding it.
BOUNDARY_TABLE = "boundary"
CHECKED_TABLE = "checked"
TABLE_ANCHORS = {
    BOUNDARY_TABLE: "agent_session_loop.artifacts",
    CHECKED_TABLE: "agent_session_loop.compact_state",
}

#: The package modules that meet the boundary contract: clean under all three
#: codes and clean under the linter below. This set is a floor: every module
#: that becomes clean adds to it, and nothing may leave it. A module removed
#: from ``pyproject.toml`` fails here rather than silently rejoining the
#: untyped baseline.


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    suite = unittest.TestSuite()
    cases = sorted(
        (
            value
            for value in globals().values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ),
        key=lambda case: case.__name__,
    )
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite
