"""Canonical supervisor workflow guide behind ``asl supervisor-guide``.

One structured model renders both the text and the JSON form, so an agent that
reads either surface receives the same commands. Only ``harnesses`` and
``responses`` are imported: the command stays light enough to be resolved
lazily from ``cli.py`` without pulling in the runner or mailbox subsystems.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from . import harnesses, responses

COMMAND_NAME = "supervisor-guide"

AUTO_HARNESS = "auto"

SKILL_NAME = "asl"

HELPER_PATH = "<skill-dir>/scripts/asl-autopilot.py"

PLAN_DIR = "PLAN_DIR"

RUN_COMMAND = f"asl run {PLAN_DIR}"

SUPERVISION_ID = "SUPERVISION_ID"

QUESTION_ID = "QUESTION_ID"

SESSION_ID = "SESSION_ID"

PLACEHOLDERS: tuple[str, ...] = (
    PLAN_DIR,
    SUPERVISION_ID,
    QUESTION_ID,
    SESSION_ID,
    "NEW_SUPERVISION_ID",
    "REPLY_TEXT",
    "GUIDANCE_TEXT",
    "REDIRECT_TEXT",
    "RESULT_PATH",
)

HANDSHAKE_KEYS: tuple[str, ...] = (
    "ok",
    "supervision_id",
    "pid",
    "plan_dir",
    "result_path",
    "stderr_path",
)

SUPERVISE_TOKENS: tuple[str, ...] = (
    "IDLE",
    "ACTION questions=<n>",
    "DONE outcome=<outcome> seq=<n>",
    "LOST reason=<reason>",
)

LEAF_HELP: tuple[str, ...] = (
    "asl outbox supervise --help",
    "asl inbox reply --help",
    "asl inbox resolve --help",
    "asl inbox guidance --help",
    "asl control abort --help",
    "asl control redirect --help",
    "asl control redirect-status --help",
    "asl control recover-conversation --help",
    "asl bridge serve --help",
)

#: The one operation in this guide a supervising agent may not decide to use.
#: Named here rather than only in prose so the JSON reader sees the same gate
#: the text reader does.
REDIRECT_AUTHORIZATION = (
    "urgent intervention: use only when the user has explicitly authorized this "
    "correction. Never self-authorize from a supervision signal, a failing "
    "check, or a suspicion that the worker is wrong."
)


def _selected_harness(
    harness: str,
    env: Mapping[str, str] | None,
) -> harnesses.HarnessName | None:
    if harness == AUTO_HARNESS:
        return harnesses.detect_invoking_harness(env)
    for name in harnesses.HARNESS_NAMES:
        if harness == name:
            return name
    permitted = ", ".join((AUTO_HARNESS, *harnesses.HARNESS_NAMES))
    raise ValueError(f"harness must be one of: {permitted}; got {harness!r}")


def _skill_invocations(
    selected: harnesses.HarnessName | None,
    env: Mapping[str, str] | None,
) -> dict[harnesses.HarnessName, str]:
    invocations = harnesses.skill_invocations(SKILL_NAME, RUN_COMMAND, env=env)
    if selected is None:
        return invocations
    return {selected: invocations[selected]}


def _helper_payload() -> dict[str, Any]:
    return {
        "path": HELPER_PATH,
        "supervision_id_command": "uuidgen",
        "launch": (
            f"{HELPER_PATH} --command '{RUN_COMMAND}' "
            f"--supervision-id {SUPERVISION_ID} --json --detach"
        ),
        "resume": (
            f"printf '%s' 'REPLY_TEXT' | {HELPER_PATH} --command '{RUN_COMMAND}' "
            "--supervision-id NEW_SUPERVISION_ID --json --reply-stdin --detach"
        ),
        "handshake_keys": list(HANDSHAKE_KEYS),
        "note": (
            "<skill-dir> is the installed asl skill directory, the one holding "
            "its SKILL.md. Mint one fresh supervision id per launch. The "
            "detached helper's verdict is the result.json at the handshake's "
            "result_path, never the launching turn's stdout."
        ),
    }


def _operations_payload() -> list[dict[str, str]]:
    tokens = ", ".join(SUPERVISE_TOKENS)
    return [
        {
            "name": "supervise",
            "description": (
                f"Long-poll the mailbox and print one of {tokens}. Reissue the "
                "identical command after IDLE; DONE and LOST both end the loop."
            ),
            "command": (
                f"asl outbox supervise {PLAN_DIR} --supervision-id {SUPERVISION_ID}"
            ),
        },
        {
            "name": "reply",
            "description": (
                "Answer an open worker question with a factual reply and "
                "confirm the worker consumed it."
            ),
            "command": (
                f"asl inbox reply {PLAN_DIR} --to {QUESTION_ID} --text REPLY_TEXT "
                "--wait-consumed --wait-timeout 300 --json"
            ),
        },
        {
            "name": "resolve-continue",
            "description": (
                "Close a plainly nonblocking question without supplying an "
                "answer; the worker resumes the same session."
            ),
            "command": (
                f"asl inbox resolve {PLAN_DIR} --to {QUESTION_ID} "
                "--action continue --wait-consumed --wait-timeout 300 --json"
            ),
        },
        {
            "name": "resolve-pause",
            "description": (
                "Turn a live wait into the ordinary terminal paused-run recovery path."
            ),
            "command": (
                f"asl inbox resolve {PLAN_DIR} --to {QUESTION_ID} "
                "--action pause --wait-consumed --wait-timeout 300 --json"
            ),
        },
        {
            "name": "guidance",
            "description": (
                "Steer implementation choices inside the selected session. "
                "Guidance is advice, not a guard: it cannot stop a run or "
                "change the session's scope or contract. The reply reports "
                "either 'delivered live' to the running turn or 'queued but "
                "not delivered', in which case it rides the next turn's "
                "prompt; only a guard enforces."
            ),
            "command": (
                f"asl inbox guidance {PLAN_DIR} --text GUIDANCE_TEXT "
                "--wait-delivered --json"
            ),
        },
        {
            "name": "guard",
            "description": (
                "Enforce, rather than advise. --deny-command blocks an action "
                "such as 'git push'; --protect-path freezes a file at its "
                "turn-start state and rejects the turn that changed it, "
                "retaining the change. Neither cancels the running turn -- "
                "only asl control abort does that."
            ),
            "command": (
                f"asl control guard add {PLAN_DIR} --deny-command 'CMD ARG ...' "
                "--protect-path ALIAS:PATH --reason TEXT --wait --json"
            ),
        },
        {
            "name": "redirect",
            "description": (
                f"{REDIRECT_AUTHORIZATION} Unlike guidance, this interrupts the "
                "turn in flight: an accepted correction stops the worker and "
                "the runner continues the session on the predecessor's "
                "conversation. Submission is at-most-once -- a timeout reports "
                "the submission uncertain rather than retracting it, and "
                "resubmitting the same --operation-id with the same text is "
                "the same request, never a second one."
            ),
            "command": (
                f"asl control redirect {PLAN_DIR} --text REDIRECT_TEXT "
                f"--session {SESSION_ID} --json"
            ),
        },
        {
            "name": "redirect-status",
            "description": (
                "Read what the redirect journal actually records: whether a "
                "correction was delivered, and whether the worker receipted "
                "it. The only way to settle an uncertain submission; watching "
                "is observational, so a timeout retracts nothing."
            ),
            "command": (
                f"asl control redirect-status {PLAN_DIR} --wait-acknowledged --json"
            ),
        },
        {
            "name": "recover-conversation",
            "description": (
                "Read why a run paused conversation-recovery-required and "
                "print the one-use relaunch that authorizes a fresh "
                "conversation. A read only: discarding an agent's saved "
                "reasoning is the user's decision, so run the printed "
                "asl run ... --fresh-conversation yourself only when the user "
                "says to. Supervision never resumes this pause on its own, and "
                "the autopilot helper refuses a command carrying "
                "--fresh-conversation outright."
            ),
            "command": f"asl control recover-conversation {PLAN_DIR} --json",
        },
        {
            "name": "status",
            "description": (
                "Recover run state after DONE outcome=error or LOST, and "
                "before treating a plan as idle."
            ),
            "command": f"asl status {PLAN_DIR} --json",
        },
        {
            "name": "read-result",
            "description": (
                "Read the detached helper's verdict from the handshake's "
                "result_path once DONE ends the loop; fall back to asl status "
                "when the outcome was error."
            ),
            "command": "cat RESULT_PATH",
        },
    ]


def guide_payload(
    *,
    harness: str = AUTO_HARNESS,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the canonical supervisor guide model.

    ``harness`` is either an explicit harness name or ``auto``. Detection that
    finds nothing or is ambiguous keeps ``selected_harness`` null and offers
    every supported harness candidate rather than failing.
    """
    selected = _selected_harness(harness, env)
    return responses.success_payload(
        COMMAND_NAME,
        selected_harness=selected,
        skills=[
            {
                "skill": SKILL_NAME,
                "description": (
                    "Supervise one asl run end to end from a harness agent, "
                    "passing the exact asl run command as the skill argument."
                ),
                "invocations": _skill_invocations(selected, env),
            }
        ],
        helper=_helper_payload(),
        operations=_operations_payload(),
        leaf_help=list(LEAF_HELP),
        placeholders=list(PLACEHOLDERS),
    )


def render_text(payload: Mapping[str, Any]) -> str:
    """Render the guide model as text carrying every command the JSON carries."""
    selected = payload["selected_harness"]
    lines = ["ASL supervisor guide", ""]
    if selected is None:
        lines.append("Harness: undetected or ambiguous - every supported harness shown")
    else:
        lines.append(f"Harness: {selected}")
    for skill in payload["skills"]:
        lines.extend(["", f"Start supervision ({skill['description']})"])
        for harness_name, invocation in skill["invocations"].items():
            lines.append(f"  {harness_name}: {invocation}")

    helper = payload["helper"]
    lines.extend(
        [
            "",
            "Launch the helper directly",
            f"  helper: {helper['path']}",
            f"  supervision id: {helper['supervision_id_command']}",
            f"  launch: {helper['launch']}",
            f"  resume: {helper['resume']}",
            f"  handshake keys: {', '.join(helper['handshake_keys'])}",
            f"  {helper['note']}",
        ]
    )

    lines.extend(["", "Supervise and answer"])
    for operation in payload["operations"]:
        lines.append(f"  {operation['name']}: {operation['description']}")
        lines.append(f"    {operation['command']}")

    lines.extend(
        [
            "",
            "Leaf help (these groups list leaves only; asl workbench is the "
            "exception and shows its serve flags on the parent)",
        ]
    )
    lines.extend(f"  {command}" for command in payload["leaf_help"])

    lines.extend(["", f"Placeholders: {', '.join(payload['placeholders'])}"])
    return "\n".join(lines)


def supervisor_guide_from_args(args: argparse.Namespace) -> int:
    payload = guide_payload(harness=args.harness)
    if args.json:
        responses.print_json(payload)
    else:
        print(render_text(payload))
    return 0
