# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import test_asl_typing_ratchet_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_typing_ratchet_shared import *
from .support_base import load_section_tests

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

#: The codes a package module opts back in to: every error code the baseline
#: hides. Each is a way a wrong value reaches a durable record instead of
#: raising at the call site. Only ``annotation-unchecked`` stays off, because it
#: silences notes rather than errors.
RATCHETED_CODES = frozenset(
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
    }
)

#: The codes the skill-scripts table opts back in to: every package code except
#: the three the scripts still need annotations for. A code may join this set
#: once the scripts are clean under it, and none may leave it.
SCRIPT_RATCHETED_CODES = RATCHETED_CODES - {"arg-type", "union-attr", "index"}

#: The skill script directories. They are not packages, so mypy names each
#: script by its bare file stem and the scripts table lists those stems.
SCRIPT_ROOTS = (
    REPO_ROOT / "skills" / "asl" / "scripts",
    REPO_ROOT / "skills" / "session-resplit" / "scripts",
)

#: The scripts table's anchor, named the same way as ``TABLE_ANCHORS``.
SCRIPTS_TABLE_ANCHOR = "asl_autopilot"

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

#: The package modules that meet the boundary contract: clean under all nine
#: codes and clean under the linter below. This set is a floor: every module
#: that becomes clean adds to it, and nothing may leave it. A module removed
#: from ``pyproject.toml`` fails here rather than silently rejoining the
#: untyped baseline.


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
