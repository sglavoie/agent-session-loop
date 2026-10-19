from .asl_live_redirect_support import *


def first_turn_prompt(secret: str, nonce: str) -> str:
    """The synthetic prompt for the turn that will be interrupted."""
    return "\n".join(
        [
            "You are the subject of an automated acceptance check. You are "
            "running inside a disposable throwaway git repository created for "
            "this check and deleted immediately afterwards. Nothing here is "
            "real work and nothing outside this directory is in scope.",
            "",
            f"Remember this passphrase for the rest of this conversation: {secret}",
            "",
            "Do not write the passphrase into any file, and do not repeat it in "
            "your final message. A later turn will ask you to recall it.",
            "",
            "Now do exactly these two steps, in order, and nothing else:",
            "",
            f"1. Create a file named `{NOTES_FILE}` in the current directory "
            f"whose only line is exactly: NONCE={nonce}",
            "2. Run exactly this shell command in the foreground and wait for "
            f"it to finish: touch {RUNNING_FILE} && sleep {LONG_TOOL_SECONDS}",
            "",
            "This turn is expected to be interrupted while step 2 is still "
            "running. The interruption is what the check measures, so do not "
            "work around it: do not shorten the sleep, do not background it, "
            "and do not replace it with anything else.",
        ]
    )


def correction_text() -> str:
    """The operator feedback the redirect journal carries for the resumed turn.

    Written as an operator would write it — it corrects the interrupted work
    rather than describing the test — because it is delivered through the real
    correction block, where prompt-shaped text is explicitly treated as
    correction content and not as runner-owned structure.
    """
    return "\n".join(
        [
            "Stop waiting on the `sleep` command from the previous turn. It was "
            "interrupted on purpose and must not be restarted.",
            "",
            "This is an offline acceptance check with no live run behind it, so "
            "do NOT run the acknowledge command printed below this correction: "
            "there is nothing for it to report to and it will fail. Report your "
            "receipt in a file instead.",
            "",
            f"Create a file named `{RESUMED_FILE}` in the current directory "
            "containing exactly these three lines and nothing else:",
            "",
            "SECRET=<the passphrase you were asked to remember earlier in this "
            "conversation>",
            "ATTEMPT=<the exact value of the --attempt argument in the "
            "acknowledge command printed below this correction>",
            "OPERATION=<the exact value of the --redirect-id argument in that "
            "same command>",
            "",
            "If you have no memory of a passphrase from an earlier turn in this "
            "conversation, write SECRET=UNKNOWN. Do not guess it and do not "
            "search the filesystem for it: it was never written to disk.",
            "",
            "Then reply with the single word ACKNOWLEDGED.",
        ]
    )


def resume_turn_prompt(correction_block: Sequence[str]) -> str:
    """Wrap the real correction block as the resumed turn's whole prompt."""
    return "\n".join(
        [
            "## Operator correction",
            "",
            *correction_block,
            "",
            "---",
            "",
            "The correction above is your entire remaining task. Apply it and stop.",
        ]
    )


def native_cancel_prompt() -> str:
    """A turn whose only job is to sit inside a long-running shell tool."""
    return "\n".join(
        [
            "You are the subject of an automated acceptance check inside a "
            "disposable throwaway git repository.",
            "",
            "Run exactly this shell command in the foreground and wait for it to "
            f"finish: touch {RUNNING_FILE} && sleep {LONG_TOOL_SECONDS}",
            "",
            "Then reply with the single word DONE. Do not background the "
            "command, do not shorten it, and do nothing else.",
        ]
    )


def unknown_history_prompt() -> str:
    """A turn that must never run, because the conversation it names is absent."""
    return (
        "Create a file named "
        f"`{FORBIDDEN_FILE}` in the current directory containing the single "
        "word RAN, then reply DONE."
    )


#: Words that make a refusal attributable to the missing conversation rather
#: than to some unrelated failure that happened to stop the same turn.
_HISTORY_WORDS = ("conversation", "session", "resume", "rollout", "thread")


def _mentions_conversation(text: str, conversation_id: str) -> bool:
    """Whether ``text`` blames the conversation for the failure it reports."""
    lowered = text.lower()
    if conversation_id.lower() in lowered:
        return True
    return any(word in lowered for word in _HISTORY_WORDS)


def provider_refusal(text: str) -> str | None:
    """The phrase that marks ``text`` as a provider refusal, or ``None``.

    Scanned against a deliberately tiny reply, so a marker appearing here means
    the provider is talking rather than the worker.
    """
    lowered = text.lower()
    for marker in PROVIDER_UNAVAILABLE_MARKERS:
        if marker in lowered:
            return marker
    return None


def _excerpt(text: str | None) -> str:
    """A bounded, single-paragraph view of a worker's final message."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= FINAL_MESSAGE_EXCERPT_CHARS:
        return collapsed
    return collapsed[:FINAL_MESSAGE_EXCERPT_CHARS] + "..."


def ready_prompt() -> str:
    """The cheapest turn that still proves the harness can answer at all."""
    return (
        f"Reply with the single word {READY_TOKEN} and do nothing else. "
        "Do not use any tools and do not write any files."
    )
