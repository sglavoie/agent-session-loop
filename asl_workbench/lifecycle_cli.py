"""Lifecycle cli."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import lifecycle_artifacts as artifacts
from . import lifecycle_common as common
from . import lifecycle_installation as installation_module
from .installation import (
    SHARED_MODE_BITS,
    InstallationError,
)
from .migration import (
    Migration,
)

# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{common.PACKAGE_DIRECTORY}.lifecycle",
        description="Install, upgrade, inspect and remove a Workbench deployment.",
    )
    parser.add_argument(
        "action",
        choices=(
            "pack",
            "install",
            "upgrade",
            "migrate",
            "rollback",
            "status",
            "remove",
            "reset",
            "recover",
        ),
    )
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--python")
    parser.add_argument("--revision")
    parser.add_argument("--target-id")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--application-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "pack":
        if not args.artifact or not args.revision:
            raise common.LifecycleError(
                "pack requires --artifact and a full commit --revision"
            )
        digest = artifacts.pack(args.artifact, args.revision)
        return {
            "artifact": str(args.artifact),
            "sha256": digest,
            "revision": args.revision,
        }
    if not args.prefix:
        raise common.LifecycleError("--prefix is required")
    installation = installation_module.Installation(args.prefix)
    if args.action in ("install", "upgrade"):
        if not args.python:
            raise common.LifecycleError("an absolute Python executable is required")
        return installation.install(
            artifact=args.artifact,
            digest=args.sha256,
            python=args.python,
            activate=args.activate,
            upgrade=args.action == "upgrade",
            dry_run=args.dry_run,
        )
    if args.action in ("remove", "reset"):
        return installation.remove(
            reset=args.action == "reset", confirm=args.confirm, dry_run=args.dry_run
        )
    if args.action == "migrate":
        # The dry run here is the plan itself, which is the whole reason a
        # migration is a separate action: an operator gets to read what would
        # happen to an installation nobody can reprovision before it happens.
        migration = Migration(args.prefix, target_id=args.target_id)
        return migration.plan() if args.dry_run else migration.apply()
    if args.dry_run:
        raise common.LifecycleError(
            "dry-run is supported for install, upgrade, migrate, remove and reset"
        )
    if args.action == "status":
        return installation.status()
    if args.action == "rollback":
        return installation.rollback(data=not args.application_only)
    return installation.recover()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.umask(SHARED_MODE_BITS)
    try:
        print(json.dumps(run(args), sort_keys=True))
    except Exception as error:  # noqa: BLE001 - CLI diagnostics must not leak state
        print(
            str(error)
            if isinstance(error, InstallationError)
            else "workbench lifecycle failed; check inputs, permissions "
            "and compatibility",
            file=sys.stderr,
        )
        return 1
    return 0
