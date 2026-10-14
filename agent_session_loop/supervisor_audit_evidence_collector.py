"""The evidence collector: redaction, bounded previews and omission accounting."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from . import (
    artifacts,
    assurance_models_findings,
    assurance_models_vocab,
    verification_diagnostics,
)
from .assurance_models_vocab import EvidenceKind
from .supervisor_audit_evidence_constants import (
    MAX_INLINE_STRING_BYTES,
    MAX_SEQUENCE_ITEMS,
    PREVIEW_HEAD_BYTES,
    PREVIEW_TAIL_BYTES,
    REASON_APPLICATION_CONTENT,
    REASON_MISSING,
    REASON_OUTSIDE_APPROVED_ROOTS,
    REASON_SIZE_CAP,
    REASON_UNREADABLE,
    REDACTED,
)
from .supervisor_audit_evidence_helpers import _sha256_text, _truncate_utf8
from .supervisor_audit_evidence_models import EvidenceRoots, _Omissions
from .supervisor_audit_evidence_redaction import (
    _ARGV_KEYS,
    _AUDIT_ROLE_SOURCE_VALUES,
    _CONTENT_KEYS,
    _CONTROL_PLANE_SOURCE_VALUES,
    _ENVIRONMENT_KEYS,
    _EXTERNAL_WRITE_CLASS_VALUES,
    _ORIGIN_KEYS,
    _SECRET_KEY_RE,
    _is_path_key,
    _is_reply_origin,
    _redact_argv,
    _redact_origin,
    _redact_secrets,
)


class _Collector:
    """Redacts, bounds, and validates every value on its way into the bundle."""

    def __init__(self, roots: EvidenceRoots) -> None:
        self._roots = roots
        self._evidence: dict[str, assurance_models_findings.EvidenceRef] = {}
        self._previews: list[dict[str, Any]] = []
        self.omissions = _Omissions()

    # -- references ------------------------------------------------------

    def locator(self, path: Path | str | None) -> str | None:
        if path is None:
            return None
        locator = self._roots.locate(path)
        if locator is None:
            self.omissions.record(
                "references", REASON_OUTSIDE_APPROVED_ROOTS, str(path)
            )
        return locator

    def reference(
        self,
        path: Path | str | None,
        *,
        kind: EvidenceKind,
        digest: str | None = None,
        detail: str | None = None,
    ) -> str | None:
        """Register ``path`` in the evidence index and return its locator."""
        locator = self.locator(path)
        if locator is None:
            return None
        if (
            locator not in self._evidence
            and len(self._evidence) < assurance_models_vocab.MAX_EVIDENCE_ENTRIES
        ):
            self._evidence[locator] = assurance_models_findings.EvidenceRef(
                kind=kind, locator=locator, digest=digest, detail=detail
            )
        return locator

    def evidence(self) -> list[dict[str, Any]]:
        return [self._evidence[locator].to_dict() for locator in sorted(self._evidence)]

    # -- redaction -------------------------------------------------------

    def redact(
        self, value: Any, *, key: str | None = None, parent: str | None = None
    ) -> Any:
        """Return ``value`` with credentials, content, and unsafe paths removed."""
        if parent == verification_diagnostics.FAILURE_DIAGNOSTIC_KEY:
            # A structured rejection travels as control-plane evidence, so
            # every field inside it is held to that standard: a value drawn
            # from ASL's own closed gate vocabulary passes, and anything else —
            # an unknown key, a nested structure, an application token in a
            # field that must name a gate or a reason — is the rejected
            # artifact's own text and is withheld like any output.
            if not verification_diagnostics.is_control_plane_diagnostic_field(
                key, value
            ):
                return self._content_stub(
                    value
                    if isinstance(value, str)
                    else json.dumps(value, default=str, sort_keys=True)
                )
            return self._redact_string(value, key=key, parent=parent)
        if isinstance(value, Mapping):
            redacted: dict[str, Any] = {}
            for raw_name, item in value.items():
                name = str(raw_name)
                if _is_path_key(name):
                    # A path-shaped key is data, not a field name: the guard
                    # keys its per-path manifests by the path it judged, and a
                    # path that merely ends in ``ntfy.token`` names no secret.
                    # Its value is therefore judged in the enclosing field's
                    # context, and the key itself is held to the same root
                    # policy as a path-shaped value.
                    redacted[self._redact_path_key(name)] = self._redact_path_keyed(
                        name, item, key=key, parent=parent
                    )
                    continue
                redacted[name] = self.redact(item, key=name, parent=key)
            return redacted
        if isinstance(value, (list, tuple)):
            items = list(value)
            if key in _ARGV_KEYS:
                items = _redact_argv(items)
            kept = [
                self.redact(item, key=key, parent=parent)
                for item in items[:MAX_SEQUENCE_ITEMS]
            ]
            for dropped in items[MAX_SEQUENCE_ITEMS:]:
                self.omissions.record(key or "payload", REASON_SIZE_CAP, dropped)
            return kept
        if isinstance(value, str):
            return self._redact_string(value, key=key, parent=parent)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return self._redact_string(str(value), key=key, parent=parent)

    def _redact_string(
        self, text: str, *, key: str | None, parent: str | None = None
    ) -> str:
        if key is not None:
            if _SECRET_KEY_RE.search(key):
                return REDACTED
            if key in _ENVIRONMENT_KEYS:
                return REDACTED
            if key in _CONTENT_KEYS:
                if parent == "role" and text in _AUDIT_ROLE_SOURCE_VALUES:
                    return text
                if key == "source" and text in _CONTROL_PLANE_SOURCE_VALUES:
                    return text
                return self._content_stub(text)
            if key in _ORIGIN_KEYS:
                if _is_reply_origin(text, key=key, parent=parent):
                    return text
                return _redact_origin(text)
        if text.startswith("/") and "\n" not in text:
            located = self._roots.locate(text)
            if located is not None:
                return located
            self.omissions.record("references", REASON_OUTSIDE_APPROVED_ROOTS, text)
            return f"[{REASON_OUTSIDE_APPROVED_ROOTS}:{_sha256_text(text)[:16]}]"
        cleaned = _redact_secrets(text)
        if len(cleaned.encode("utf-8")) <= MAX_INLINE_STRING_BYTES:
            return cleaned
        self.omissions.record(key or "payload", REASON_SIZE_CAP, text)
        return (
            f"{_truncate_utf8(cleaned, MAX_INLINE_STRING_BYTES)}"
            f"…[truncated sha256:{_sha256_text(text)[:16]}]"
        )

    def _redact_path_keyed(
        self, name: str, item: Any, *, key: str | None, parent: str | None
    ) -> Any:
        """Redact the value stored under the path-shaped key ``name``."""
        if isinstance(item, str):
            if item in _EXTERNAL_WRITE_CLASS_VALUES:
                return item
            if _SECRET_KEY_RE.search(Path(name).name):
                # A free-form string stored against a credential-shaped
                # file name may be that file's content; withhold it.
                return REDACTED
        return self.redact(item, key=key, parent=parent)

    def _redact_path_key(self, name: str) -> str:
        """Return a path-shaped map key, hashed when outside approved roots.

        A key inside an approved root is kept verbatim so a lookup by path
        still resolves; one outside every root is withheld exactly as a
        path-shaped value would be.
        """
        if self._roots.locate(name) is not None:
            return name
        self.omissions.record("references", REASON_OUTSIDE_APPROVED_ROOTS, name)
        return f"[{REASON_OUTSIDE_APPROVED_ROOTS}:{_sha256_text(name)[:16]}]"

    def _content_stub(self, text: str) -> str:
        self.omissions.record("payload", REASON_APPLICATION_CONTENT, len(text))
        return (
            f"[{REASON_APPLICATION_CONTENT} bytes:{len(text.encode('utf-8'))} "
            f"sha256:{_sha256_text(text)[:16]}]"
        )

    # -- files -----------------------------------------------------------

    def file_digest(
        self, path: Path, *, kind: EvidenceKind, section: str | None = None
    ) -> dict[str, Any]:
        """Describe a file by identity alone: locator, size, and digest.

        ``section`` names the omission bucket an absent or unreadable file is
        counted under. Evidence kinds are a closed vocabulary, so a caller that
        wants a narrower bucket than its kind says so here.
        """
        record: dict[str, Any] = {"reference": None, "available": False}
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            record["reason"] = REASON_MISSING
        except OSError:
            record["reason"] = REASON_UNREADABLE
        else:
            digest = artifacts.sha256_hex(data)
            record.update(
                available=True,
                byte_count=len(data),
                sha256=digest,
                reference=self.reference(path, kind=kind, digest=digest),
            )
            return record
        record["reference"] = self.locator(path)
        self.omissions.record(section or kind, record["reason"], str(path))
        return record

    def file_preview(self, path: Path, *, kind: EvidenceKind) -> dict[str, Any]:
        """Describe a stream by identity plus a bounded, shrinkable preview."""
        capture = artifacts.StreamCapture(
            head_bytes=PREVIEW_HEAD_BYTES, tail_bytes=PREVIEW_TAIL_BYTES
        )
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    capture.feed(chunk)
        except FileNotFoundError:
            capture.discard()
            return self._unavailable_preview(path, kind, REASON_MISSING)
        except OSError:
            capture.discard()
            return self._unavailable_preview(path, kind, REASON_UNREADABLE)
        metadata = capture.finalize(failed=False)
        record = {
            "available": True,
            "reference": self.reference(path, kind=kind, digest=metadata.sha256),
            "byte_count": metadata.byte_count,
            "sha256": metadata.sha256,
            "truncated": metadata.truncated,
            "preview": _redact_secrets(metadata.preview),
        }
        self._previews.append(record)
        return record

    def _unavailable_preview(
        self, path: Path, kind: EvidenceKind, reason: str
    ) -> dict[str, Any]:
        self.omissions.record(kind, reason, str(path))
        return {
            "available": False,
            "reason": reason,
            "reference": self.locator(path),
        }

    def text_preview(self, text: str, *, label: str) -> dict[str, Any]:
        """Bound a string that is already in memory, keeping its full digest."""
        encoded = text.encode("utf-8")
        preview, truncated = artifacts.bounded_preview(
            encoded, head_bytes=PREVIEW_HEAD_BYTES, tail_bytes=PREVIEW_TAIL_BYTES
        )
        record = {
            "available": True,
            "label": label,
            "byte_count": len(encoded),
            "sha256": artifacts.sha256_hex(encoded),
            "truncated": truncated,
            "preview": _redact_secrets(preview),
        }
        self._previews.append(record)
        return record

    # -- size cap --------------------------------------------------------

    def shrink_previews(
        self, budget: int, *, only: Iterable[dict[str, Any]] | None = None
    ) -> bool:
        """Trim preview text to ``budget`` bytes, reporting whether anything moved."""
        targets = self._previews if only is None else list(only)
        changed = False
        for record in targets:
            preview = record.get("preview")
            if not isinstance(preview, str) or not preview:
                continue
            if len(preview.encode("utf-8")) <= budget:
                continue
            record["preview"] = _truncate_utf8(preview, budget)
            record["truncated"] = True
            self.omissions.record("previews", REASON_SIZE_CAP, record.get("sha256"))
            changed = True
        return changed

    def adopt_previews(self, payload: Any) -> None:
        """Register the previews already inside ``payload`` as shrinkable.

        A collector that did not build the payload has no preview list, and
        :meth:`shrink_previews` would then silently shrink nothing — leaving a
        second shedding pass to drop whole turns where trimming their text would
        have done. Recovering them from the payload keeps the give-up order
        intact across passes.
        """
        if isinstance(payload, Mapping):
            if isinstance(payload.get("preview"), str) and "sha256" in payload:
                self._previews.append(cast(dict[str, Any], payload))
                return
            for value in payload.values():
                self.adopt_previews(value)
        elif isinstance(payload, list):
            for value in payload:
                self.adopt_previews(value)

    @property
    def previews(self) -> list[dict[str, Any]]:
        return self._previews


def _projection(
    collector: _Collector, state_payload: Mapping[str, Any], keys: Sequence[str]
) -> dict[str, Any]:
    return {
        key: collector.redact(state_payload.get(key), key=key)
        for key in keys
        if key in state_payload
    }
