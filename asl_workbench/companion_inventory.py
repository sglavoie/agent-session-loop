"""The Companion's surface, pinned here and cross-checked against its checkout.

The Companion lives in a different repository
(``sglavoie_raspberry_pi:services/pi-monitoring/companion``) and is not
importable from ``scripts``, so the capability matrix cannot derive its rows
the way it derives the Workbench's from the live route table and argument
parser. Pinning the surface as data is the alternative, and a pin that nothing
compares against is exactly the kind of inventory that silently goes stale --
so the pin comes with a reader.

:func:`locate_companion_package` resolves the Companion package from a
``scripts`` checkout using the path the plan's repository registration already
declares, or from :data:`ENV_PI_CHECKOUT` when a caller knows better.
:func:`derive_modules` and :func:`derive_routes` then read the real surface out
of that checkout: the module list off the filesystem, the route table by
parsing ``service.py``'s ``ROUTES`` tuple rather than importing it, since
importing the Companion would need its own dependencies and its own Python.

When the Pi checkout is absent -- a ``scripts``-only clone, or CI -- the pinned
tuples still stand on their own and the cross-check reports that it could not
run. It does not quietly pass.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Where the Companion sits relative to the workspace root that holds both
#: repositories, as registered by the plan: ``scripts`` and
#: ``1_dev_projects/sglavoie_raspberry_pi`` are siblings under one root.
PI_REPO_RELATIVE_PATH = Path("1_dev_projects") / "sglavoie_raspberry_pi"
COMPANION_RELATIVE_PATH = Path("services") / "pi-monitoring" / "companion"

#: An explicit override, for a checkout laid out some other way.
ENV_PI_CHECKOUT = "ASL_WORKBENCH_PI_CHECKOUT"

#: The service module whose ``ROUTES`` tuple is the Companion's route table.
SERVICE_MODULE_FILE_NAME = "service.py"
DASHBOARD_MODULE_FILE_NAME = "dashboard.py"

#: Route roles, as ``companion/auth.py`` defines them. Repeated here because
#: the pin below carries them and nothing in ``scripts`` can import the source.
ROLE_CONNECTOR = "connector"
ROLE_SUMMARY = "summary"
ROLE_OWNER = "owner"
ROLE_PUBLIC = "public"

#: Every module in the Companion package, by dotted name.
COMPANION_MODULES: tuple[str, ...] = (
    "companion",
    "companion.auth",
    "companion.commands",
    "companion.config",
    "companion.connector",
    "companion.dashboard",
    "companion.framing",
    "companion.freshness",
    "companion.history",
    "companion.identity",
    "companion.incidents",
    "companion.integrations",
    "companion.integrations_kuma",
    "companion.lifecycle",
    "companion.live",
    "companion.ntfy",
    "companion.observation",
    "companion.projects",
    "companion.protocol",
    "companion.questions",
    "companion.recovery",
    "companion.resume",
    "companion.runtime",
    "companion.service",
    "companion.store",
)


@dataclass(frozen=True)
class CompanionRoute:
    """One route the Companion's Pi service answers on, and who may call it."""

    method: str
    path: str
    role: str

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


#: The Companion service's route table. The dashboard page and its two assets
#: sit behind the same owner check as the data they render, which is why they
#: are routes here rather than an unauthenticated static mount.
COMPANION_ROUTES: tuple[CompanionRoute, ...] = (
    CompanionRoute("GET", "/", ROLE_OWNER),
    CompanionRoute("GET", "/dashboard", ROLE_OWNER),
    CompanionRoute("GET", "/assets/dashboard.css", ROLE_OWNER),
    CompanionRoute("GET", "/assets/dashboard.js", ROLE_OWNER),
    CompanionRoute("GET", "/v1/health", ROLE_PUBLIC),
    CompanionRoute("GET", "/v1/summary", ROLE_SUMMARY),
    CompanionRoute("GET", "/v1/status", ROLE_OWNER),
    CompanionRoute("GET", "/v1/runs", ROLE_OWNER),
    CompanionRoute("GET", "/v1/history", ROLE_OWNER),
    CompanionRoute("GET", "/v1/questions", ROLE_OWNER),
    CompanionRoute("GET", "/v1/projects", ROLE_OWNER),
    CompanionRoute("POST", "/v1/ingest", ROLE_CONNECTOR),
    CompanionRoute("POST", "/v1/commands/poll", ROLE_CONNECTOR),
    CompanionRoute("POST", "/v1/commands/redeem", ROLE_CONNECTOR),
    CompanionRoute("GET", "/v1/operations", ROLE_OWNER),
    CompanionRoute("POST", "/v1/operations", ROLE_OWNER),
    CompanionRoute("POST", "/v1/operations/cancel", ROLE_OWNER),
)


class InventoryError(Exception):
    """Raised when a located Companion checkout cannot be read as expected."""


def locate_companion_package(
    scripts_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve the Companion package directory, or ``None`` when absent.

    :data:`ENV_PI_CHECKOUT` wins when set and pointing at a real directory;
    otherwise the sibling layout the plan registered is tried. Returning
    ``None`` rather than raising is deliberate: a ``scripts``-only checkout is
    a legitimate place to run the suite, and the caller decides whether a
    missing cross-check is a skip or a failure.
    """
    env = os.environ if environ is None else environ
    override = env.get(ENV_PI_CHECKOUT)
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    candidates.append(Path(scripts_root).resolve().parent / PI_REPO_RELATIVE_PATH)
    for candidate in candidates:
        package = candidate / COMPANION_RELATIVE_PATH
        if package.is_dir():
            return package
    return None


def derive_modules(package_root: Path) -> tuple[str, ...]:
    """Read the Companion's module list off a checkout, sorted by dotted name."""
    root = Path(package_root)
    if not root.is_dir():
        raise InventoryError(f"not a companion package directory: {root}")
    names: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        parts = (root.name, *relative.parts[:-1])
        if path.name != "__init__.py":
            parts = (*parts, relative.stem)
        names.append(".".join(parts))
    return tuple(sorted(names))


def _string_constant(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _assigned_tuple(tree: ast.Module, name: str) -> tuple[ast.expr, ...] | None:
    """The elements of a module-level ``name = (...)`` tuple, or ``None``."""
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            continue
        value = node.value
        if isinstance(value, ast.Tuple):
            return tuple(value.elts)
        return None
    return None


def _role_name(node: ast.expr) -> str | None:
    """Resolve a role argument written as a ``ROLE_*`` constant reference."""
    if isinstance(node, ast.Name) and node.id.startswith("ROLE_"):
        return node.id.removeprefix("ROLE_").lower()
    return _string_constant(node)


def _assigned_dict_keys(tree: ast.Module, name: str) -> tuple[str, ...] | None:
    """The string keys of a module-level ``name = {...}`` mapping."""
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            return None
        keys = [_string_constant(key) for key in node.value.keys if key is not None]
        if any(key is None for key in keys):
            return None
        return tuple(key for key in keys if key is not None)
    return None


def _dashboard_path_constants(package_root: Path) -> dict[str, tuple[str, ...]]:
    """The path tuples ``service.py`` splats into its route table.

    These are the one part of the Companion's route table that is not a literal
    in ``service.py``: it iterates ``DASHBOARD_PATHS`` and ``ASSET_PATHS`` from
    ``dashboard.py``. ``ASSET_PATHS`` is ``tuple(sorted(ASSETS))`` there, so the
    mapping's keys are read and sorted the same way.
    """
    source = (package_root / DASHBOARD_MODULE_FILE_NAME).read_text(encoding="utf-8")
    tree = ast.parse(source)
    pages = _assigned_tuple(tree, "DASHBOARD_PATHS")
    if pages is None:
        raise InventoryError("companion/dashboard.py has no DASHBOARD_PATHS tuple")
    page_paths = [_string_constant(element) for element in pages]
    if any(path is None for path in page_paths):
        raise InventoryError("DASHBOARD_PATHS is not a tuple of plain literals")
    assets = _assigned_dict_keys(tree, "ASSETS")
    if not assets:
        raise InventoryError("companion/dashboard.py has no literal ASSETS mapping")
    return {
        "DASHBOARD_PATHS": tuple(path for path in page_paths if path is not None),
        "ASSET_PATHS": tuple(sorted(assets)),
    }


def derive_routes(package_root: Path) -> tuple[CompanionRoute, ...]:
    """Read the Companion's route table off a checkout, in declaration order.

    ``service.py`` is parsed rather than imported: the Companion has its own
    interpreter and its own installation prefix on the Pi, and importing it
    from the ``scripts`` suite would couple this check to both.
    """
    root = Path(package_root)
    source = (root / SERVICE_MODULE_FILE_NAME).read_text(encoding="utf-8")
    elements = _assigned_tuple(ast.parse(source), "ROUTES")
    if elements is None:
        raise InventoryError("companion/service.py has no ROUTES tuple")
    splat_paths = _dashboard_path_constants(root)
    routes: list[CompanionRoute] = []
    for element in elements:
        if isinstance(element, ast.Starred):
            # ``*(Route("GET", path, ROLE_OWNER, ...) for path in <PATHS>)``:
            # the method and role are literal, the paths come from dashboard.py.
            comprehension = element.value
            if not isinstance(comprehension, ast.GeneratorExp) or not isinstance(
                comprehension.elt, ast.Call
            ):
                raise InventoryError("unexpected splat in companion ROUTES")
            args = comprehension.elt.args
            method = _string_constant(args[0])
            role = _role_name(args[2])
            iterated = comprehension.generators[0].iter
            if method is None or role is None or not isinstance(iterated, ast.Name):
                raise InventoryError("unexpected splat arguments in companion ROUTES")
            if iterated.id not in splat_paths:
                raise InventoryError(
                    f"unknown splat source {iterated.id} in companion ROUTES"
                )
            for path in splat_paths[iterated.id]:
                routes.append(CompanionRoute(method, path, role))
            continue
        if not isinstance(element, ast.Call):
            raise InventoryError("unexpected entry in companion ROUTES")
        method = _string_constant(element.args[0])
        path = _string_constant(element.args[1])
        role = _role_name(element.args[2])
        if method is None or path is None or role is None:
            raise InventoryError("companion ROUTES entry is not a plain literal")
        routes.append(CompanionRoute(method, path, role))
    return tuple(routes)
