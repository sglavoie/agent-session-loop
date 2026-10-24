# ruff: noqa: F403, F405
"""CLI entry points for the deployed staging rehearsal."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .staging_rehearsal_models import *
from .staging_rehearsal_native import *


def _installation(
    prefix: Path,
    site: IntegrationSite,
    environment: Mapping[str, str] | None = None,
) -> lifecycle.Installation:
    check_prefix_isolated(prefix)
    return lifecycle.Installation(
        prefix,
        manager_factory=staging_manager_factory(prefix, site, environment),
        site=site,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """One rehearsal operation, named, against the staging site."""
    parser = argparse.ArgumentParser(
        prog="workbench_staging",
        description="Drive one step of the deployed staging rehearsal.",
    )
    parser.add_argument(
        "action",
        choices=(
            "site",
            "install",
            "upgrade",
            "status",
            "rollback",
            "recover",
            "restart",
            "remove",
            "migrate",
        ),
    )
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--python")
    parser.add_argument("--target-id")
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--role", default=lifecycle.ROLE_SERVICE)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--application-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    site = staging_site()
    check_isolated(site)
    if args.action == "site":
        label, unit = (
            staging_names(args.prefix)
            if args.prefix
            else (
                STAGING_LABEL,
                STAGING_UNIT,
            )
        )
        print(
            json.dumps(
                {
                    "port": site.port,
                    "bind_port": site.bind_port,
                    "public_origin": site.public_origin,
                    "deployment": site.deployment,
                    "label": label,
                    "unit": unit,
                    "serve": shlex.join(
                        [
                            "tailscale",
                            "serve",
                            "--bg",
                            f"--https={site.port}",
                            site.target,
                        ]
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    if not args.prefix:
        parser.error("--prefix is required")
    # Only the Mac half has an environment of its own: the Pi's staging unit
    # moves its site in the ``ExecStart`` line instead, and nothing about the
    # service depends on where a connector's plan roots are.
    environment = staging_environment(args.fixtures) if args.fixtures else None
    if args.action == "restart":
        StagingNativeManager(
            args.role, prefix=args.prefix, site=site, environment=environment
        ).restart()
        print(json.dumps({"restarted": args.role}, sort_keys=True))
        return 0
    if args.action == "migrate":
        from asl_workbench.migration import Migration

        check_prefix_isolated(args.prefix)
        migration = Migration(args.prefix, target_id=args.target_id, site=site)
        print(
            json.dumps(
                migration.plan() if args.dry_run else migration.apply(), sort_keys=True
            )
        )
        return 0
    installation = _installation(args.prefix, site, environment)
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
