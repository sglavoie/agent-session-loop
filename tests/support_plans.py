from .support_base import *


def write_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    contracts_by_id = contracts_by_id or {}
    for number, status_value in enumerate(statuses, start=1):
        contract_block = contracts_by_id.get(str(number), "")
        body = [
            f"# Session {number}: Test",
            "",
            "Instructions.",
            "",
        ]
        if contract_block:
            body.extend([contract_block, ""])
        body.extend(
            [
                "## Handoff",
                "",
                f"- **Status**: {status_value}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        )
        (plan_dir / f"session-{number}.md").write_text(
            "\n".join(body),
            encoding="utf-8",
        )


def write_plan_with_ids(
    plan_dir: Path,
    statuses: tuple[tuple[str, str], ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    contracts_by_id = contracts_by_id or {}
    for session_id, status_value in statuses:
        display_label = plans_session_ids.session_id_display_label(session_id)
        contract_block = contracts_by_id.get(session_id, "")
        body = [
            f"# {display_label}: Test",
            "",
            "Instructions.",
            "",
        ]
        if contract_block:
            body.extend([contract_block, ""])
        body.extend(
            [
                "## Handoff",
                "",
                f"- **Status**: {status_value}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        )
        (plan_dir / plans_session_ids.session_id_to_filename(session_id)).write_text(
            "\n".join(body),
            encoding="utf-8",
        )


def install_fake_codex(tmp_dir: Path) -> Path:
    return install_fake_harness(tmp_dir, "codex")


def install_fake_harness(tmp_dir: Path, executable_name: str) -> Path:
    fake_path = tmp_dir / "codex"
    if executable_name != "codex":
        fake_path = tmp_dir / executable_name
    shutil.copyfile(FAKE_CODEX_FIXTURE, fake_path)
    for support_path in FAKE_CODEX_FIXTURE.parent.glob("fake_codex_*.py"):
        shutil.copyfile(support_path, tmp_dir / support_path.name)
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path


def install_fake_amx(tmp_dir: Path) -> tuple[Path, Path]:
    fake_path = tmp_dir / "amx"
    log_path = tmp_dir / "fake-amx.jsonl"
    fake_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                "import json",
                "import os",
                "import sys",
                "import uuid",
                "",
                "log_path = os.environ.get('FAKE_AMX_LOG')",
                "if log_path:",
                "    Path(log_path).parent.mkdir(parents=True, exist_ok=True)",
                "    with Path(log_path).open('a', encoding='utf-8') as handle:",
                "        handle.write(json.dumps({",
                "            'argv': sys.argv,",
                "            'cwd': os.getcwd(),",
                "            'stdin': sys.stdin.read(),",
                "        }, separators=(',', ':')) + '\\n')",
                "if '--json' in sys.argv:",
                "    request_id = ''",
                "    if '--request-id' in sys.argv:",
                "        request_id = sys.argv[sys.argv.index('--request-id') + 1]",
                "    print(json.dumps({",
                "        'schema_version': 1,",
                "        'status': 'accepted',",
                "        'retryable': False,",
                "        'reason': 'accepted',",
                "        'trace_id': str(uuid.uuid4()),",
                "        'request_id': request_id,",
                "    }, separators=(',', ':')))",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path, log_path


#: Generation bound the commit-timeout probes drive the fake harness with. It
#: is a hang guard, not a performance assertion: the bound has to outlast the
#: whole cold start it races -- an `asl commit` subprocess that stages, captures
#: the diff and only then spawns the fixture, which starts an interpreter of its
#: own before publishing the grandchild PID the probes read. That costs seconds
#: rather than milliseconds while the sharded suite saturates the machine, and
#: too tight a bound kills the process tree before the PID is published, which
#: reads as a flaky test rather than as the overload it is.
GENERATION_TIMEOUT_PROBE_SECONDS = 15
