"""Source tests extracted from test_workbench_shell."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from asl_workbench import http as workbench_http
from asl_workbench.static_sources import read_static_source

from .workbench_shell_support import (
    BROWSER_HARNESS,
    MODULES_ROOT,
    REPO_ROOT,
    STATIC_ROOT,
)


class ShellSourceTests(unittest.TestCase):
    """One front end, in one place, that a browser can run as it stands."""

    def test_the_shell_has_exactly_one_source_in_the_tree(self) -> None:
        # The consolidation's failure mode is two front ends that drift: a copy
        # under the local server package, or a second entry point beside the
        # first. The bundle both modes serve is the portable one, and there is
        # no other entry module anywhere in this repository.
        self.assertEqual(
            STATIC_ROOT,
            Path(workbench_http.shell_bundle().root),
            "both deployments must serve the bundle rooted at STATIC_ROOT",
        )
        self.assertEqual(
            "asl_workbench",
            STATIC_ROOT.parent.name,
            "the single front-end source belongs to the portable package",
        )
        entries = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in REPO_ROOT.glob("*/static/app.js")
        )
        self.assertEqual(["asl_workbench/static/app.js"], entries)

    def test_the_shell_is_divided_into_modules_rather_than_one_script(
        self,
    ) -> None:
        modules = sorted(path.name for path in MODULES_ROOT.glob("*.js"))
        self.assertGreater(
            len(modules),
            4,
            "the shell should be several cohesive modules, not one script",
        )
        # Nothing is allowed to have grown back into the old single file. The
        # bound is deliberately generous: this is a check against one module
        # quietly becoming the application again, not a style rule.
        for path in [STATIC_ROOT / "app.js", *MODULES_ROOT.glob("*.js")]:
            with self.subTest(module=path.name):
                lines = path.read_text(encoding="utf-8").count("\n")
                self.assertLess(
                    lines,
                    2000,
                    f"{path.name} has grown back towards one undivided script",
                )

    def test_the_shell_runs_with_no_build_step_and_no_runtime_dependency(
        self,
    ) -> None:
        # Native modules, loaded by the browser as served. Any bare specifier
        # would need an import map or a bundler, and either one would make the
        # Pi's deployment depend on a toolchain it does not have.
        index = read_static_source(STATIC_ROOT / "index.html").decode("utf-8")
        self.assertRegex(
            index,
            r'<script\s+type="module"\s+src="/static/app\.js"',
            "index.html must load the entry module as a native ES module",
        )
        specifier = re.compile(
            r'^\s*(?:import|export)\b[^;]*?\bfrom\s+"([^"]+)"', re.MULTILINE
        )
        for path in [STATIC_ROOT / "app.js", *sorted(MODULES_ROOT.glob("*.js"))]:
            source = path.read_text(encoding="utf-8")
            for target in specifier.findall(source):
                with self.subTest(module=path.name, imports=target):
                    self.assertTrue(
                        target.startswith("./") or target.startswith("../"),
                        "a bare specifier needs a bundler or an import map",
                    )
                    self.assertTrue(
                        target.endswith(".js"),
                        "an extensionless specifier is not resolvable by a browser",
                    )
        for path in sorted(STATIC_ROOT.rglob("*")):
            with self.subTest(path=path.name):
                self.assertNotIn(
                    path.name,
                    {"package.json", "node_modules", "package-lock.json"},
                    "the served front end must carry no package manifest",
                )

    def test_the_browser_harness_is_development_only_and_lives_in_scripts(
        self,
    ) -> None:
        # The harness is a test tool, so it lives with the tests and the
        # shipped package must not reach for it. `npm test` has to be the entry
        # point the completion contract runs, which is not what the Companion's
        # own harness named its script.
        manifest = json.loads(
            (BROWSER_HARNESS / "package.json").read_text(encoding="utf-8")
        )
        self.assertEqual("playwright test", manifest["scripts"]["test"])
        self.assertTrue(manifest.get("private"), "a test harness is not published")
        self.assertEqual(
            {},
            manifest.get("dependencies", {}),
            "the harness must add no runtime dependency, only a dev one",
        )
        self.assertIn("@playwright/test", manifest.get("devDependencies", {}))
        self.assertTrue((BROWSER_HARNESS / "shell.spec.js").is_file())
        self.assertTrue((BROWSER_HARNESS / "playwright.config.js").is_file())
        # The shipped packages may *say* the harness covers them -- that is
        # what a coverage reference in the capability matrix is for -- but they
        # may not reach for it. The distinction is the whole point: a named
        # path in a matrix row is documentation, while an import or a spawned
        # `npm` would make a Node toolchain part of what the Pi has to have.
        for package in ("asl_workbench", "agent_session_loop"):
            for path in (REPO_ROOT / package).rglob("*.py"):
                for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if "workbench_browser" not in line:
                        continue
                    with self.subTest(module=path.name, line=number):
                        self.assertIn(
                            '"scripts:tests/workbench_browser/',
                            line,
                            "the shipped packages may cite the harness as "
                            "coverage but must not depend on it",
                        )


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


class ShellAccessibilityTests(unittest.TestCase):
    """What the stylesheet and markup promise a keyboard and a reader."""

    def setUp(self) -> None:
        self.css = read_static_source(STATIC_ROOT / "styles.css").decode("utf-8")
        self.index = read_static_source(STATIC_ROOT / "index.html").decode("utf-8")

    def light_tokens(self) -> dict[str, str]:
        block = re.search(
            r"@media \(prefers-color-scheme: light\) \{\s*:root \{(.*?)\}",
            self.css,
            re.DOTALL,
        )
        assert block is not None
        return dict(re.findall(r"(--wb-[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block[1]))

    def test_theme_tokens_are_declared_once(self) -> None:
        # Two declarations meant the first was dead weight that read as truth.
        self.assertEqual(1, len(re.findall(r"^:root \{", self.css, re.MULTILINE)))
        self.assertEqual(1, self.css.count("@media (prefers-color-scheme: light)"))

    def test_light_mode_text_tokens_meet_aa_on_the_page_background(self) -> None:
        tokens = self.light_tokens()
        for name in ("--wb-text", "--wb-text-dim", "--wb-text-faint", "--wb-warning"):
            with self.subTest(token=name):
                self.assertGreaterEqual(_contrast(tokens[name], tokens["--wb-bg"]), 4.5)

    def test_no_rule_targets_the_retired_recovery_primary_card(self) -> None:
        self.assertNotIn("wb-recovery__primary", self.css)

    def test_the_controls_feedback_is_announced(self) -> None:
        tag = re.search(r'<div\s+id="wb-controls-feedback"[^>]*>', self.index)
        assert tag is not None
        self.assertIn('role="status"', tag[0])
        self.assertIn('aria-live="polite"', tag[0])

    def test_a_queue_row_and_the_plan_heading_take_focus(self) -> None:
        self.assertRegex(self.index, r'<article class="wb-row"[^>]*tabindex="0"')
        self.assertRegex(self.index, r'id="wb-detail-label"[^>]*tabindex="-1"')
        preview = (MODULES_ROOT / "queue_rows.js").read_text(encoding="utf-8")
        self.assertIn('row.addEventListener("keydown"', preview)


class ShellRequestSourceTests(unittest.TestCase):
    """Request behaviour that lives in the shell's own modules."""

    def test_a_refresh_asked_for_mid_read_is_run_afterwards(self) -> None:
        # Dropping it left the queue answering the previous filter's question.
        source = (MODULES_ROOT / "queue.js").read_text(encoding="utf-8")
        self.assertNotIn("if (state.fetchInFlight) return;", source)
        self.assertIn("state.refreshPending = true", source)

    def test_a_launch_names_its_operation_id_as_the_launch_id_too(self) -> None:
        # The local launch and resume routes read `launch_id`; without it a
        # retried launch was a second launch to the Mac.
        source = (MODULES_ROOT / "operation_mutations.js").read_text(encoding="utf-8")
        self.assertIn('const LAUNCH_ID_PATHS = ["/api/launch", "/api/reply"];', source)
        self.assertIn("next.launch_id = operationId;", source)
        self.assertIn("retryableReceipt(spec.action, planId)", source)
