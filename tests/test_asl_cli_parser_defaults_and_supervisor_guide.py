"""Parser defaults and the supervisor-guide / notify / outbox parser contracts."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_session_loop import (
    cli,
    harnesses,
    mailbox_cli,
    run_entry,
    supervisor_guide,
)


class ParserDefaultsAndSupervisorGuideTests(unittest.TestCase):
    """Parser defaults and the supervisor-guide / notify / outbox parser contracts."""

    def test_commit_flags_are_mutually_exclusive(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                [
                    "run",
                    "/tmp/plan",
                    "--cwd",
                    "/tmp/repo",
                    "--commit-per-session",
                    "--commit-at-end",
                ]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_run_and_doctor_default_cwd_to_current_directory(self) -> None:
        parser = cli.build_parser()

        run_args = parser.parse_args(["run", "/tmp/plan"])
        self.assertEqual(".", run_args.cwd)

        doctor_args = parser.parse_args(
            ["doctor", "--harness", "codex", "--plan-dir", "/tmp/plan"]
        )
        self.assertEqual(".", doctor_args.cwd)

    def test_run_and_doctor_default_to_unsafe_bypass_with_opt_out(self) -> None:
        parser = cli.build_parser()

        run_args = parser.parse_args(["run", "/tmp/plan"])
        self.assertTrue(run_args.unsafe_bypass)

        run_explicit_args = parser.parse_args(["run", "/tmp/plan", "--unsafe-bypass"])
        self.assertTrue(run_explicit_args.unsafe_bypass)

        run_safe_args = parser.parse_args(["run", "/tmp/plan", "--no-unsafe-bypass"])
        self.assertFalse(run_safe_args.unsafe_bypass)

        doctor_args = parser.parse_args(
            ["doctor", "--harness", "codex", "--plan-dir", "/tmp/plan"]
        )
        self.assertTrue(doctor_args.unsafe_bypass)

        doctor_safe_args = parser.parse_args(
            [
                "doctor",
                "--harness",
                "codex",
                "--plan-dir",
                "/tmp/plan",
                "--no-unsafe-bypass",
            ]
        )
        self.assertFalse(doctor_safe_args.unsafe_bypass)

    def test_run_mailbox_defaults_active_capable_with_explicit_opt_out(self) -> None:
        parser = cli.build_parser()

        default_args = parser.parse_args(["run", "/tmp/plan"])
        disabled_args = parser.parse_args(["run", "/tmp/plan", "--no-mailbox"])

        self.assertFalse(default_args.no_mailbox)
        self.assertTrue(disabled_args.no_mailbox)
        self.assertIn("no_mailbox", run_entry._RESUME_OVERLAY_DESTS)

    def test_notify_and_outbox_include_parser_contract(self) -> None:
        parser = cli.build_parser()

        notify_args = parser.parse_args(
            [
                "notify",
                "/tmp/plan",
                "checkpoint",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )
        self.assertEqual("checkpoint", notify_args.text)
        self.assertFalse(notify_args.text_stdin)
        self.assertEqual(
            "mailbox_cli:notify_from_args",
            notify_args.handler_ref,
        )
        self.assertIs(
            mailbox_cli.notify_from_args,
            cli.resolve_handler(notify_args),
        )

        stdin_args = parser.parse_args(["notify", "/tmp/plan", "--text-stdin"])
        self.assertIsNone(stdin_args.text)
        self.assertTrue(stdin_args.text_stdin)

        default_wait = parser.parse_args(["outbox", "wait", "/tmp/plan"])
        self.assertEqual("questions", default_wait.include)
        for include in ("questions", "notices", "all"):
            with self.subTest(include=include):
                wait_args = parser.parse_args(
                    [
                        "outbox",
                        "wait",
                        "/tmp/plan",
                        "--include",
                        include,
                    ]
                )
                self.assertEqual(include, wait_args.include)

    def test_outbox_supervise_parser_contract(self) -> None:
        parser = cli.build_parser()
        launch = "3d483756-71fb-4eed-8d10-a1f929dcf623"

        supervise_args = parser.parse_args(
            ["outbox", "supervise", "/tmp/plan", "--supervision-id", launch]
        )
        self.assertEqual(launch, supervise_args.supervision_id)
        self.assertEqual(550.0, supervise_args.timeout)
        self.assertEqual(
            "mailbox_cli:outbox_supervise_from_args",
            supervise_args.handler_ref,
        )
        self.assertIs(
            mailbox_cli.outbox_supervise_from_args,
            cli.resolve_handler(supervise_args),
        )
        # The cursor is persisted and the mode is fixed, so neither knob exists.
        self.assertFalse(hasattr(supervise_args, "since"))
        self.assertFalse(hasattr(supervise_args, "include"))

        for argv in (
            ["outbox", "supervise", "/tmp/plan"],
            ["outbox", "supervise", "/tmp/plan", "--since", "2"],
            ["outbox", "supervise", "/tmp/plan", "--include", "all"],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                parser.parse_args(argv)
            self.assertEqual(2, raised.exception.code)

    def test_supervisor_guide_parser_contract(self) -> None:
        parser = cli.build_parser()

        defaults = parser.parse_args(["supervisor-guide"])
        self.assertEqual("auto", defaults.harness)
        self.assertFalse(defaults.json)
        self.assertEqual(
            "supervisor_guide:supervisor_guide_from_args",
            defaults.handler_ref,
        )
        self.assertIs(
            supervisor_guide.supervisor_guide_from_args,
            cli.resolve_handler(defaults),
        )

        for harness in harnesses.HARNESS_NAMES:
            with self.subTest(harness=harness):
                args = parser.parse_args(
                    ["supervisor-guide", "--harness", harness, "--json"]
                )
                self.assertEqual(harness, args.harness)
                self.assertTrue(args.json)

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            parser.parse_args(["supervisor-guide", "--harness", "nope"])
        self.assertEqual(2, rejected.exception.code)

        root_help = io.StringIO()
        with redirect_stdout(root_help), self.assertRaises(SystemExit) as printed:
            parser.parse_args(["--help"])
        self.assertEqual(0, printed.exception.code)
        self.assertIn("supervisor-guide", root_help.getvalue())

    def test_supervisor_guide_text_and_json_carry_equivalent_commands(self) -> None:
        parser = cli.build_parser()
        handler = cli.resolve_handler(parser.parse_args(["supervisor-guide"]))

        json_out = io.StringIO()
        with redirect_stdout(json_out):
            self.assertEqual(
                0,
                handler(
                    parser.parse_args(
                        ["supervisor-guide", "--harness", "codex", "--json"]
                    )
                ),
            )
        payload = json.loads(json_out.getvalue())

        text_out = io.StringIO()
        with redirect_stdout(text_out):
            self.assertEqual(
                0,
                handler(parser.parse_args(["supervisor-guide", "--harness", "codex"])),
            )
        text = text_out.getvalue()

        self.assertTrue(payload["ok"])
        self.assertEqual("supervisor-guide", payload["command"])
        self.assertEqual("codex", payload["selected_harness"])
        self.assertEqual(
            {"codex": "$asl asl run PLAN_DIR"},
            payload["skills"][0]["invocations"],
        )
        self.assertEqual(
            [
                "supervise",
                "reply",
                "resolve-continue",
                "resolve-pause",
                "guidance",
                "guard",
                "redirect",
                "redirect-status",
                "recover-conversation",
                "acknowledge-external-changes",
                "status",
                "read-result",
            ],
            [operation["name"] for operation in payload["operations"]],
        )
        self.assertIn(
            "--supervision-id SUPERVISION_ID --json --detach",
            payload["helper"]["launch"],
        )
        self.assertIn("--reply-stdin --detach", payload["helper"]["resume"])
        note = payload["helper"]["note"]
        self.assertIn("result_path", note)
        self.assertIn("<skill-dir>", payload["helper"]["path"])
        self.assertIn("<skill-dir> is the installed asl skill directory", note)
        self.assertIn("SKILL.md", note)

        commands = [
            payload["helper"]["launch"],
            payload["helper"]["resume"],
            *(operation["command"] for operation in payload["operations"]),
            *payload["leaf_help"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_supervisor_guide_guidance_route_waits_for_a_delivery_verdict(
        self,
    ) -> None:
        """The generated command has to be the one that reports reality.

        A supervisor who follows the guide verbatim must end up asking whether
        the guidance was delivered, not just whether it was appended -- and the
        description must not promise that guidance can enforce anything.
        """
        payload = supervisor_guide.guide_payload(harness="codex")
        [guidance] = [
            operation
            for operation in payload["operations"]
            if operation["name"] == "guidance"
        ]

        self.assertEqual(
            "asl inbox guidance PLAN_DIR --text GUIDANCE_TEXT --wait-delivered --json",
            guidance["command"],
        )
        self.assertIn("delivered live", guidance["description"])
        self.assertIn("queued but not delivered", guidance["description"])
        self.assertIn("only a guard enforces", guidance["description"])
        self.assertNotIn("sent", guidance["description"])
        self.assertIn(guidance["command"], supervisor_guide.render_text(payload))

    def test_supervisor_guide_offers_the_enforcing_lever_next_to_the_advisory_one(
        self,
    ) -> None:
        """Guidance and guards sit together, and say which one enforces.

        A supervisor reading only this route has to be able to tell the two
        apart without being told the third thing -- that a guard still does not
        cancel the turn in flight.
        """
        payload = supervisor_guide.guide_payload(harness="codex")
        names = [operation["name"] for operation in payload["operations"]]
        self.assertEqual(names.index("guidance") + 1, names.index("guard"))
        [guard] = [
            operation
            for operation in payload["operations"]
            if operation["name"] == "guard"
        ]

        self.assertIn("--deny-command", guard["command"])
        self.assertIn("--protect-path", guard["command"])
        self.assertIn("--wait", guard["command"])
        self.assertIn("turn-start", guard["description"])
        self.assertIn("asl control abort", guard["description"])
        self.assertIn(guard["command"], supervisor_guide.render_text(payload))

    def test_supervisor_guide_selects_or_offers_every_harness_candidate(self) -> None:
        detected = supervisor_guide.guide_payload(env={"CLAUDECODE": "1"})
        self.assertEqual("claude", detected["selected_harness"])
        self.assertEqual(
            {"claude": "/asl asl run PLAN_DIR"},
            detected["skills"][0]["invocations"],
        )

        explicit = supervisor_guide.guide_payload(
            harness="copilot", env={"CLAUDECODE": "1"}
        )
        self.assertEqual("copilot", explicit["selected_harness"])
        self.assertEqual(
            {"copilot": "/asl asl run PLAN_DIR"},
            explicit["skills"][0]["invocations"],
        )

        for label, env in (
            ("ambiguous", {"CLAUDECODE": "1", "CODEX_HOME": "/tmp"}),
            ("undetected", {}),
        ):
            with self.subTest(detection=label):
                payload = supervisor_guide.guide_payload(env=env)
                invocations = payload["skills"][0]["invocations"]

                self.assertIsNone(payload["selected_harness"])
                self.assertEqual(list(harnesses.HARNESS_NAMES), list(invocations))
                for name, invocation in invocations.items():
                    self.assertTrue(
                        invocation.startswith(harnesses.skill_prefixes()[name]),
                        invocation,
                    )


if __name__ == "__main__":
    unittest.main()
