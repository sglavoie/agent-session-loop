from .supervisor_audit_agent_contract import *
from .supervisor_audit_agent_render import render_audit_markdown


@dataclass(frozen=True)
class StagedAuditReports:
    """Both canonical reports, written in full but not yet canonical.

    The reports have to be readable before the ledger record that makes them
    true exists, because the record names them by digest. Staging separates the
    two facts: the bytes are final and hashed here, while the canonical names
    appear only once identity has been validated under the append lock. A
    publication that never gets that far therefore leaves no report for a reader
    to find, which is the difference between an audit that did not publish and a
    summary nobody wrote.
    """

    run_dir: Path
    directory: Path
    #: Each report as its staged path, the canonical name it is promoted to, and
    #: the digest of the bytes that were written.
    entries: tuple[tuple[Path, Path, str], ...]
    durable: bool = True
    #: Canonical names this staging has taken, so a publication that promoted
    #: and then failed can give them back.
    promoted: list[Path] = field(default_factory=list, repr=False, compare=False)

    @property
    def references(self) -> tuple[assurance_models_findings.EvidenceRef, ...]:
        """Evidence naming the canonical paths, not the staged ones."""
        return tuple(
            _artifact_reference(canonical, self.run_dir, digest)
            for _, canonical, digest in self.entries
        )

    def promote(self) -> None:
        """Give both reports their canonical names, or neither of them.

        Each link is refused by the filesystem if the canonical name is already
        taken, which is the same rule :func:`assurance_models_artifacts.write_new_bytes`
        enforces and for the same reason: two reports under one name would mean
        two audits disagree about one run. A failure part-way unlinks whatever
        was already promoted, so the caller can abort with the run directory
        holding no half-published audit.
        """
        for staged, canonical, _ in self.entries:
            try:
                os.link(staged, canonical)
            except OSError as error:
                self.unpromote()
                raise assurance_models_primitives.AssuranceError(
                    f"could not promote audit report {canonical}: {error}"
                ) from error
            self.promoted.append(canonical)
        if not self.durable:
            return
        try:
            descriptor = os.open(self.run_dir, os.O_RDONLY)
            try:
                durability.fullsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            self.unpromote()
            raise assurance_models_primitives.AssuranceError(
                f"could not sync audit report directory {self.run_dir}: {error}"
            ) from error

    def unpromote(self) -> None:
        """Release the canonical names again.

        Promotion happens before the ledger record is durable, so an append that
        fails afterwards would otherwise leave reports no summary names and a
        canonical name a later attempt could never take. Giving the names back
        keeps a transient failure retryable instead of permanent.
        """
        for path in self.promoted:
            with contextlib.suppress(OSError):
                path.unlink()
        self.promoted.clear()

    def discard(self) -> None:
        """Remove the staging directory, promoted or not.

        Best effort: the promoted reports are separate directory entries for the
        same data, so losing the staging copies costs nothing, and failing to
        remove them must not fail a publication that already succeeded.
        """
        shutil.rmtree(self.directory, ignore_errors=True)


def stage_audit_reports(
    run_dir: Path | str,
    audit: assurance_models_audit.SupervisorAudit,
    *,
    durable: bool = True,
) -> StagedAuditReports:
    """Write both canonical reports into this run's staging directory.

    A staging directory left behind by an earlier attempt is cleared rather than
    refused. Nothing durable ever names a path inside it, so its contents are
    unreferenced scratch, and treating them as state to protect would make one
    crashed publication permanently unable to publish this run.
    """
    root = Path(run_dir)
    directory = root / AUDIT_STAGING_DIR_NAME
    shutil.rmtree(directory, ignore_errors=True)
    try:
        directory.mkdir(parents=True)
    except OSError as error:
        raise assurance_models_primitives.AssuranceError(
            f"could not create audit staging directory {directory}: {error}"
        ) from error
    entries: list[tuple[Path, Path, str]] = []
    for name, data in (
        (
            assurance_models_vocab.AUDIT_REPORT_FILENAME,
            assurance_models_primitives.canonical_json_bytes(audit.to_dict()),
        ),
        (
            assurance_models_vocab.AUDIT_MARKDOWN_FILENAME,
            render_audit_markdown(audit).encode("utf-8"),
        ),
    ):
        staged = assurance_models_artifacts.write_audit_artifact(
            directory, name, data, durable=durable
        )
        entries.append(
            (
                staged,
                root / assurance_models_artifacts.audit_artifact_name(name),
                artifacts.sha256_hex(data),
            )
        )
    return StagedAuditReports(
        run_dir=root,
        directory=directory,
        entries=tuple(entries),
        durable=durable,
    )


def write_audit_reports(
    run_dir: Path | str,
    audit: assurance_models_audit.SupervisorAudit,
    *,
    durable: bool = True,
) -> tuple[Path, Path]:
    """Write both canonical reports, each exactly once."""
    staged = stage_audit_reports(run_dir, audit, durable=durable)
    try:
        staged.promote()
    finally:
        staged.discard()
    (_, report, _), (_, markdown, _) = staged.entries
    return report, markdown


def _artifact_reference(
    path: Path, run_dir: Path, digest: str
) -> assurance_models_findings.EvidenceRef:
    return assurance_models_findings.EvidenceRef(
        kind="artifact",
        locator=f"{audit_evidence_constants.EVIDENCE_ROOT_RUN}/{path.relative_to(run_dir).as_posix()}",
        digest=digest,
    )


__all__ = (
    "render_audit_markdown",
    "StagedAuditReports",
    "stage_audit_reports",
    "write_audit_reports",
    "_artifact_reference",
)
