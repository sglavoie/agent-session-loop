# `asl_workbench` — the portable half of the unified Workbench

One HTTP application and one set of browser assets, shared by a Mac local
provider and a Pi remote provider. This package is the half that has to run in
both places; `agent_session_loop` keeps the Mac-only worker runtime and stays
the authority on permission and outcome.

The package is now a deployable application as well as an inventory: it is
packaged as a pinned artifact and started on the Pi by a systemd user unit, and
its modules are loaded in-process on the Mac. The table below names only the
modules this file has been kept current for; `python3 -m asl_workbench` renders
the full matrix, which is the inventory that does not go stale.

## What is here

| Module | What it is |
|---|---|
| `capabilities.py` | The capability/ownership matrix: every route, entry point, workflow, integration, and persistence boundary of both halves, each naming one owner, its authority, its compatibility adapters, and its regression coverage. |
| `changes.py` | The two behaviors the consolidation changes on purpose, with the tests currently pinning the old behavior. |
| `compatibility.py` | The three portability boundaries — Python version, stdlib-only dependencies, no ASL import — in executable form. |
| `companion_inventory.py` | The Companion's pinned module list and route table, plus readers that cross-check the pin against a real Pi checkout. |
| `lifecycle.py` | Packaging and both native lifecycles: a content-addressed artifact of this package for the Pi service, a recorded attachment to the installed ASL for the Mac connector, and the install, upgrade, rollback, recover and remove operations over a single prefix. |
| `migration.py` | The one-way trip from an installed Companion onto this package, and the backup half of the trip back: the state, configuration, checkpoints, journal, credentials and receipts an existing prefix already holds, moved to the names this package opens without changing an identity, repeating an action, or reinstating a revoked credential. |
| `runtime.py` | The service entry point the unit starts: one prefix, one runtime lock, one readiness note, and a `--check` that asks whether a release can read a state before it is allowed to serve it. |
| `server.py` | The HTTP server the runtime binds, and the shutdown handling that lets a service manager stop it without leaving the state locked. |

`python3 -m asl_workbench` renders the matrix as Markdown, and
`python3 -m asl_workbench --json` prints the row counts and the declared
coverage gaps as data. Both validate the matrix first and refuse to print an
invalid one. There is no generated file to go stale: the data is the source of
truth.

## The two rules this package lives under

**It runs on the Pi.** The deployed host is on Python 3.13 while this
repository targets 3.14, so `MINIMUM_PYTHON_VERSION` is `(3, 13)` — the same
floor the Companion installer already enforces — and `pyproject.toml` pins
`asl_workbench/**/*.py` to `py313` in Ruff's `per-file-target-version` table.
The Pi installs no wheels, so every import must resolve to the standard library
or to this package.

**It does not import `agent_session_loop`.** Not at module level, not inside a
function. What it needs arrives through parameters and seams. This is the rule
that stops the consolidation from producing a second implementation, so it is
checked by scanning the source rather than left to convention.

`check_boundary()` runs both. `tests/test_workbench_foundations.py` runs it and
then loads the package in a fresh interpreter with `agent_session_loop` blocked
at `sys.meta_path` — the in-process check cannot prove anything here, because
the test suite has already imported ASL by the time it runs.

## Using the matrix

Every migrated behavior needs a row, and the row is what says which
implementation survives:

- **owner** — the one place the behavior lives. Exactly one.
- **authority** — who remains the source of truth behind it. A row owned by
  this package usually still answers `agent_session_loop`, because ASL decides
  whether a mutation is permitted and what happened. Collapsing owner and
  authority is how an adapter grows into a second implementation.
- **compatibility_adapters** — what must keep translating old requests. An
  adapter may translate; it may not reimplement.
- **regression_coverage** / **coverage_gap** — the tests holding it, or an
  honest statement that nothing automated does.

Adding a route or a CLI subcommand without a row fails
`tests/test_workbench_foundations.py`: the live route table and argument parser
are read and compared against the matrix in both directions.
