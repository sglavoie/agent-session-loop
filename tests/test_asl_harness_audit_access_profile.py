"""Audit access profile handling."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_session_loop import (
    harness_codex,
    harnesses,
)

ASL_TEST_LANE = "integration"


class AuditAccessProfileTests(unittest.TestCase):
    """The read-only profile expressed in argv, per adapter.

    Real sandbox enforcement lives inside the installed harnesses and cannot be
    exercised offline. What is testable — and what actually decides whether the
    boundary holds — is that every audit invocation asks for the restricted
    mode, names no writable root, and carries no widening flag.
    """

    #: Flags that widen a harness past its default sandbox, by any spelling.
    BYPASS_TOKENS = (
        "--dangerously-bypass-approvals-and-sandbox",
        "--allow-all-tools",
        "--allow-all-paths",
        "bypassPermissions",
        "workspace-write",
        "dontAsk",
    )

    def _audit_argv(self, name: str, read_roots: tuple[Path, ...]) -> list[str]:
        adapter = harnesses.get_adapter(name)
        # Codex states the audit profile over the app-server protocol now, so
        # its *argv* boundary is the fallback transport's argv; the protocol
        # side is asserted separately against the turn plan.
        builder = getattr(adapter, "build_exec_argv", adapter.build_argv)
        return builder(
            cwd=Path("/audit"),
            plan_dir=None,
            prompt="audit",
            last_message_path=Path("/audit/last.txt"),
            access=harnesses.audit_read_only_access(read_roots),
        )

    def _audit_access_text(self, name: str, read_roots: tuple[Path, ...]) -> str:
        """Everything that describes one audit turn's access, on either transport.

        Argv stopped being the whole story once a harness could be driven over
        a protocol, so a bypass check that read argv alone would pass
        vacuously for codex. This renders argv *and* the turn plan, which is
        where the sandbox, the approval policy, and the writable roots now
        live.
        """
        adapter = harnesses.get_adapter(name)
        invocation = adapter.build_invocation(
            cwd=Path("/audit"),
            plan_dir=None,
            prompt="audit",
            last_message_path=Path("/audit/last.txt"),
            access=harnesses.audit_read_only_access(read_roots),
        )
        rendered = "\n".join(invocation.argv)
        if invocation.turn_plan is not None:
            rendered += "\n" + json.dumps(
                invocation.turn_plan.to_payload(), sort_keys=True
            )
        return rendered

    def _granted_dirs(self, argv: list[str]) -> list[str]:
        return [
            argv[index + 1] for index, token in enumerate(argv) if token == "--add-dir"
        ]

    def test_worker_argv_grants_exactly_the_access_it_always_did(self) -> None:
        # Claude's argv gained ``--input-format stream-json`` and lost its
        # prompt when the turn became a conversation, and copilot's gained
        # ``--acp`` and lost its prompt for the same reason, so this is no
        # longer a byte-for-byte identity test for either harness. Every
        # access-bearing token is unchanged, which is what the assertion is
        # actually for.
        expected = {
            "claude": [
                "claude",
                "--print",
                "--verbose",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--input-format",
                "stream-json",
                "--replay-user-messages",
                "--add-dir",
                "/plan",
                "--permission-mode",
                "dontAsk",
            ],
            "copilot": [
                "copilot",
                "-C",
                "/work",
                "--add-dir",
                "/plan",
                "--no-ask-user",
                "--silent",
                "--acp",
            ],
        }

        for name, argv in expected.items():
            with self.subTest(harness=name):
                self.assertEqual(
                    argv,
                    harnesses.get_adapter(name).build_argv(
                        cwd=Path("/work"),
                        plan_dir=Path("/plan"),
                        prompt="prompt",
                        last_message_path=Path("/work/last.txt"),
                    ),
                )

    def test_claude_worker_prompt_never_reaches_argv(self) -> None:
        adapter = harnesses.get_adapter("claude")
        invocation = adapter.build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt="prompt",
            last_message_path=Path("/work/last.txt"),
        )

        # Under streaming input the prompt is the conversation's first stdin
        # message, so it must not appear in a process listing at all.
        self.assertNotIn("prompt", invocation.argv)
        self.assertEqual(harnesses.TRANSPORT_STREAM_JSON, invocation.transport)

    def test_audit_argv_never_carries_a_bypass_flag(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                argv = self._audit_argv(name, (Path("/evidence"),))
                described = self._audit_access_text(name, (Path("/evidence"),))

                for token in self.BYPASS_TOKENS:
                    self.assertNotIn(token, argv)
                    self.assertNotIn(token, described)

    def test_codex_worker_turns_run_over_the_app_server(self) -> None:
        adapter = harnesses.get_adapter("codex")

        invocation = adapter.build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt="prompt",
            last_message_path=Path("/work/last.txt"),
        )

        # The prompt is no longer a command-line argument at all, so it no
        # longer appears in any process listing.
        self.assertEqual(["codex", "app-server"], invocation.argv)
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, invocation.transport)
        self.assertIsNotNone(invocation.turn_plan)
        assert invocation.turn_plan is not None
        plan = invocation.turn_plan.to_payload()
        self.assertEqual(
            {
                "cwd": "/work",
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "config": {"sandbox_workspace_write": {"writable_roots": ["/plan"]}},
            },
            plan["thread_params"],
        )
        self.assertEqual("workspaceWrite", plan["expected_sandbox"])

    def test_codex_worker_argv_still_carries_the_legacy_fallback_turn(self) -> None:
        self.assertEqual(
            [
                "codex",
                "exec",
                "-C",
                "/work",
                "--add-dir",
                "/plan",
                "-s",
                "workspace-write",
                "-o",
                "/work/last.txt",
                "prompt",
            ],
            harness_codex.CodexAdapter().build_exec_argv(
                cwd=Path("/work"),
                plan_dir=Path("/plan"),
                prompt="prompt",
                last_message_path=Path("/work/last.txt"),
            ),
        )

    def test_codex_unsafe_bypass_reaches_the_app_server_as_full_access(self) -> None:
        invocation = harnesses.get_adapter("codex").build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt="prompt",
            unsafe_bypass=True,
            last_message_path=Path("/work/last.txt"),
        )

        assert invocation.turn_plan is not None
        plan = invocation.turn_plan.to_payload()
        self.assertEqual("danger-full-access", plan["thread_params"]["sandbox"])
        self.assertEqual("dangerFullAccess", plan["expected_sandbox"])

    def test_codex_audits_run_in_its_read_only_sandbox(self) -> None:
        argv = self._audit_argv("codex", (Path("/evidence"), Path("/skills")))
        invocation = harnesses.get_adapter("codex").build_invocation(
            cwd=Path("/audit"),
            plan_dir=None,
            prompt="audit",
            last_message_path=Path("/audit/last.txt"),
            access=harnesses.audit_read_only_access(
                (Path("/evidence"), Path("/skills"))
            ),
        )

        assert invocation.turn_plan is not None
        plan = invocation.turn_plan.to_payload()
        self.assertEqual("read-only", plan["thread_params"]["sandbox"])
        self.assertEqual("readOnly", plan["expected_sandbox"])
        # No writable root of any kind: the read-only sandbox already reads
        # anywhere, so the audit's read roots need no grant at all.
        self.assertNotIn("config", plan["thread_params"])
        # The fallback transport states the same profile the old way.
        self.assertEqual(["-s", "read-only"], argv[4:6])
        self.assertIn("--skip-git-repo-check", argv)
        # For codex ``--add-dir`` grants *write* access, so read roots must not
        # be passed through it.
        self.assertNotIn("--add-dir", argv)

    def test_claude_audits_expose_only_read_and_search_tools(self) -> None:
        argv = self._audit_argv("claude", (Path("/evidence"), Path("/skills")))

        mode = argv.index("--permission-mode")
        self.assertEqual(["--permission-mode", "plan"], argv[mode : mode + 2])
        self.assertEqual(
            ["--tools", "Read,Glob,Grep", "--allowed-tools", "Read,Glob,Grep"],
            argv[mode + 2 : mode + 6],
        )
        self.assertEqual(
            [
                "Bash",
                "Edit",
                "Write",
                "NotebookEdit",
                "WebFetch",
                "WebSearch",
                "Task",
                "TodoWrite",
            ],
            argv[argv.index("--disallowed-tools") + 1].split(","),
        )
        for flag in (
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(["/evidence", "/skills"], self._granted_dirs(argv))

    def test_copilot_audits_deny_write_shell_and_url_access(self) -> None:
        argv = self._audit_argv("copilot", (Path("/evidence"), Path("/skills")))

        self.assertIn("--available-tools=view,rg,glob", argv)
        for denied in ("write", "shell", "url"):
            self.assertIn(f"--deny-tool={denied}", argv)
        for flag in (
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-remote",
            "--no-remote-export",
            "--disallow-temp-dir",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(["/evidence", "/skills"], self._granted_dirs(argv))

    def test_an_audit_invocation_refuses_worker_grants(self) -> None:
        adapter = harnesses.get_adapter("codex")
        audit = harnesses.audit_read_only_access((Path("/evidence"),))

        with self.assertRaises(harnesses.HarnessError) as bypass:
            adapter.build_invocation(
                cwd=Path("/audit"),
                plan_dir=None,
                prompt="audit",
                unsafe_bypass=True,
                last_message_path=Path("/audit/last.txt"),
                access=audit,
            )
        with self.assertRaises(harnesses.HarnessError) as writable:
            adapter.build_invocation(
                cwd=Path("/audit"),
                plan_dir=Path("/plan"),
                prompt="audit",
                last_message_path=Path("/audit/last.txt"),
                access=audit,
            )

        self.assertIn("unsafe bypass", str(bypass.exception))
        self.assertIn("writable plan_dir", str(writable.exception))

    def test_a_harness_that_cannot_enforce_the_profile_refuses_to_build(
        self,
    ) -> None:
        self.addCleanup(
            setattr, harness_codex.CodexAdapter, "enforces_audit_read_only", True
        )
        harness_codex.CodexAdapter.enforces_audit_read_only = False

        with self.assertRaises(harnesses.HarnessError) as refused:
            harnesses.get_adapter("codex").build_invocation(
                cwd=Path("/audit"),
                plan_dir=None,
                prompt="audit",
                last_message_path=Path("/audit/last.txt"),
                access=harnesses.audit_read_only_access((Path("/evidence"),)),
            )

        self.assertIn("cannot enforce", str(refused.exception))
        self.assertEqual(
            "profile-unenforceable",
            harnesses.audit_read_only_unavailable_reason("codex"),
        )
