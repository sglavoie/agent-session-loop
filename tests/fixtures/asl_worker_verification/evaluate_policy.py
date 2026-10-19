# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""Worker-verification policy surfaces and deterministic fixture digest."""

from evaluate_shared import *  # noqa: F403


def _contract_block(scenario: Scenario) -> str:
    payload = {
        "session": "1",
        "checks": [
            {
                "name": CONTRACT_CHECK_NAME,
                "type": "command",
                "argv": ["python3", "checks/contract.py"],
                "timeout_seconds": 120,
            }
        ],
    }
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            f"- `python3 checks/focused.py {scenario.case}` passes.",
            "",
            "### Verify",
            "",
            "```json",
            json.dumps(payload, indent=2),
            "```",
        ]
    )


def session_text(scenario: Scenario) -> str:
    """Render the trial's one session file.

    Deliberately free of verification policy: the only statements here are the
    task, the evidence, and the contract. Whatever a worker knows about who
    owns the final gate has to have come from the packet the runner built, not
    from this fixture.
    """
    return "\n".join(
        [
            f"# Session 1: {scenario.title}",
            "",
            scenario.body.rstrip(),
            "",
            _contract_block(scenario),
            "",
            "## Handoff",
            "",
            "- **Status**: pending",
            "- **Files modified**: (none yet)",
            "- **Key decisions**: (none yet)",
            "- **Blockers for next session**: (none)",
            "- **Notes**:",
            "",
        ]
    )


# --------------------------------------------------------------------------
# Digests
# --------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _policy_lines(text: str) -> list[str]:
    """Extract the policy-bearing lines from one worker-facing surface.

    A line is policy-bearing when it carries one of :data:`POLICY_ANCHORS`:
    the bullets that bound a repeated or widened check, and the statements that
    assign the final gate to the runner. Only those lines are taken, so
    unrelated prose *elsewhere in the surface* does not age a trial's evidence
    out; prose sharing a line with a policy statement is hashed with it, since
    the line is the unit each surface's authors chose for a paragraph. A
    surface that states nothing yields an empty list, which is itself a policy
    change the digest reports.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        collapsed = " ".join(raw.split())
        if any(anchor in collapsed for anchor in POLICY_ANCHORS):
            lines.append(collapsed)
    return lines


def _rendered_packet(*, is_review: bool) -> str:
    packet = task_packet.SessionTaskPacket(
        harness="claude",
        plan_dir="/plan",
        session_id="1",
        session_path="/plan/session-1.md",
        context_path="/plan/context.md",
        session_kind=(
            plans_payloads.SESSION_KIND_REVIEW
            if is_review
            else plans_payloads.SESSION_KIND_IMPLEMENTATION
        ),
    )
    return packet.render()


def policy_surfaces(candidate_root: Path) -> dict[str, list[str]]:
    """Return the policy text each worker-facing surface currently states.

    A surface that cannot be rendered at all records the failure instead of
    being skipped: an evaluator that quietly dropped a surface would report a
    matching digest for a checkout whose policy it could no longer read.
    """
    surfaces: dict[str, list[str]] = {}
    for name, is_review in (("packet:implementation", False), ("packet:review", True)):
        try:
            surfaces[name] = _policy_lines(_rendered_packet(is_review=is_review))
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            surfaces[name] = [f"<unrenderable: {type(error).__name__}: {error}>"]
    skill_path = candidate_root / "skills" / "session-run" / "SKILL.md"
    try:
        surfaces["skill:session-run"] = _policy_lines(
            skill_path.read_text(encoding="utf-8")
        )
    except OSError as error:
        surfaces["skill:session-run"] = [f"<unreadable: {error}>"]
    return surfaces


def missing_policy_anchors(surfaces: Any) -> dict[str, list[str]]:
    """Return, per surface, the :data:`REQUIRED_POLICY_ANCHORS` it omits.

    Read from a receipt's recorded surfaces rather than from the checkout, so
    it answers what the policy said *when the trial ran*. The digest answers
    the other half — whether that text is still what the checkout says now.
    """
    if not isinstance(surfaces, dict):
        return {}
    missing: dict[str, list[str]] = {}
    for name, lines in surfaces.items():
        recorded = (
            [line for line in lines if isinstance(line, str)]
            if isinstance(lines, list)
            else []
        )
        absent = [
            anchor
            for anchor in REQUIRED_POLICY_ANCHORS
            if not any(anchor in line for line in recorded)
        ]
        if absent:
            missing[str(name)] = absent
    return missing


def policy_digest(surfaces: Mapping[str, Sequence[str]]) -> str:
    canonical = json.dumps(
        {name: list(lines) for name, lines in sorted(surfaces.items())},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(canonical)


def fixture_digest(repo_template: Path | None = None) -> str:
    """Digest the inputs that shape a trial, and nothing else.

    The seeded repository and the scenario definitions decide what a worker
    was asked to do; the validator that reads the results afterwards does not.
    Digesting the validator too would age every receipt out on any later edit
    to this file while proving nothing about the trial.

    *repo_template* exists so the deterministic tests can digest a copy they
    are free to modify; a trial always digests the shipped one.
    """
    template = REPO_TEMPLATE if repo_template is None else repo_template
    parts: dict[str, str] = {}
    for path in sorted(template.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(template).as_posix()
        parts[f"repo/{relative}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    for scenario in SCENARIOS:
        parts[f"scenario/{scenario.key}"] = _sha256_text(
            json.dumps(
                {
                    "case": scenario.case,
                    "defect": list(scenario.defect),
                    "extra_criteria": list(scenario.extra_criteria),
                    "session": session_text(scenario),
                },
                sort_keys=True,
            )
        )
    return _sha256_text(json.dumps(parts, sort_keys=True, separators=(",", ":")))


# --------------------------------------------------------------------------
# Trial execution
# --------------------------------------------------------------------------
