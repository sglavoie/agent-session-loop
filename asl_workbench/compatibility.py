"""The portable package's three boundaries: version, dependencies, imports.

The unified application is maintained in ``scripts``, whose tooling targets
Python 3.14, and deployed to a Pi documented at Python 3.13 with no wheels
installed. Those two facts make three rules, and this module is each rule's
executable form rather than its description:

* **Version.** :data:`MINIMUM_PYTHON_VERSION` is the floor preserved from the
  retired Companion installer: ``(3, 13) <= version < (4, 0)``.
  :func:`check_runtime` answers for a version; :func:`unsupported_syntax`
  answers for the source, by reparsing every module under
  ``ast.parse(feature_version=...)`` so a 3.14-only construct fails here
  instead of at import time on the deployed host.
* **Dependencies.** Every import must resolve to the standard library or to
  this package. There is no requirements file to keep honest, so the rule is
  read off :data:`sys.stdlib_module_names` instead of off a pin.
* **Imports.** Nothing here may import ``agent_session_loop``. The Mac keeps
  the authoritative worker runtime and the providers that call it; the portable
  package receives what it needs through parameters and seams. This is the rule
  that stops the consolidation from becoming a second implementation, and it is
  why the check is a scan of the source rather than a convention.

:func:`check_boundary` runs all three and returns one report. The scan is
static -- it parses the package's files -- so an import buried in a function
body is caught as surely as a module-level one, which a namespace check after
a successful import would miss.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: This package's dotted name and its directory. Both are derived rather than
#: written twice so a rename cannot leave one of them behind.
PACKAGE_NAME = __name__.split(".")[0]
PACKAGE_ROOT = Path(__file__).resolve().parent

#: The oldest interpreter the portable package supports, and the series it
#: stops at. The floor preserves the deployed Pi contract inherited during
#: migration; raising it requires a deliberate deployment compatibility change.
MINIMUM_PYTHON_VERSION: tuple[int, int] = (3, 13)
MAXIMUM_PYTHON_SERIES: tuple[int, int] = (4, 0)

#: :class:`RuntimeSupport` reasons. ``supported`` is the only one that is not a
#: refusal, so callers test the reason rather than pattern-match a message.
REASON_SUPPORTED = "supported"
REASON_BELOW_MINIMUM = "below-minimum"
REASON_UNSUPPORTED_SERIES = "unsupported-series"

#: Import roots the portable package may never reach for. ``agent_session_loop``
#: is the whole point: it is the Mac-only worker runtime and authority.
FORBIDDEN_IMPORT_ROOTS: frozenset[str] = frozenset({"agent_session_loop"})

#: :class:`ImportFinding` kinds. The two failures are different problems --
#: one is a layering violation, the other is an undeclared dependency the Pi
#: has no way to install -- so they are reported apart.
KIND_FORBIDDEN = "forbidden-root"
KIND_THIRD_PARTY = "third-party"


class BoundaryError(Exception):
    """Raised when a boundary check cannot be performed at all."""


@dataclass(frozen=True)
class RuntimeSupport:
    """Whether one interpreter version may run the portable package."""

    version: tuple[int, ...]
    reason: str

    @property
    def supported(self) -> bool:
        return self.reason == REASON_SUPPORTED

    def describe(self) -> str:
        rendered = ".".join(str(part) for part in self.version)
        if self.supported:
            return f"Python {rendered} is supported"
        floor = ".".join(str(part) for part in MINIMUM_PYTHON_VERSION)
        series = ".".join(str(part) for part in MAXIMUM_PYTHON_SERIES)
        if self.reason == REASON_BELOW_MINIMUM:
            return f"Python {rendered} is older than the {floor} floor"
        return f"Python {rendered} is outside the supported series (< {series})"

    def to_payload(self) -> dict[str, object]:
        return {
            "version": list(self.version),
            "reason": self.reason,
            "supported": self.supported,
            "detail": self.describe(),
        }


@dataclass(frozen=True)
class ImportFinding:
    """One import the portable package is not allowed to make."""

    module: str
    line: int
    imported: str
    kind: str

    def describe(self) -> str:
        return f"{self.module}:{self.line} imports {self.imported} ({self.kind})"


@dataclass(frozen=True)
class SyntaxFinding:
    """One module that does not parse under the minimum supported version."""

    module: str
    line: int
    message: str

    def describe(self) -> str:
        floor = ".".join(str(part) for part in MINIMUM_PYTHON_VERSION)
        return (
            f"{self.module}:{self.line} needs newer than Python {floor}: {self.message}"
        )


@dataclass(frozen=True)
class BoundaryReport:
    """The portable package's standing against all three boundaries."""

    runtime: RuntimeSupport
    forbidden_imports: tuple[ImportFinding, ...]
    third_party_imports: tuple[ImportFinding, ...]
    unsupported_syntax: tuple[SyntaxFinding, ...]

    @property
    def ok(self) -> bool:
        return (
            self.runtime.supported
            and not self.forbidden_imports
            and not self.third_party_imports
            and not self.unsupported_syntax
        )

    def problems(self) -> tuple[str, ...]:
        """Every violation as a line naming the file and the fix."""
        lines: list[str] = []
        if not self.runtime.supported:
            lines.append(self.runtime.describe())
        for finding in (*self.forbidden_imports, *self.third_party_imports):
            lines.append(finding.describe())
        for syntax in self.unsupported_syntax:
            lines.append(syntax.describe())
        return tuple(lines)

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "runtime": self.runtime.to_payload(),
            "problems": list(self.problems()),
        }


def check_runtime(version: Sequence[int] | None = None) -> RuntimeSupport:
    """Judge an interpreter version against the portable floor.

    ``None`` asks about the running interpreter. A short tuple is judged on
    what it carries -- ``(3,)`` is below the floor -- because a caller that
    could not name a minor version has not named a supported one.
    """
    resolved = tuple(
        int(part) for part in (sys.version_info[:3] if version is None else version)
    )
    if resolved[:2] >= MAXIMUM_PYTHON_SERIES:
        return RuntimeSupport(resolved, REASON_UNSUPPORTED_SERIES)
    if resolved[:2] < MINIMUM_PYTHON_VERSION:
        return RuntimeSupport(resolved, REASON_BELOW_MINIMUM)
    return RuntimeSupport(resolved, REASON_SUPPORTED)


def package_module_paths(root: Path | None = None) -> tuple[Path, ...]:
    """Every source file in the portable package, sorted and without caches."""
    base = PACKAGE_ROOT if root is None else Path(root)
    if not base.is_dir():
        raise BoundaryError(f"not a package directory: {base}")
    return tuple(
        sorted(path for path in base.rglob("*.py") if "__pycache__" not in path.parts)
    )


def dotted_name(path: Path, root: Path | None = None) -> str:
    """The dotted name for a package file, rooted at the package directory.

    An ``__init__.py`` is named by its package, matching what mypy and the
    typing ratchet match on, so a finding's module name is the name a
    ``pyproject.toml`` override table would carry.
    """
    base = PACKAGE_ROOT if root is None else Path(root)
    relative = path.resolve().relative_to(base.resolve())
    parts = (base.name, *relative.parts[:-1])
    if path.name != "__init__.py":
        parts = (*parts, relative.stem)
    return ".".join(parts)


def _imported_roots(tree: ast.Module) -> Iterator[tuple[int, str, str]]:
    """Yield ``(line, imported name, root package)`` for every absolute import.

    Relative imports are skipped: ``from . import capabilities`` names a
    sibling, which is neither a dependency nor a layering violation.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            yield node.lineno, node.module, node.module.split(".")[0]


def package_sources(root: Path | None = None) -> dict[str, str]:
    """Every module in the package as ``dotted name -> source text``.

    The scans below take sources rather than paths so the same three rules can
    be applied to a packaged artifact, whose members are bytes in a zip and
    have no directory to walk. Reading the files is the only part that differs
    between "the checkout" and "what was built from it", and it is this
    function.
    """
    base = PACKAGE_ROOT if root is None else Path(root)
    return {
        dotted_name(path, base): path.read_text(encoding="utf-8")
        for path in package_module_paths(base)
    }


def source_imports(
    module: str, source: str, *, package: str = PACKAGE_NAME
) -> tuple[ImportFinding, ...]:
    """Every import in one module that the portable package may not make.

    Findings carry their kind so a layering violation is not reported as a
    missing wheel: an ``agent_session_loop`` import is :data:`KIND_FORBIDDEN`,
    anything else unknown is :data:`KIND_THIRD_PARTY`.
    """
    allowed = set(sys.stdlib_module_names) | {package}
    tree = ast.parse(source, filename=module)
    findings: list[ImportFinding] = []
    for line, imported, package_root in _imported_roots(tree):
        if package_root in FORBIDDEN_IMPORT_ROOTS:
            findings.append(ImportFinding(module, line, imported, KIND_FORBIDDEN))
        elif package_root not in allowed:
            findings.append(ImportFinding(module, line, imported, KIND_THIRD_PARTY))
    return tuple(findings)


def source_syntax(module: str, source: str) -> tuple[SyntaxFinding, ...]:
    """Whether one module needs a newer interpreter than the floor allows.

    ``ast.parse`` with ``feature_version`` is the only mechanism that answers
    this without a second interpreter on the machine. It rejects grammar the
    named version could not parse, which is the class of breakage that reaches
    the Pi as an import failure rather than as a test failure here.
    """
    try:
        ast.parse(source, filename=module, feature_version=MINIMUM_PYTHON_VERSION)
    except SyntaxError as error:
        return (SyntaxFinding(module, error.lineno or 0, error.msg),)
    return ()


def external_imports(root: Path | None = None) -> tuple[ImportFinding, ...]:
    """Every import in the package that is neither stdlib nor this package."""
    base = PACKAGE_ROOT if root is None else Path(root)
    return tuple(
        finding
        for module, source in package_sources(base).items()
        for finding in source_imports(module, source, package=base.name)
    )


def unsupported_syntax(root: Path | None = None) -> tuple[SyntaxFinding, ...]:
    """Every module in the package that needs a newer interpreter."""
    return tuple(
        finding
        for module, source in package_sources(root).items()
        for finding in source_syntax(module, source)
    )


def check_sources(
    sources: Mapping[str, str],
    *,
    package: str = PACKAGE_NAME,
    version: Sequence[int] | None = None,
) -> BoundaryReport:
    """Judge a set of modules against all three boundaries.

    This is the check itself, separated from where the source came from. The
    packaging path hands it the members of an artifact, so what is verified is
    what will be installed rather than what happens to be in the working tree
    beside it.

    Syntax is decided first, and a module that fails it is not scanned for
    imports. Import scanning parses with *this* interpreter, so a module the
    running Python cannot read at all would raise out of the report instead of
    appearing in it -- which is precisely the case the Pi's interpreter hits
    when it checks an artifact built on the Mac.
    """
    syntax = tuple(
        finding
        for module in sorted(sources)
        for finding in source_syntax(module, sources[module])
    )
    unreadable = {finding.module for finding in syntax}
    imports = tuple(
        finding
        for module in sorted(sources)
        if module not in unreadable
        for finding in source_imports(module, sources[module], package=package)
    )
    return BoundaryReport(
        runtime=check_runtime(version),
        forbidden_imports=tuple(
            finding for finding in imports if finding.kind == KIND_FORBIDDEN
        ),
        third_party_imports=tuple(
            finding for finding in imports if finding.kind == KIND_THIRD_PARTY
        ),
        unsupported_syntax=syntax,
    )


def check_boundary(
    root: Path | None = None,
    *,
    version: Sequence[int] | None = None,
) -> BoundaryReport:
    """Run the version, dependency, and import checks and report all three."""
    base = PACKAGE_ROOT if root is None else Path(root)
    return check_sources(package_sources(base), package=base.name, version=version)


def blocking_meta_path_source(roots: Sequence[str] | None = None) -> str:
    """Python that refuses the forbidden roots, for an isolated load check.

    Returned as source rather than installed here because the only honest proof
    that the package loads without ASL is a fresh interpreter: this one has
    already imported ``agent_session_loop`` by the time any test in the suite
    runs, so a namespace check here would pass no matter what the package did.
    ``tests/test_workbench_foundations.py`` feeds this to ``python3 -c``.
    """
    blocked = sorted(FORBIDDEN_IMPORT_ROOTS if roots is None else roots)
    return (
        "import sys\n"
        f"_blocked = {blocked!r}\n"
        "class _Refuse:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in _blocked:\n"
        "            raise ImportError('blocked by the portable boundary: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Refuse())\n"
    )
