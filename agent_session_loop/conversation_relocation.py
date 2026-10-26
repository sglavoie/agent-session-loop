"""Conversation relocation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import (
    artifacts,
)
from . import (
    conversation_ledger_models as models,
)
from . import (
    conversation_storage as storage,
)
from . import (
    conversation_validation as validation,
)
from .plans_files import canonicalize_path


@dataclass(frozen=True)
class RelocationReport:
    """What :func:`relocate_records` did, or would do, to one plan's ledger."""

    ledger_path: str
    records: int
    rewritten: int
    rejected: int
    applied: bool


def recorded_plan_dirs(plan_state: str | Path) -> tuple[str, ...]:
    """Every distinct plan directory the readable records name, sorted.

    State-directory migration validates against this: a ledger naming a plan
    other than the one being migrated is not this plan's operational state, and
    relocating it would attach another plan's conversations to the target.
    """
    ledger = storage.read_conversations(plan_state)
    return tuple(
        sorted({record.reference.binding.plan_dir for record in ledger.records})
    )


def relocate_records(
    plan_state: str | Path,
    *,
    from_plan_dir: str | Path,
    to_plan_dir: str | Path,
    apply: bool = False,
) -> RelocationReport:
    """Point this ledger's records at a plan directory that moved.

    The one sanctioned rewrite of an append-only file, and the same exception
    ``state.json``'s path fields already are: a plan that moved has operational
    state whose absolute paths are stale, and leaving them stale strands the
    conversations rather than preserving them.

    Line order is preserved and every unreadable line is copied back unchanged,
    so migration neither reorders history nor silently repairs corruption it
    could not read in the first place. The rewrite goes through one atomic,
    durable replace, so a crash mid-migration leaves either the old ledger or the
    new one. ``apply=False`` reports what would change and writes nothing.
    """
    ledger_path = storage.conversation_ledger_path(plan_state)
    old_plan = str(canonicalize_path(from_plan_dir))
    new_plan = str(canonicalize_path(to_plan_dir))
    try:
        data = ledger_path.read_bytes()
    except FileNotFoundError, NotADirectoryError:
        return RelocationReport(
            ledger_path=str(ledger_path),
            records=0,
            rewritten=0,
            rejected=0,
            applied=False,
        )
    except OSError as error:
        raise validation.ConversationError(
            validation.ERROR_LEDGER_WRITE,
            f"could not read {ledger_path} before relocating: {error}",
            {"path": str(ledger_path)},
        ) from error
    lines = storage._physical_lines(data)
    terminated = data.endswith(b"\n")
    records = 0
    rewritten = 0
    rejected = 0
    rendered: list[bytes] = []
    for offset, raw in enumerate(lines):
        parsed = storage._parse_line(offset + 1, raw)
        if isinstance(parsed, models.RejectedLine):
            rejected += 1
            rendered.append(raw + b"\n")
            continue
        records += 1
        if parsed.reference.binding.plan_dir != old_plan:
            rendered.append(raw + b"\n")
            continue
        payload = parsed.to_payload()
        payload["reference"]["plan_dir"] = new_plan
        rendered.append(storage._encode_record(payload))
        rewritten += 1
    # An unterminated final line stays unterminated: it is the signature of an
    # interrupted append, and healing it here would erase the evidence that the
    # next append is meant to preserve.
    if rendered and not terminated:
        rendered[-1] = rendered[-1].removesuffix(b"\n")
    if apply and rewritten:
        try:
            with storage._append_lock(Path(plan_state)):
                artifacts.atomic_write_bytes(
                    ledger_path, b"".join(rendered), durable=True
                )
        except OSError as error:
            # Same error type as the read above, so migration reports the stale
            # ledger it leaves behind instead of a traceback.
            raise validation.ConversationError(
                validation.ERROR_LEDGER_WRITE,
                f"could not rewrite {ledger_path} while relocating: {error}",
                {"path": str(ledger_path)},
            ) from error
    return RelocationReport(
        ledger_path=str(ledger_path),
        records=records,
        rewritten=rewritten,
        rejected=rejected,
        applied=bool(apply and rewritten),
    )
