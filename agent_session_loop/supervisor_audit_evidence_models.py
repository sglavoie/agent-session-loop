"""Evidence roots, the audit input bundle and the omission ledger."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    assurance_models_artifacts,
    assurance_models_findings,
    assurance_models_primitives,
    assurance_models_vocab,
    workspaces,
)
from .supervisor_audit_evidence_constants import (
    EVIDENCE_ROOT_PACKAGE,
    EVIDENCE_ROOT_PLAN,
    EVIDENCE_ROOT_RUN,
    EVIDENCE_ROOT_SKILLS,
    EVIDENCE_ROOT_STATE,
    EVIDENCE_ROOT_WORKSPACE,
)


@dataclass(frozen=True)
class EvidenceRoots:
    """The approved roots every reference in the bundle must resolve inside.

    References are rendered as ``<root>/<relative>`` rather than as absolute
    paths, which is what makes containment decidable: a locator that cannot be
    expressed this way names something outside the audit's reach and is
    withheld instead of copied.
    """

    roots: tuple[tuple[str, Path], ...]

    @classmethod
    def build(
        cls,
        *,
        run_dir: Path | str,
        state_dir: Path | str,
        plan_dir: Path | str,
        workspace_root: Path | str | None = None,
    ) -> EvidenceRoots:
        package_root = Path(__file__).resolve().parent
        declared: list[tuple[str, Path | str]] = [
            (EVIDENCE_ROOT_RUN, run_dir),
            (EVIDENCE_ROOT_STATE, state_dir),
            (EVIDENCE_ROOT_PLAN, plan_dir),
            (EVIDENCE_ROOT_PACKAGE, package_root),
            (EVIDENCE_ROOT_SKILLS, package_root.parent / "skills"),
        ]
        if workspace_root is not None:
            declared.append((EVIDENCE_ROOT_WORKSPACE, workspace_root))
        return cls(
            roots=tuple(
                (name, workspaces.canonicalize_filesystem_path(value))
                for name, value in declared
            )
        )

    def to_dict(self) -> dict[str, str]:
        return {name: str(path) for name, path in self.roots}

    def locate(self, path: Path | str) -> str | None:
        """Return the symbolic locator for ``path``, or ``None`` when unsafe.

        The deepest matching root wins, so an artifact inside the run directory
        is named ``run/...`` rather than ``state/runs/<id>/...`` even though
        both containments hold.
        """
        candidate = workspaces.canonicalize_filesystem_path(path)
        best: tuple[int, str] | None = None
        for name, root in self.roots:
            if candidate == root:
                locator = name
            elif workspaces.is_descendant(candidate, root):
                locator = f"{name}/{candidate.relative_to(root).as_posix()}"
            else:
                continue
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, locator)
        return None if best is None else best[1]


@dataclass(frozen=True)
class AuditInput:
    """The bounded, control-plane-only evidence bundle an auditor reads."""

    payload: dict[str, Any]

    def to_bytes(self) -> bytes:
        return assurance_models_primitives.canonical_json_bytes(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def evidence(self) -> tuple[assurance_models_findings.EvidenceRef, ...]:
        return tuple(
            assurance_models_findings.parse_evidence_ref(entry)
            for entry in self.payload.get("evidence", ())
        )

    @property
    def omissions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload.get("omissions", ()))

    def write(self, run_dir: Path | str, *, durable: bool = True) -> Path:
        """Write the bundle as the run's canonical audit input, exactly once."""
        return assurance_models_artifacts.write_audit_artifact(
            run_dir,
            assurance_models_vocab.AUDIT_INPUT_FILENAME,
            self.to_bytes(),
            durable=durable,
        )


class _Omissions:
    """Counts, digests, and reasons for everything the bundle left out."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}

    def record(self, section: str, reason: str, payload: Any) -> None:
        entry = self._entries.setdefault(
            (section, reason),
            {
                "section": section,
                "reason": reason,
                "count": 0,
                "digest": hashlib.sha256(),
            },
        )
        entry["count"] += 1
        entry["digest"].update(
            assurance_models_primitives.canonical_json_bytes(payload)
        )

    def seed(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Adopt already-rendered omissions so a later pass extends them.

        A rendered entry carries a digest, not the payloads it was built from,
        so the running hash is re-opened over that digest: the result still
        identifies exactly one set of withheld payloads, and whatever the next
        pass drops extends it. Counts stay exact, which is what a reader of the
        bundle acts on.
        """
        for entry in entries:
            section = str(entry.get("section") or "")
            reason = str(entry.get("reason") or "")
            count = entry.get("count")
            digest = hashlib.sha256()
            digest.update(
                assurance_models_primitives.canonical_json_bytes(entry.get("sha256"))
            )
            self._entries[(section, reason)] = {
                "section": section,
                "reason": reason,
                "count": count if isinstance(count, int) and count > 0 else 0,
                "digest": digest,
            }

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {
                "section": entry["section"],
                "reason": entry["reason"],
                "count": entry["count"],
                "sha256": entry["digest"].hexdigest(),
            }
            for entry in sorted(
                self._entries.values(),
                key=lambda item: (item["section"], item["reason"]),
            )
        ]
