"""Workbench recovery-center and triage-inbox docs regression tests."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    workbench_recovery_models,
    workbench_summary_model,
)
from tests.asl_docs_support import read_repo_text

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class WorkbenchTriageDocsTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def test_docs_describe_recovery_center_api_and_payload(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("### Recovery Center payload", docs)
        self.assertIn("### Recovery payload examples", docs)
        self.assertIn("`GET /api/recovery?plan_dir=<path>`", docs)
        self.assertIn("`WorkbenchRecovery.compact_payload()`", docs)
        self.assertIn("`workbench_recovery_models.RECOVERY_KINDS`", docs)
        example_start = docs.index("### Recovery payload examples")
        example_end = docs.index("Representative mappings:", example_start)
        examples = docs[example_start:example_end]
        for kind in workbench_recovery_models.RECOVERY_KINDS:
            self.assertIn(
                f"`{kind}`",
                docs,
                msg=f"missing recovery kind: {kind}",
            )
            self.assertIn(
                f'"kind": "{kind}"',
                examples,
                msg=f"missing recovery example for kind: {kind}",
            )
        for field in (
            "`plan_dir`",
            "`kind`",
            "`title`",
            "`summary`",
            "`severity`",
            "`recoverable`",
            "`run_id`",
            "`session_id`",
            "`turn_index`",
            "`commit_index`",
            "`evidence`",
            "`actions`",
            "`commands`",
            "`artifacts`",
            "`warnings`",
            "`warning_details`",
            "`compact`",
        ):
            self.assertIn(field, docs, msg=f"missing recovery payload field: {field}")
        for warning_field in ("`code`", "`message`", "`severity`"):
            self.assertIn(
                warning_field,
                docs,
                msg=f"missing warning_details field: {warning_field}",
            )
        for warning_code in (
            "`state-missing-race`",
            "`state-unreadable`",
            "`state-malformed-json`",
            "`state-invalid-json-shape`",
        ):
            self.assertIn(
                warning_code,
                docs,
                msg=f"missing warning_details code: {warning_code}",
            )
        self.assertIn("`RECOVERY_ACTION_TARGETS`", docs)
        self.assertIn("`RECOVERY_ACTION_TARGETS_BY_ID`", docs)
        for target in workbench_recovery_models.RECOVERY_ACTION_TARGETS:
            self.assertIn(
                f"`{target}`",
                docs,
                msg=f"missing recovery action target: {target}",
            )
            self.assertIn(
                f'"target": "{target}"',
                examples,
                msg=f"missing recovery example target: {target}",
            )
        self.assertIn("explicit but unsupported target", docs)
        self.assertIn(
            "`evidence`, `commands`, and `artifacts` are uncapped",
            docs,
        )
        self.assertIn("Artifact paths are best-effort pointers", docs)
        self.assertIn(
            "Compact payloads intentionally\n  expose only `warning_count`",
            docs,
        )

    def test_docs_describe_recovery_center_security_boundaries(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("The Recovery Center is a read-only diagnosis layer", docs)
        self.assertIn("copyable repair commands", docs)
        self.assertIn("Workbench never executes those\n  commands", docs)
        for endpoint in (
            "`/api/reply`",
            "`/api/launch`",
            "`/api/unlock`",
        ):
            self.assertIn(endpoint, docs, msg=f"missing existing endpoint: {endpoint}")
        for command in (
            "`asl preflight <plan-dir> --mark-loop-ready`",
            "`$plan-preflight-iterate <plan-dir>/`",
            "`asl verify <plan-dir>\n  --session <id> --json`",
            "`asl preflight <plan-dir> --json`",
        ):
            self.assertIn(command, docs, msg=f"missing copy-only command: {command}")

    def test_docs_describe_recovery_center_frontend_behavior(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("The Recovery Center appears near the top", docs)
        self.assertIn("renders compact queue/detail `plan.recovery`", docs)
        self.assertIn("full `/api/recovery` evidence", docs)
        self.assertIn("Recovery unavailable", docs)
        self.assertIn("Reply-needed recoveries", docs)
        self.assertIn("`/api/questions`", docs)
        self.assertIn("`/api/reply`", docs)
        self.assertIn("`/api/launch`", docs)
        self.assertIn("`/api/unlock`", docs)
        self.assertIn("inspect and navigate actions scroll/focus", docs)

    def test_docs_troubleshooting_covers_recovery_center_examples(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        for text in (
            "Recovery Center reports `not-loop-ready`",
            "Recovery Center reports `verify-failed`, `no-contract`, or\n  "
            "`malformed-contract`",
            "Recovery Center reports `commit-failed` or `commit-preflight`",
            "Recovery Center reports `stale-lock` or `stale-state`",
        ):
            self.assertIn(text, docs)

    def test_docs_describe_triage_inbox_default_behavior(self) -> None:
        # Default triage view hides completed plans and sorts by
        # urgency_rank. The docs must say so explicitly.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("triage inbox", docs)
        self.assertIn("sort ascending by\n  `urgency_rank`", docs)
        self.assertIn('"show completed" toggle', docs)
        # Search/sort/completed control bar with the five supported
        # sort modes (session 4 added `oldest`).
        self.assertIn("control bar", docs)
        self.assertIn("matches against\n  `searchable_text`", docs)
        for sort_mode in (
            "`urgency`",
            "`recent`",
            "`label`",
            "`path`",
            "`oldest`",
        ):
            self.assertIn(sort_mode, docs, msg=f"missing sort mode: {sort_mode}")
        self.assertIn("debounces at 150 ms", docs)
        # Empty-state kinds (data-state values).
        for kind in (
            "`loading`",
            "`no-plans`",
            "`no-search-results`",
            "`no-filter-matches`",
            "`completed-hidden`",
            "`no-actionable`",
        ):
            self.assertIn(kind, docs, msg=f"missing empty-state kind: {kind}")

    def test_docs_describe_two_pane_shell_and_preview(self) -> None:
        # Sessions 3 + 4 introduced the two-pane shell at the 960 px
        # breakpoint plus the plan preview panel. Document both surfaces.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("960 px", docs)
        self.assertIn("two-pane shell", docs)
        self.assertIn("preview panel", docs)
        self.assertIn("(no extra fetch)", docs)
        # Shell data attributes the CSS branches on.
        for attribute in (
            '`data-route="queue|detail"`',
            '`data-viewport="mobile|desktop"`',
            '`data-preview="empty|active"`',
        ):
            self.assertIn(attribute, docs, msg=f"missing shell attribute: {attribute}")

    def test_docs_describe_row_action_focus_contract(self) -> None:
        # Session 6 introduced #plan?dir=…&focus=<key>; document the
        # valid keys, the data-action-kind attribute, and the one-shot
        # focus semantics.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("`#plan?dir=<encoded>[&focus=<key>]`", docs)
        self.assertIn("`DETAIL_FOCUS_TARGETS`", docs)
        for key in ("`reply`", "`events`", "`active`", "`launch`", "`unlock`"):
            self.assertIn(key, docs, msg=f"missing focus key: {key}")
        self.assertIn('`data-action-kind="<RecommendedAction>"`', docs)
        for action in (
            "`watch`",
            "`reply`",
            "`inspect-failure`",
            "`unlock`",
            "`view`",
            "`run`",
            "`repair-plan`",
        ):
            self.assertIn(action, docs, msg=f"missing recommended action: {action}")
        # Row buttons must remain navigation-only — no inline mutations.
        self.assertIn("navigation-only", docs)
        # Focus is one-shot so polling does not re-snap the viewport.
        self.assertIn("one-shot", docs)

    def test_docs_describe_session_storage_preferences(self) -> None:
        # Sessions 2 + 6 persist preferences under wb.* keys and
        # explicitly exclude the reply textarea. Document both the
        # persisted keys and the privacy invariant.
        docs = self._read("docs/agent-session-loop.md")
        for key in (
            "`wb.token`",
            "`wb.filter`",
            "`wb.search`",
            "`wb.sort`",
            "`wb.completed`",
            "`wb.selected`",
        ):
            self.assertIn(key, docs, msg=f"missing sessionStorage key: {key}")
        # Reply textarea privacy regression.
        self.assertIn("reply textarea is intentionally **not** persisted", docs)
        self.assertIn("`wb.reply`", docs)
        # Session 4: cleanup mode reveals completed plans without writing
        # the persisted show-completed preference.
        self.assertIn(
            "Cleanup mode reveals\n  completed plans on its own gate and "
            "never writes `wb.completed`",
            docs,
        )

    def test_docs_smoke_checklist_covers_triage_inbox(self) -> None:
        # The Workbench browser smoke checklist must include operator-
        # verifiable cases for the new triage behavior: default sort +
        # completed hidden, search/sort/completed persistence, two-pane
        # layout, focus-hint navigation, and the reply draft survival
        # invariant.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("`urgency_rank`", docs)
        self.assertIn('"show completed" toggle', docs)
        self.assertIn('`data-state="no-search-results"`', docs)
        self.assertIn("150 ms search debounce", docs)
        self.assertIn("survive via sessionStorage", docs)
        self.assertIn("two-pane shell", docs)
        self.assertIn("`#plan?dir=…&focus=reply`", docs)
        self.assertIn("reply draft", docs)
        # Session 4: completed timestamp visibility and filtered bulk
        # archive selection must be operator-verifiable.
        self.assertIn("completion time", docs)
        self.assertIn('"not recorded"', docs)
        self.assertIn("`select visible`", docs)
        self.assertIn("`select candidates`", docs)
        for recovery_case in (
            "ready (`kind: none`)",
            "running (`kind: none` with active lock)",
            "reply-needed",
            "not-loop-ready",
            "verify-failed",
            "stale-lock or\n  stale-state",
            "commit-failed",
            "complete (`kind: none`)",
        ):
            self.assertIn(
                recovery_case,
                docs,
                msg=f"missing Recovery Center smoke case: {recovery_case}",
            )
        self.assertIn("without executing arbitrary repair commands", docs)

    def test_docs_smoke_checklist_covers_archive_bulk_selection(self) -> None:
        # Session 4: the Workbench browser smoke checklist must include an
        # operator-verifiable Archive view case covering select-all, bulk
        # restore, typed-count purge, clear-selection, and partial-success
        # feedback wording.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("Open the Archive view with several archived plans", docs)
        self.assertIn("`select all`", docs)
        self.assertIn("`N of M entries\n  selected`", docs)
        self.assertIn("`restore selected`", docs)
        self.assertIn("restore best-effort through `POST /api/archive/restore`", docs)
        self.assertIn("`restored X, Y failed` on partial failure", docs)
        self.assertIn("`purge selected`", docs)
        self.assertIn("type the exact count to confirm the typed-count purge", docs)
        self.assertIn("`clear\n  selection`", docs)

    def test_docs_urgency_table_lists_activity_labels(self) -> None:
        # The per-state activity_label column and its zero-pending review
        # continuation override are both documented so external readers can
        # predict the exact UI badge text without consulting workbench_summary_model.py.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("activity_label", docs)
        for (
            primary_state,
            label,
        ) in workbench_summary_model.ACTIVITY_LABEL_BY_STATE.items():
            self.assertIn(
                label,
                docs,
                msg=(
                    f"docs urgency table missing activity_label "
                    f"{label!r} for state {primary_state!r}"
                ),
            )
        self.assertIn(workbench_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL, docs)


if __name__ == "__main__":
    unittest.main()
