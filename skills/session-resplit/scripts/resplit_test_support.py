#!/usr/bin/env python3
"""Shared plan fixtures and script loading for the resplit session suites."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "resplit-session.py")
SPEC = importlib.util.spec_from_file_location("resplit_session", SCRIPT_PATH)
resplit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resplit
assert SPEC.loader is not None
SPEC.loader.exec_module(resplit)

from agent_session_loop import (  # noqa: E402 - sys.path insert above
    plans_session_ids,
)


def write_session(
    plan_dir: Path,
    session_id: str,
    status: str,
    *,
    title: str = "Test",
    body: str = "Instructions.",
) -> None:
    (plan_dir / plans_session_ids.session_id_to_filename(session_id)).write_text(
        "\n".join(
            [
                f"# Session {session_id}: {title}",
                "",
                body,
                "",
                "## Handoff",
                "",
                f"- **Status**: {status}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        ),
        encoding="utf-8",
    )


def parent_contract_block(check_names: tuple[str, ...] = ("parent-suite",)) -> str:
    checks = [
        {
            "name": name,
            "type": "command",
            "argv": [
                "python3",
                "-m",
                "unittest",
                "-q",
                f"tests.{name.replace('-', '_')}",
            ],
            "timeout_seconds": 120,
        }
        for name in check_names
    ]
    verify_json = json.dumps({"session": "2", "checks": checks}, indent=2)
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "",
            "- Parent deliverables ship together.",
            "",
            "### Verify",
            "",
            "```json",
            *verify_json.splitlines(),
            "```",
        ]
    )


def write_resplit_plan(
    plan_dir: Path,
    *,
    parent_status: str = "pending",
    parent_check_names: tuple[str, ...] | None = ("parent-suite",),
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text(
        "\n".join(
            [
                "# Context",
                "",
                "| Session | Title | Status |",
                "|---|---|---|",
                "| S1 | Setup | complete |",
                f"| S2 | Parent Work | {parent_status} |",
                "| S3 | Wrap | pending |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plan_dir / "manifest.md").write_text(
        "\n".join(
            [
                "# Manifest",
                "",
                "**Sessions**: 3",
                "",
                "| # | Title | Status | File |",
                "|---|---|---|---|",
                "| 1 | Setup | complete | session-1.md |",
                f"| 2 | Parent Work | {parent_status} | session-2.md |",
                "| 3 | Wrap | pending | session-3.md |",
                "",
                "### Dependency Waves",
                "",
                "| Wave | Sessions | Safe concurrent set | Notes |",
                "|---|---|---|---|",
                "| 1 | 1 | none | setup |",
                "| 2 | 2 | none | parent |",
                "| 3 | 3 | none | wrap |",
                "",
                "### Session Ownership",
                "",
                "| Session | Classification | Owned write paths | Required prior "
                "sessions | "
                "Allowed concurrent sessions | Serial blockers |",
                "|---|---|---|---|---|---|",
                "| 1 | serial-required | setup.py | none | none | serial |",
                "| 2 | serial-required | app.py, docs.md | 1 | none | serial |",
                "| 3 | serial-required | wrap.py | 2 (serial work-session sequence) | "
                "none | serial |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_session(plan_dir, "1", "complete", title="Setup")
    write_session(
        plan_dir,
        "2",
        parent_status,
        title="Parent Work",
        body="\n".join(
            [
                "> **Before starting**: Read `context.md` in this directory "
                "for shared project context.",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: `serial-required`",
                "- **Required prior sessions**: 1",
                "- **Allowed concurrent sessions**: none",
                "- **Owned write paths**: app.py, docs.md",
                "- **Forbidden/shared paths**: shared files require coordinator "
                "ownership",
                "- **Serial blockers**: run serially",
                "- **Optional internal tracks**: none",
                "",
                "---",
                "",
                "Implement these deliverables: schema setup, CLI parser, docs update.",
            ]
            + (
                ["", parent_contract_block(parent_check_names)]
                if parent_check_names is not None
                else []
            )
        ),
    )
    write_session(
        plan_dir,
        "3",
        "pending",
        title="Wrap",
        body="\n".join(
            [
                "> **Before starting**: Read `context.md` in this directory "
                "for shared project context.",
                "> **Previous session**: Read the **Handoff** section at the "
                "bottom of `session-2.md`.",
                "",
                "Use parent output.",
            ]
        ),
    )
    (plan_dir / "session-2.expand.md").write_text(
        "historical facts\n", encoding="utf-8"
    )


def add_multi_repo_scopes(plan_dir: Path) -> None:
    manifest_path = plan_dir / "manifest.md"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest.replace(
            "# Manifest\n\n",
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            "| SVC | svc | https://example.com/svc.git |\n"
            "| OAK | oak | https://example.com/oak.git |\n\n",
            1,
        ),
        encoding="utf-8",
    )
    scopes = {
        "1": ("SVC", "(none)"),
        "2": ("SVC, OAK", "SVC"),
        "3": ("OAK", "(none)"),
    }
    for session_id, (reads, writes) in scopes.items():
        path = plan_dir / f"session-{session_id}.md"
        text = path.read_text(encoding="utf-8")
        if session_id == "2":
            text = text.replace(
                '"type": "command",',
                '"type": "command",\n      "repo": "SVC",',
            )
        path.write_text(
            text.replace(
                "\n\n",
                "\n\n"
                f"**Reads repositories**: {reads}\n"
                f"**Writes repositories**: {writes}\n\n",
                1,
            ),
            encoding="utf-8",
        )
