"""Overseer decision evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from . import (
    clock,
    mailbox_constants,
    mailbox_models,
    overseer_history,
    overseer_policy_client,
)
from . import (
    overseer_decision_models as models,
)
from . import (
    overseer_decision_scope as scope_module,
)
from . import (
    overseer_decision_vocab as vocab,
)


def generated_marker(text: str) -> str:
    """The machine-prompt marker a text carries, or ``""`` if it carries none."""
    for marker in vocab.GENERATED_PROMPT_MARKERS:
        if marker in text:
            return marker
    return ""


def transcript_authorship(
    session_class: str,
    *,
    generated_classes: Sequence[str] = (),
) -> vocab.Authorship:
    """What one conversation's class says about who wrote its ``user`` turns.

    Checked in the order that keeps ``generated_classes`` a restriction: a
    named class is machine-authored whatever AgentMux called it, ``automated``
    is machine-authored on AgentMux's own word, ``main`` is the one human
    answer, and everything else -- ``unknown``, the empty class an older answer
    carries, a spelling from a newer AgentMux -- is unattributable.
    """
    named = session_class.strip()
    if named in {value.strip() for value in generated_classes if value.strip()}:
        return "generated"
    if named == vocab.SESSION_CLASS_AUTOMATED:
        return "generated"
    if named in vocab.HUMAN_SESSION_CLASSES:
        return "human"
    return "unattributed"


def statements_from_history(
    history: overseer_history.History,
    *,
    generated_classes: Sequence[str] = (),
    repositories: Sequence[str] = (),
) -> tuple[models.Statement, ...]:
    """Read transcript evidence as statements, scoped to the project it names.

    Evidence that was not permitted yields nothing: without a grant there is no
    answer to read, and reading one anyway would be exactly the substitution
    :mod:`overseer_history` refuses to make on its own side.

    A conversation's scope is the project AgentMux authorized it under, and no
    more -- a transcript names no plan, so a claim that wants one has to narrow
    the scope itself and be checked for it.

    ``repositories`` names the roots a plan declares. A conversation's working
    directory is resolved back to the one containing it, because the project is
    what AgentMux authorized and the directory is merely where the harness was
    started: a conversation opened in a repository subdirectory is evidence
    about that repository, and scoping it to the subdirectory would name a
    project no request ever carries. A directory under none of them keeps its
    own spelling, which authorizes nothing, and passing no repositories at all
    reads every directory literally.

    Authorship comes from the conversation's own class before anything about
    the turn is read, on the fail-closed reading in
    :func:`transcript_authorship`: an ``automated`` conversation contributes
    ``generated_prompt`` statements and an unattributable one contributes
    ``unattributed`` statements, neither of which can carry a decision, whether
    or not the text holds a recognized marker.

    ``generated_classes`` names further conversation classes whose ``user``
    turns are written by another agent. It is a restriction and only a
    restriction: no value in it can make a class human, so a caller that
    forgets it loses no safeguard.
    """
    if history.grant() is None:
        return ()
    roots = scope_module._declared_roots(repositories)
    statements: list[models.Statement] = []
    for conversation in history.conversations:
        authorship = transcript_authorship(
            conversation.session_class, generated_classes=generated_classes
        )
        locator = f"{conversation.harness}:{conversation.session_id}"
        project = scope_module._project_of(conversation.directory, roots)
        for excerpt in conversation.excerpts:
            marker = generated_marker(excerpt.text)
            voice = _transcript_voice(
                role=excerpt.role, marker=marker, authorship=authorship
            )
            statements.append(
                _statement(
                    statement_id=f"{vocab.TRANSCRIPT_SPACE}:{locator}#{excerpt.index}",
                    voice=voice,
                    space=vocab.TRANSCRIPT_SPACE,
                    locator=locator,
                    scope=models.Scope(project=project),
                    at=excerpt.timestamp,
                    text=excerpt.text,
                    marker=marker if voice == "generated_prompt" else "",
                )
            )
    return tuple(statements)


def statements_from_supervision(
    messages: Iterable[mailbox_models.MailboxMessage],
    *,
    grant: overseer_policy_client.Grant | None,
    scope: models.Scope,
) -> tuple[models.Statement, ...]:
    """Read a plan's mailbox traffic as statements, scoped to that plan.

    The grant is checked against the scope before anything is classified, on the
    same terms :mod:`overseer_context` checks it: the same plan, the ``context``
    category, and both identities present. A mismatch yields no statements
    rather than unscoped ones, because a statement with a scope nobody
    authorized is a statement that could be claimed against any plan.

    Nothing here becomes a ``user_instruction``. The mailbox records no author,
    so its instruction-shaped records are relayed: they show that an instruction
    arrived, never that a human composed it.
    """
    if (
        grant is None
        or not scope.plan_dir
        or grant.plan_dir != scope.plan_dir
        or grant.access != vocab.CONTEXT_ACCESS
        or not grant.request_digest
        or not grant.config_fingerprint
    ):
        return ()
    statements: list[models.Statement] = []
    for message in messages:
        text = message.text or ""
        voice = vocab.SUPERVISION_VOICES.get(message.kind, "unattributed")
        marker = generated_marker(text)
        if marker and voice == "relayed_instruction":
            voice = "generated_prompt"
        statements.append(
            _statement(
                statement_id=f"{vocab.STATE_SPACE}:{message.seq}:{message.id}",
                voice=voice,
                space=vocab.STATE_SPACE,
                locator=f"{mailbox_constants.MAILBOX_FILE_NAME}#{message.seq}",
                scope=scope,
                at=message.created_at,
                text=text,
                marker=marker if voice == "generated_prompt" else "",
            )
        )
    return tuple(statements)


def evidence_digest(
    statements: Sequence[models.Statement],
    *,
    scope: models.Scope,
) -> str:
    """Summarize the human evidence in scope, so a reading can name what it read.

    Only :data:`REVIEWED_VOICES` statements whose own scope bears on ``scope``
    take part. Both narrowings matter: a machine record cannot move the digest,
    since ASL writes one for every message it delivers and a digest that
    answered to those would report a change on every action; and a statement
    about another plan cannot move it either, for the same reason a decision
    about another plan cannot authorize this one.

    Bearing on a scope is deliberately weaker than authorizing it: a statement
    in one of a multi-repository plan's projects counts, even though on its own
    it could authorize nothing there. Freshness asks what the host could have
    read, and a hold it would have read is a hold that has to move the digest.

    The identity is order-independent -- statements are sorted by their own ID
    -- so a boundary returning the same records in a different order is the same
    record. Everything a reading could turn on is hashed: the statement's
    identity and voice, its moment, its text, and the scope it was made in, each
    length-prefixed so no two different records can pack into one string.
    """
    relevant = sorted(
        (
            item
            for item in statements
            if item.voice in vocab.REVIEWED_VOICES and item.scope.bears_on(scope)
        ),
        key=lambda item: item.statement_id,
    )
    digest = hashlib.sha256()
    parts = [vocab.EVIDENCE_DIGEST_PREFIX]
    for item in relevant:
        parts.extend(
            (
                item.statement_id,
                item.voice,
                item.at,
                item.text,
                "\x1f".join(item.scope.named_projects),
                item.scope.plan_dir,
                item.scope.run_id,
                item.scope.incident,
            )
        )
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return f"ev1-{digest.hexdigest()[:32]}"


def _transcript_voice(
    *, role: str, marker: str, authorship: vocab.Authorship
) -> vocab.Voice:
    """Attribute one transcript message from its conversation, role and text.

    Only the ``user`` role is ambiguous, so only it consults the conversation's
    authorship: an assistant is an assistant in an automated conversation too,
    and neither answer authorizes anything.
    """
    normalized = role.strip().lower()
    if normalized == "assistant":
        return "assistant_interpretation"
    if normalized != "user":
        return "unattributed"
    if authorship == "unattributed":
        return "unattributed"
    if authorship == "generated" or marker:
        return "generated_prompt"
    return "user_instruction"


def _statement(
    *,
    statement_id: str,
    voice: vocab.Voice,
    space: str,
    locator: str,
    scope: models.Scope,
    at: str,
    text: str,
    marker: str = "",
) -> models.Statement:
    """One statement, clipped and dated, however the source spelled its moment."""
    clipped = text[: vocab.MAX_STATEMENT_CHARS]
    return models.Statement(
        statement_id=statement_id,
        voice=voice,
        space=space,
        locator=locator,
        scope=scope,
        at=at,
        text=clipped,
        truncated=len(clipped) < len(text),
        marker=marker,
        moment=clock.parse_datetime(at),
    )


def _scope_text(scope: models.Scope) -> str:
    """One scope, rendered for a refusal an operator has to act on."""
    named = scope.named_projects
    label = "projects" if len(named) > 1 else "project"
    parts = [f"{label} {', '.join(named) or '-'}"]
    for label, value in (
        ("plan", scope.plan_dir),
        ("run", scope.run_id),
        ("incident", scope.incident),
    ):
        if value:
            parts.append(f"{label} {value}")
    return ", ".join(parts)
