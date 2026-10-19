from .support_base import *


def _load_script_module(module_name: str, *relative_parts: str):
    script_path = REPO_ROOT.joinpath(*relative_parts)
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_prepare_plan(plan_dir: Path, session_ids: tuple[str, ...] = ("1",)) -> None:
    """Write a minimal canonical plan for prepare/expansion tests."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    for session_id in session_ids:
        body = "\n".join(
            [
                f"# {plans_session_ids.session_id_display_label(session_id)}: Test",
                "",
                authoring_render.CONTEXT_REFERENCE_LINE,
                "",
                "---",
                "",
                "Update the retry helper in `foo/bar.py` to use `load_config`.",
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- The helper in `foo/bar.py` passes tests.",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": session_id,
                        "checks": [{"name": "c1", "type": "command", "argv": ["true"]}],
                    }
                ),
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Files modified**: (none yet)",
                "- **Key decisions**: (none yet)",
                "- **Blockers for next session**: (none)",
                "- **Notes**: remember `foo/bar.py` cleanup",
                "",
            ]
        )
        (plan_dir / plans_session_ids.session_id_to_filename(session_id)).write_text(
            body, encoding="utf-8"
        )


def _validator_payload(**session_overrides: object) -> dict:
    """A minimal valid validator payload for one session, with overrides."""
    session: dict[str, Any] = {
        "session_id": "1",
        "title": "Test",
        "claims": [
            {
                "claim": "load_config",
                "claim_type": "S",
                "location": "foo/config.py",
                "line": 12,
                "to_be_created": False,
            }
        ],
        "issues": [],
        "details": [],
        "patterns": [],
        "dependencies": [],
        "contract_checked": True,
        "contract_problems": [],
        "corrections": [],
    }
    session.update(session_overrides)
    return {
        "version": prepare_models.LEGACY_PREPARE_VALIDATION_VERSION,
        "codebase_root": "/repo",
        "sessions": [session],
    }


def _write_prepare_source_plan(
    path: Path,
    *,
    session_count: int = 2,
    omit_contract_for: tuple[str, ...] = (),
    check_json_by_id: dict[str, dict] | None = None,
    body_by_id: dict[str, str] | None = None,
    heading_by_id: dict[str, str] | None = None,
) -> Path:
    """Write a source plan file with explicit sessions and inline contracts."""
    lines = ["# Prepare Demo Plan", "", "Shared context for the demo plan.", ""]
    for number in range(1, session_count + 1):
        session_id = str(number)
        heading = (heading_by_id or {}).get(
            session_id, f"## Session {session_id}: Step {session_id}"
        )
        lines.extend([heading, ""])
        body = (body_by_id or {}).get(
            session_id, f"Implement step {session_id} in `demo/module.py`."
        )
        lines.extend([body, ""])
        if session_id in omit_contract_for:
            continue
        check = (check_json_by_id or {}).get(
            session_id,
            {
                "name": f"check-{session_id}",
                "type": "command",
                "argv": ["python3", "-c", "raise SystemExit(0)"],
                "timeout_seconds": 30,
            },
        )
        lines.extend(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                f"- Step {session_id} outcome holds.",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps({"session": session_id, "checks": [check]}),
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _prepare_pipeline_payload(
    codebase_root: str,
    session_ids: tuple[str, ...],
    *,
    corrections_by_id: dict[str, list[dict]] | None = None,
    version: int = prepare_models.LEGACY_PREPARE_VALIDATION_VERSION,
    repositories: tuple[prepare_models.ValidatorRepository, ...] = (),
    session_repositories: tuple[
        tuple[prepare_models.ValidatorRepository, ...], ...
    ] = (),
) -> dict:
    """A schema-valid validator payload covering the given sessions."""
    sessions = []
    for index, session_id in enumerate(session_ids):
        repo = (
            session_repositories[index][0].alias
            if version == prepare_models.PREPARE_VALIDATION_VERSION
            else None
        )
        claim = {
            "claim": "demo/module.py",
            "claim_type": "P",
            "location": "demo/module.py",
            "line": 1,
            "to_be_created": False,
        }
        corrections = [
            dict(correction)
            for correction in (corrections_by_id or {}).get(session_id, [])
        ]
        if repo is not None:
            claim["repo"] = repo
            for correction in corrections:
                correction["repo"] = repo
        sessions.append(
            {
                "session_id": session_id,
                "title": f"Step {session_id}",
                "claims": [claim],
                "issues": [],
                "details": [],
                "patterns": [],
                "dependencies": [],
                "contract_checked": True,
                "contract_problems": [],
                "corrections": corrections,
            }
        )
    payload = {"version": version, "sessions": sessions}
    if version == prepare_models.PREPARE_VALIDATION_VERSION:
        payload["workspace_root"] = codebase_root
        payload["repositories"] = [repo.to_dict() for repo in repositories]
    else:
        payload["codebase_root"] = codebase_root
    return payload


def _recording_validator(
    calls: list,
    *,
    corrections_by_id: dict[str, list[dict]] | None = None,
    payload_override: object | None = None,
):
    """A fake validator that records packets and returns valid payloads."""

    def _validator(packet: prepare_packet.ValidationTaskPacket) -> object:
        calls.append(packet)
        if payload_override is not None:
            return payload_override
        return _prepare_pipeline_payload(
            packet.codebase_root,
            packet.session_ids,
            corrections_by_id=corrections_by_id,
            version=packet.version,
            repositories=packet.repositories,
            session_repositories=packet.session_read_repositories,
        )

    return _validator
