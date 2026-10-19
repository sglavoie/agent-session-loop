"""Content-addressed installation for both halves, on their own native managers.

Two roles are deployed, and they are deployed differently on purpose:

* **service** -- the Pi. A pinned zip of this package and nothing else, unpacked
  into ``releases/<sha256>/``, run by a systemd user unit as
  ``python3 -m asl_workbench.runtime --prefix PREFIX``. The artifact is built
  from a commit, verified against an explicit digest, and checked *as packaged
  content* against the portable boundary, so a module that imports the Mac's
  worker runtime or uses grammar the Pi's interpreter cannot parse is refused
  at pack time and again at install time rather than at start-up on the host.
* **connector** -- the Mac. Nothing is packaged. The connector is
  ``asl workbench connect`` from the ASL installation that is already there,
  started by a launch agent, and what the installation records is the
  attachment: which executable, which revision of the checkout behind it, and
  what the two ends may do with each other on the wire. Copying ASL into an
  artifact would create a second ASL, which is the duplication this program
  exists to close; recording the attachment instead makes "which ASL is the Pi
  actually paired with?" a fact in ``deployment.json``.

Everything else is one mechanism:

* **The prefix is provisioned before it is installed into.** Credentials are
  :func:`asl_workbench.integrations.provision`'s to mint, never packaging's.
  An installer that could create a credential could create a *second* one for a
  pairing that already exists.
* **A release is immutable and content-addressed.** Reinstalling the same
  digest verifies the unpacked bytes rather than trusting the directory name,
  and the original zip is retained so a rollback rebuilds a verified artifact
  from bytes rather than from files something may have edited.
* **An interrupted install is recoverable, not ambiguous.** ``transaction.json``
  is written before anything native is touched and removed after the last step;
  while it exists every other operation refuses and points at ``recover``.
* **No implicit schema downgrade.** Both the incoming and the outgoing release
  must read a copy of the state before the switch, so a rollback is known to be
  possible while it is still possible. See
  :func:`asl_workbench.runtime.check_state`.
* **The worker keeps its own process group.** ``AbandonProcessGroup`` on the
  launch agent and ``KillMode=control-group`` on the unit are preserved from
  the Companion's definitions: stopping the connector must not kill the runs an
  operator launched through it, and stopping the service must take its own
  threads with it.

The native identifiers -- ``com.sglavoie.asl-companion`` and
``asl-companion.service`` -- are deliberately unchanged. They name resources an
already-deployed host owns; renaming them would orphan a launch agent and a
unit file that are running right now.


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .installation import (
    BACKUPS_DIRECTORY as BACKUPS_DIRECTORY,
)
from .installation import (
    CONNECTOR_TOKEN_PATH as CONNECTOR_TOKEN_PATH,
)
from .installation import (
    DEFINITIONS_DIRECTORY as DEFINITIONS_DIRECTORY,
)
from .installation import (
    RELEASES_DIRECTORY as RELEASES_DIRECTORY,
)
from .lifecycle_artifacts import (
    artifact_bytes as artifact_bytes,
)
from .lifecycle_artifacts import (
    artifact_members as artifact_members,
)
from .lifecycle_artifacts import (
    check_artifact as check_artifact,
)
from .lifecycle_artifacts import (
    normalize as normalize,
)
from .lifecycle_artifacts import (
    pack as pack,
)
from .lifecycle_artifacts import (
    packable as packable,
)
from .lifecycle_artifacts import (
    unpack as unpack,
)
from .lifecycle_attachment import (
    attached_root as attached_root,
)
from .lifecycle_attachment import (
    attachment as attachment,
)
from .lifecycle_attachment import (
    connector_settings as connector_settings,
)
from .lifecycle_attachment import (
    deployment_role as deployment_role,
)
from .lifecycle_attachment import (
    prerequisites as prerequisites,
)
from .lifecycle_attachment import (
    transport_record as transport_record,
)
from .lifecycle_cli import (
    build_parser as build_parser,
)
from .lifecycle_cli import (
    main as main,
)
from .lifecycle_cli import (
    run as run,
)
from .lifecycle_common import (
    _ALLOWED_MEMBER_TYPES as _ALLOWED_MEMBER_TYPES,
)
from .lifecycle_common import (
    _CONNECT_FLAGS as _CONNECT_FLAGS,
)
from .lifecycle_common import (
    _DIGEST as _DIGEST,
)
from .lifecycle_common import (
    _HELLO_PROBE as _HELLO_PROBE,
)
from .lifecycle_common import (
    _REVISION as _REVISION,
)
from .lifecycle_common import (
    COMMAND_TIMEOUT_SECONDS as COMMAND_TIMEOUT_SECONDS,
)
from .lifecycle_common import (
    DEPLOYMENT_ROLES as DEPLOYMENT_ROLES,
)
from .lifecycle_common import (
    LABEL as LABEL,
)
from .lifecycle_common import (
    MAX_ARTIFACT as MAX_ARTIFACT,
)
from .lifecycle_common import (
    MAX_MEMBERS as MAX_MEMBERS,
)
from .lifecycle_common import (
    PACKAGE_DIRECTORY as PACKAGE_DIRECTORY,
)
from .lifecycle_common import (
    REQUIRED_MEMBERS as REQUIRED_MEMBERS,
)
from .lifecycle_common import (
    ROLE_CONNECTOR as ROLE_CONNECTOR,
)
from .lifecycle_common import (
    ROLE_SERVICE as ROLE_SERVICE,
)
from .lifecycle_common import (
    SOURCE_SUFFIX as SOURCE_SUFFIX,
)
from .lifecycle_common import (
    STATIC_DIRECTORY as STATIC_DIRECTORY,
)
from .lifecycle_common import (
    STATIC_SUFFIXES as STATIC_SUFFIXES,
)
from .lifecycle_common import (
    UNIT as UNIT,
)
from .lifecycle_common import (
    LifecycleError as LifecycleError,
)
from .lifecycle_common import (
    command as command,
)
from .lifecycle_common import (
    executable as executable,
)
from .lifecycle_common import (
    python_check as python_check,
)
from .lifecycle_common import (
    service_environment as service_environment,
)
from .lifecycle_definitions import (
    connector_arguments as connector_arguments,
)
from .lifecycle_definitions import (
    definition as definition,
)
from .lifecycle_definitions import (
    quote_unit as quote_unit,
)
from .lifecycle_definitions import (
    service_arguments as service_arguments,
)
from .lifecycle_definitions import (
    validate_native as validate_native,
)
from .lifecycle_installation import (
    Installation as Installation,
)
from .lifecycle_native import (
    NativeManager as NativeManager,
)
from .lifecycle_state import (
    release_check as release_check,
)
from .lifecycle_state import (
    snapshot as snapshot,
)

__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "DEPLOYMENT_ROLES",
    "LABEL",
    "MAX_ARTIFACT",
    "MAX_MEMBERS",
    "REQUIRED_MEMBERS",
    "ROLE_CONNECTOR",
    "ROLE_SERVICE",
    "STATIC_SUFFIXES",
    "UNIT",
    "Installation",
    "LifecycleError",
    "NativeManager",
    "artifact_bytes",
    "artifact_members",
    "attached_root",
    "attachment",
    "build_parser",
    "check_artifact",
    "command",
    "connector_arguments",
    "connector_settings",
    "definition",
    "deployment_role",
    "executable",
    "main",
    "normalize",
    "pack",
    "packable",
    "python_check",
    "quote_unit",
    "release_check",
    "run",
    "service_arguments",
    "service_environment",
    "snapshot",
    "transport_record",
    "unpack",
    "validate_native",
]


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
