"""Shared safe rendering primitives for mailbox-shaped prompt and CLI text."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any

from . import mailbox


MAILBOX_MESSAGE_BEGIN = "BEGIN ASL MAILBOX MESSAGE JSONL"
MAILBOX_MESSAGE_END = "END ASL MAILBOX MESSAGE JSONL"
MAX_RENDERED_GUIDANCE = 20
MAX_RENDERED_GUIDANCE_BYTES = 64 * 1024

RenderedMessage = tuple[mailbox.MailboxMessage, str]
GuidanceSectionBuilder = Callable[
    [
        Sequence[RenderedMessage],
        Sequence[mailbox.MailboxMessage],
        Sequence[mailbox.MailboxMessage],
        Sequence[mailbox.MailboxMessage],
    ],
    list[str],
]


@dataclass(frozen=True)
class BoundedGuidance:
    """One fully rendered guidance section and the records it omitted."""

    lines: tuple[str, ...]
    records: tuple[RenderedMessage, ...]
    record_cap_omitted: tuple[mailbox.MailboxMessage, ...]
    individually_oversized: tuple[mailbox.MailboxMessage, ...]
    aggregate_budget_omitted: tuple[mailbox.MailboxMessage, ...]

    @property
    def omitted(self) -> tuple[mailbox.MailboxMessage, ...]:
        return (
            self.record_cap_omitted
            + self.individually_oversized
            + self.aggregate_budget_omitted
        )


def ascii_json(payload: Mapping[str, Any]) -> str:
    """Return one deterministic ASCII JSON record safe for line framing."""
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def message_json(message: mailbox.MailboxMessage) -> str:
    """Render the public identity and text fields of one mailbox message."""
    return ascii_json(
        {
            "id": message.id,
            "seq": message.seq,
            "text": message.text or "",
        }
    )


def render_bounded_guidance(
    pending_guidance: Sequence[mailbox.MailboxMessage],
    build_section: GuidanceSectionBuilder,
) -> BoundedGuidance:
    """Render newest guidance under the shared record and complete-byte caps.

    The target-specific builder owns prose and delimiters, so the byte budget
    applies to the complete section that actually reaches a prompt or terminal.
    Records that cannot fit alone are removed first; aggregate overflow then
    evicts the oldest remaining records and predictably preserves the newest.
    """
    pending = tuple(pending_guidance)
    candidates = pending[-MAX_RENDERED_GUIDANCE:]
    record_cap_omitted = pending[: len(pending) - len(candidates)]
    rendered_candidates = tuple(
        (message, message_json(message)) for message in candidates
    )

    individually_oversized: list[mailbox.MailboxMessage] = []
    fitting_records: list[RenderedMessage] = []
    for record in rendered_candidates:
        single_record_lines = build_section(
            (record,),
            record_cap_omitted,
            (),
            (),
        )
        if (
            len("\n".join(single_record_lines).encode("utf-8"))
            > MAX_RENDERED_GUIDANCE_BYTES
        ):
            individually_oversized.append(record[0])
        else:
            fitting_records.append(record)

    aggregate_budget_omitted: list[mailbox.MailboxMessage] = []
    rendered_lines = build_section(
        fitting_records,
        record_cap_omitted,
        individually_oversized,
        aggregate_budget_omitted,
    )
    while (
        len("\n".join(rendered_lines).encode("utf-8"))
        > MAX_RENDERED_GUIDANCE_BYTES
        and fitting_records
    ):
        aggregate_budget_omitted.append(fitting_records.pop(0)[0])
        rendered_lines = build_section(
            fitting_records,
            record_cap_omitted,
            individually_oversized,
            aggregate_budget_omitted,
        )

    if (
        len("\n".join(rendered_lines).encode("utf-8"))
        > MAX_RENDERED_GUIDANCE_BYTES
    ):
        raise ValueError(
            "guidance section metadata exceeds the shared rendering budget"
        )

    return BoundedGuidance(
        lines=tuple(rendered_lines),
        records=tuple(fitting_records),
        record_cap_omitted=tuple(record_cap_omitted),
        individually_oversized=tuple(individually_oversized),
        aggregate_budget_omitted=tuple(aggregate_budget_omitted),
    )
