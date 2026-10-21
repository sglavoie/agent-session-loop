"""Verify that the active ASL install points at this checkout."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .install_verify_agent_mux import _check_agent_mux
from .install_verify_common import readable_path
from .install_verify_maintenance import _check_maintenance_job
from .install_verify_skills import (
    _check_asl_skill,
    _check_session_run_content,
    _check_skill_links,
    _check_startup_context,
)
from .install_verify_smoke import _check_asl_path, _check_status_smoke


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def verify_install(
    *,
    repo_root: str | Path | None = None,
    home: str | Path | None = None,
    path_env: str | None = None,
    require_agent_mux: bool = False,
) -> dict[str, Any]:
    """Return a structured active-install verification payload."""

    resolved_repo = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    resolved_home = (
        Path(home).expanduser().resolve() if home is not None else Path.home()
    )
    active_asl, asl_check = _check_asl_path(resolved_repo, path_env)
    checks = [
        asl_check,
        *_check_skill_links(resolved_repo, resolved_home),
        *_check_session_run_content(resolved_home),
        *_check_startup_context(resolved_home),
        *_check_asl_skill(resolved_repo, resolved_home),
        _check_status_smoke(
            active_asl=active_asl,
            home=resolved_home,
            path_env=path_env,
        ),
        _check_maintenance_job(resolved_home),
        *_check_agent_mux(
            path_env=path_env,
            home=resolved_home,
            active_asl=active_asl,
        ),
    ]
    if require_agent_mux:
        checks = [
            {**check, "advisory": False} if check["advisory"] else check
            for check in checks
        ]
    failures = [check for check in checks if not check["ok"] and not check["advisory"]]
    advisories = [check for check in checks if not check["ok"] and check["advisory"]]
    return {
        "ok": not failures,
        "repo_root": readable_path(resolved_repo),
        "home": readable_path(resolved_home),
        "active_asl": active_asl,
        "require_agent_mux": require_agent_mux,
        "checks": checks,
        "failures": failures,
        "advisories": advisories,
    }


def _print_text(payload: dict[str, Any]) -> None:
    status = "ok" if payload["ok"] else "FAIL"
    print(f"ASL install verification: {status}")
    print(f"Repo: {payload['repo_root']}")
    print(f"Home: {payload['home']}")
    print(f"Active asl: {payload['active_asl'] or 'not found'}")
    for check in payload["checks"]:
        if check["ok"]:
            prefix = "ok"
        elif check["advisory"]:
            prefix = "warn"
        else:
            prefix = "FAIL"
        print(f"{prefix} {check['name']}: {check['detail']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that active ASL wrappers and skills resolve to this "
        "checkout.",
    )
    parser.add_argument(
        "--repo-root",
        help="Checkout root to verify against (default: inferred from this module)",
    )
    parser.add_argument(
        "--home",
        help="Home directory whose installed links should be checked (default: $HOME)",
    )
    parser.add_argument(
        "--path",
        dest="path_env",
        help="PATH to search for asl (default: current process PATH)",
    )
    parser.add_argument(
        "--require-agent-mux",
        action="store_true",
        help=(
            "Fail when the paired Agent Mux cockpit is missing or unhealthy "
            "(advisory by default; ASL never requires it)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = verify_install(
        repo_root=args.repo_root,
        home=args.home,
        path_env=args.path_env,
        require_agent_mux=args.require_agent_mux,
    )
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        _print_text(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
