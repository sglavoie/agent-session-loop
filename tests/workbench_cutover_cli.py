"""Focused portion of tests/workbench_cutover.py."""

from .workbench_cutover_journal import *


def _installation(prefix: Path) -> lifecycle.Installation:
    check_prefix_production(prefix)
    check_production(PI_SITE)
    return lifecycle.Installation(prefix, site=PI_SITE)


def main(argv: Sequence[str] | None = None) -> int:
    """One cutover operation, named, against the production site."""
    parser = argparse.ArgumentParser(
        prog="workbench_cutover",
        description="Drive one step of the production cutover.",
    )
    parser.add_argument(
        "action",
        choices=(
            "site",
            "backup",
            "verify-backup",
            "counts",
            "owner",
            "install",
            "upgrade",
            "status",
            "migrate",
            "rollback",
            "recover",
            "remove",
        ),
    )
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--python")
    parser.add_argument("--target-id")
    parser.add_argument("--role", default=lifecycle.ROLE_SERVICE)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--application-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    check_production(PI_SITE)
    if args.action == "site":
        print(
            json.dumps(
                {
                    "port": PI_SITE.port,
                    "bind_port": PI_SITE.bind_port,
                    "public_origin": PI_SITE.public_origin,
                    "deployment": PI_SITE.deployment,
                    "label": lifecycle.LABEL,
                    "unit": lifecycle.UNIT,
                    "prefix": PRODUCTION_PREFIX_NAME,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.action == "verify-backup":
        if not args.archive:
            parser.error("--archive is required")
        print(json.dumps(verify_backup(args.archive), sort_keys=True))
        return 0
    if args.action == "counts":
        if not args.database:
            parser.error("--database is required")
        print(json.dumps(table_counts(args.database), sort_keys=True))
        return 0
    if not args.prefix:
        parser.error("--prefix is required")
    if args.action == "backup":
        if not args.destination:
            parser.error("--destination is required")
        print(
            json.dumps(
                capture_backup(args.prefix, args.destination, role=args.role),
                sort_keys=True,
            )
        )
        return 0
    if args.action == "owner":
        print(
            json.dumps(check_single_owner(args.prefix, role=args.role), sort_keys=True)
        )
        return 0
    if args.action == "migrate":
        check_prefix_production(args.prefix)
        plan = migration.Migration(args.prefix, target_id=args.target_id, site=PI_SITE)
        print(json.dumps(plan.plan() if args.dry_run else plan.apply(), sort_keys=True))
        return 0
    installation = _installation(args.prefix)
    if args.action in ("install", "upgrade"):
        if not args.python:
            parser.error("--python is required; a release runs on a named interpreter")
        result = installation.install(
            artifact=args.artifact,
            digest=args.sha256,
            python=args.python,
            activate=args.activate,
            upgrade=args.action == "upgrade",
            dry_run=args.dry_run,
        )
    elif args.action == "status":
        result = installation.status()
    elif args.action == "rollback":
        result = installation.rollback(data=not args.application_only)
    elif args.action == "recover":
        result = installation.recover()
    else:
        result = installation.remove(confirm=args.confirm, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - the cutover's entry point
    raise SystemExit(main())

__all__ = ["argparse", "hashlib", "json", "plistlib", "re", "sqlite3", "tarfile", "tempfile", "time", "Mapping", "Sequence", "closing", "fields", "Path", "Any", "lifecycle", "migration", "BACKUPS_DIRECTORY", "DEPLOYMENT_PATH", "PRIVATE_DIRECTORY_MODE", "PRIVATE_FILE_MODE", "RELEASES_DIRECTORY", "atomic", "document", "PI_SITE", "IntegrationSite", "DEPLOYMENT_PRODUCTION", "PRODUCTION_PREFIX_NAME", "OWNED_SITE_FIELDS", "PRODUCTION_CHECKS", "PHONE_CHECKS", "HOST_ROLES", "CHECK_HOSTS", "SCHEMA_VERSION", "STAGE", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "VERDICT_PENDING", "PROVENANCE_ACTUAL_HOST", "MAX_CLOCK_SKEW_SECONDS", "FORBIDDEN_HOST_MARKERS", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "CutoverError", "check_production", "check_prefix_production", "BACKUP_EXCLUDED", "backup_members", "BACKUP_SCHEMA_VERSION", "BACKUP_MANIFEST", "MAX_BACKUP_BYTES", "_digest_file", "table_counts", "_installed_artifact", "capture_backup", "verify_backup", "check_single_owner", "Journal", "_window", "_installation", "main"]  # fmt: skip
