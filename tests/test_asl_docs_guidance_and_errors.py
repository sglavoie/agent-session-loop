"""Guidance delivery and command-error docs regression tests."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agent_session_loop import (
    control_queue,
    mailbox_cli_constants,
    mailbox_constants,
    mailbox_rendering,
)
from tests.asl_docs_support import ASL_GUIDE, ASL_SKILL_MID_RUN_CONTROLS, read_repo_text

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class GuidanceAndErrorDocsTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def test_docs_describe_guidance_rendering_bounds(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        normalized = " ".join(mailbox_docs.split())

        self.assertIn(
            f"newest {mailbox_rendering.MAX_RENDERED_GUIDANCE} pending guidance "
            "records",
            normalized,
        )
        self.assertIn(
            f"{mailbox_rendering.MAX_RENDERED_GUIDANCE_BYTES:,}-byte UTF-8 "
            "rendering budget",
            normalized,
        )
        self.assertIn(
            "records remain pending and unacknowledged",
            normalized,
        )
        self.assertIn(
            "repeats the shell-safe `asl inbox read` command",
            normalized,
        )
        self.assertIn(
            "identifies each omitted record by physical sequence and message ID",
            normalized,
        )
        self.assertIn(
            "cannot fit within the complete subsection on its own is omitted first",
            normalized,
        )
        self.assertIn(
            "evicts the oldest remaining record until the subsection fits",
            normalized,
        )
        self.assertIn(
            "labels the delimited block as intentionally empty",
            normalized,
        )
        self.assertIn(
            "text form of `asl inbox read` applies the same "
            "newest-20-record and 65,536-byte complete-section limits",
            normalized,
        )
        self.assertIn(
            "acknowledgement covers that full snapshot through the highest "
            "pending sequence",
            normalized,
        )
        self.assertIn(
            "The `--json` payload is not rendering-truncated",
            normalized,
        )

    def test_docs_separate_appending_guidance_from_delivering_it(self) -> None:
        """The prose has to carry the distinction the ledger enforces.

        Every operator surface used to say guidance was "sent" and left the
        reader to assume it arrived. What the docs owe a reader now is the
        per-harness truth: which route confirms a delivery, and what an
        unconfirmed one is called instead.
        """
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        normalized = " ".join(mailbox_docs.split())

        for marker in (
            "Appending guidance and delivering it are different events",
            "unsupported-active-steer",
            "delivered-by-read",
            "Guidance delivered live",
            "Guidance queued but not delivered",
            "enforcement remains the job of guards, not guidance",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit guidance-delivery marker: {marker!r}",
            )

    def test_operator_surfaces_never_call_an_append_a_delivery(self) -> None:
        """ "Guidance sent" is the exact claim the ledger cannot support."""
        for path in (
            "README.md",
            ASL_GUIDE,
            "agent_session_loop/README.md",
            "docs/agent-session-loop.md",
            "skills/asl/SKILL.md",
            ASL_SKILL_MID_RUN_CONTROLS,
        ):
            normalized = " ".join(self._read(path).split())
            self.assertNotIn(
                "Guidance sent",
                normalized,
                msg=f"{path} still reports an append as a delivery",
            )

    def test_asl_skill_steer_route_waits_for_a_delivery_verdict(self) -> None:
        # The quick reference keeps the route; the delivery verdicts live in
        # the on-demand mid-run controls reference.
        self.assertIn(
            "asl inbox guidance PLAN_DIR --text GUIDANCE_TEXT --wait-delivered --json",
            " ".join(self._read("skills/asl/SKILL.md").split()),
        )
        normalized = " ".join(self._read(ASL_SKILL_MID_RUN_CONTROLS).split())
        for marker in (
            "Always pass `--wait-delivered`",
            "Guidance delivered live to <harness>",
            "Guidance queued but not delivered (<reason>)",
            "Delivery is never inferred from a successful write to a pipe",
            "never cancels or restarts a turn to deliver guidance",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits guidance-delivery marker: {marker!r}",
            )

    def test_docs_describe_mailbox_and_reply_text_framing(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        normalized = " ".join(mailbox_docs.split())

        self.assertIn(
            "In text mode, `asl inbox read`, `asl ask`, and `asl outbox wait`",
            normalized,
        )
        self.assertIn("`BEGIN ASL MAILBOX MESSAGE JSONL`", normalized)
        self.assertIn(
            "Every reply continuation source—mailbox, `--reply`, or "
            "`--reply-stdin`—uses the same safe prompt representation",
            normalized,
        )
        self.assertIn("`BEGIN ASL OPERATOR REPLY JSONL`", normalized)
        self.assertIn("worker consumes only its `text` value", normalized)

    def test_docs_lock_wait_timeout_and_bare_label_resolution(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        status_docs = docs.split("`status` reads durable plan files", 1)[1].split(
            "\n\n`tail` prints", 1
        )[0]

        self.assertIn(
            "`--wait-timeout` accepts 0 through 1800 seconds and requires `--wait`.",
            mailbox_docs,
        )
        self.assertIn(
            "Supplying `--wait-timeout` alone fails with `reason: invalid-timeout`.",
            mailbox_docs,
        )
        self.assertIn(
            "Bare-label resolution is available to `status`, `asl control`,",
            status_docs,
        )
        for command in (
            "`status`",
            "`asl control`",
            "`asl inbox guidance`",
            "`asl inbox reply`",
            "`asl inbox read`",
            "`asl ask`",
            "`asl notify`",
            "`asl outbox wait`",
        ):
            self.assertIn(command, status_docs)
        self.assertRegex(
            status_docs,
            r"other plan-taking\s+commands remain path-only",
        )
        self.assertNotIn(
            "Resolution is limited to `status` and `asl control`",
            status_docs,
        )

    def test_docs_enumerate_mailbox_command_error_reasons(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("### Mailbox commands", 1)[1].split("\n### ", 1)[0]
        mailbox_cli_paths = sorted(
            Path(mailbox_cli_constants.__file__).parent.glob("mailbox_cli_*.py")
        )
        constants_modules = {
            "mailbox_constants": mailbox_constants,
            "mailbox_cli_constants": mailbox_cli_constants,
        }

        def resolve_reason(node: ast.expr) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in constants_modules
            ):
                value = getattr(constants_modules[node.value.id], node.attr, None)
                return value if isinstance(value, str) else None
            return None

        cli_reasons = {
            reason
            for path in mailbox_cli_paths
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(call, ast.Call)
            for keyword in call.keywords
            if keyword.arg == "reason"
            if (reason := resolve_reason(keyword.value)) is not None
        }
        operation_reasons = {
            value
            for name, value in vars(mailbox_constants).items()
            if name.startswith("ERROR_") and isinstance(value, str)
        }
        expected_reasons = (
            cli_reasons
            | operation_reasons
            | {
                "ambiguous-plan",
                "invalid-target",
                "malformed-plan",
                "os-error",
                "plan-not-found",
                "question-already-answered",
                "question-not-found",
                "state-error",
            }
        )

        # Keep the most important repaired distinctions visible in this
        # regression even though their production constants are also derived.
        self.assertIn("missing-text", expected_reasons)
        self.assertIn("invalid-text-source", expected_reasons)
        self.assertIn("stdin-read-error", expected_reasons)
        for reason in sorted(expected_reasons):
            self.assertIn(
                f"`{reason}`",
                mailbox_docs,
                msg=f"mailbox command reason {reason!r} is undocumented",
            )
        self.assertRegex(
            mailbox_docs,
            r"`missing-text` when a required message body is\s+absent or blank",
        )
        self.assertRegex(
            mailbox_docs,
            r"`invalid-text-source` when both `--text` and\s+`--text-stdin` are "
            r"supplied",
        )

    def test_docs_enumerate_control_command_error_reasons(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        control_docs = docs.split("`asl control` is the programmatic equivalent", 1)[
            1
        ].split("\n`status` reads durable plan files", 1)[0]
        control_cli_paths = sorted(
            Path(control_queue.__file__).parent.glob("control_cli_*.py")
        )

        def resolve_reason(node: ast.expr) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "control_queue"
            ):
                value = getattr(control_queue, node.attr, None)
                return value if isinstance(value, str) else None
            return None

        expected_reasons = {
            reason
            for path in control_cli_paths
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(call, ast.Call)
            for keyword in call.keywords
            if keyword.arg == "reason"
            if (reason := resolve_reason(keyword.value)) is not None
        }

        self.assertIn(
            control_queue.REJECT_MISSING_MODEL_CONTROL_OPERATION,
            expected_reasons,
        )
        for reason in sorted(expected_reasons):
            self.assertIn(
                f"`{reason}`",
                control_docs,
                msg=f"control command reason {reason!r} is undocumented",
            )


if __name__ == "__main__":
    unittest.main()
