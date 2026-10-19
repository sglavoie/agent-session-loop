# ruff: noqa: F403, F405
from .migration_schema import *


def reconcile_journal(
    records: Iterable[Mapping[str, Any]], *, now: float
) -> list[dict[str, Any]]:
    """Close every operation the migrated connector cannot account for.

    The same two answers a restart gives, for the same reason. ``pending`` is
    the one state that demonstrably never ran -- admission and attempt are
    separate steps -- so it expires. Everything else still open was attempted,
    and nothing on this side of the wire can establish that it did not happen,
    so it becomes ``unconfirmed``: not settled, still open to late evidence,
    and closed to any further attempt.

    Nothing is re-dispatched and nothing is re-sent. The records carry no reply
    text and no action token to re-send *with*, and the projection below is the
    same allowlist the journal is written through, so a legacy record that
    somehow carried one loses it here rather than downstream.
    """
    reconciled: list[dict[str, Any]] = []
    for entry in records:
        if not isinstance(entry, Mapping):
            continue
        operation_id = entry.get("operation_id")
        status = entry.get("status")
        if not isinstance(operation_id, str) or not operation_id:
            continue
        if not isinstance(status, str) or status not in OPERATION_STATUSES:
            continue
        try:
            record = persist_operation(entry)
            if status in UNRECONCILED_STATUSES:
                record = advance_record(
                    record,
                    OPERATION_EXPIRED
                    if status == OPERATION_PENDING
                    else OPERATION_UNCONFIRMED,
                    now=now,
                    detail_code=DETAIL_CONNECTOR_RESTARTED,
                )
        except OperationError:
            # A record this package cannot project is a record it cannot
            # reason about either. Dropping it is the Companion's own posture
            # for an unreadable journal, and the alternative -- carrying it
            # forward unexamined -- is carrying forward the one thing that
            # could name an action.
            continue
        reconciled.append(record)
    reconciled.sort(key=lambda record: str(record["operation_id"]))
    return reconciled


def journal_document(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The durable journal document, under the version both halves read."""
    return {
        "version": JOURNAL_SCHEMA_VERSION,
        "operations": [dict(record) for record in records],
    }


def migrate_journal(data: object, *, now: float) -> dict[str, Any]:
    """Read one legacy journal document and return its reconciled successor.

    A document from another schema yields an empty journal rather than a
    refusal, matching ``companion/commands.py``: the operations it describes
    are, by the time anybody is migrating, certainly over, and refusing to
    migrate over a file nobody can read would strand an installation on a
    journal entry.
    """
    if not isinstance(data, Mapping) or data.get("version") != JOURNAL_SCHEMA_VERSION:
        return journal_document([])
    entries = data.get("operations")
    return journal_document(
        reconcile_journal(entries if isinstance(entries, list) else [], now=now)
    )


def migrate_checkpoints(data: object) -> dict[str, Any]:
    """Carry a legacy history checkpoint into this package's payload shape.

    The Companion's payload is this one without ``open_questions``, so the
    carry is a read and a write rather than a rewrite -- and it is worth doing
    explicitly because the alternative is discovering at the first import that
    the document was silently discarded and a week of history is being read
    from the top again.
    """
    return checkpoint_document(list(checkpoints_from_document(data).values()))


def migrate_connector_configuration(
    data: Mapping[str, Any], *, target_id: str
) -> dict[str, Any]:
    """Translate a legacy connector configuration, given the target it pairs to.

    ``target_id`` cannot be derived: the Companion never recorded one, and the
    transport hello is judged against it, so a value invented here would attach
    the Mac to a service nobody named. It is a required argument for exactly
    that reason.
    """
    if not target_id or len(target_id) > 128:
        raise MigrationError("a connector migration requires a target id")
    unknown = sorted(set(data) - LEGACY_CONNECTOR_KEYS - CONNECTOR_KEYS)
    if unknown:
        raise MigrationError("connector configuration carries unknown fields")
    migrated = {key: data[key] for key in CONNECTOR_KEYS if key in data}
    migrated["role"] = ROLE_CONNECTOR
    migrated["target_id"] = target_id
    if set(migrated) != CONNECTOR_KEYS:
        raise MigrationError("connector configuration is missing required fields")
    return migrated


def migrate_service_configuration(
    data: Mapping[str, Any], *, site: IntegrationSite
) -> dict[str, Any]:
    """Reduce a legacy service configuration to the fields this package reads.

    ``observation_only`` is made explicit rather than left to a default. The
    Companion's default was "mutations permitted" and this package's is "observe
    only", so a configuration that simply omitted it would change meaning in
    the one direction an operator would not notice until a mutation was
    refused.
    """
    raw = data.get("settings")
    if not isinstance(raw, Mapping):
        raise MigrationError("service configuration has no settings object")
    settings: dict[str, Any] = {
        key: raw[key] for key in SERVICE_SETTINGS_KEYS if key in raw
    }
    settings["observation_only"] = bool(raw.get("observation_only", True))
    owner = settings.get("owner_identity")
    if not isinstance(owner, str) or not owner:
        raise MigrationError("service configuration has no owner identity")
    for key, expected in (
        ("bind_host", site.bind_host),
        ("bind_port", site.bind_port),
        ("public_origin", site.public_origin),
    ):
        # Refused rather than rewritten. A prefix configured for a different
        # endpoint is a deployment this migration was not shown, and silently
        # pointing it at this site would move a running service.
        if key in settings and settings[key] != expected:
            raise MigrationError("service configuration names another endpoint")
    return {"role": ROLE_SERVICE, "settings": settings}


# ---------------------------------------------------------------------------
# Files
