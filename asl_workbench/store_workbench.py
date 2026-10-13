"""The portable SQLite store: migrations, ordered ingestion, retention, reads.

This is the Companion's store, moved into the package that both halves of the
application can load, and it is a module rather than a handful of queries at
the endpoints because of four properties that are easy to break one endpoint at
a time.

**Ingestion is ordered, and the order is time rather than the generation
counter.** Each upload carries the observation time and the generation of the
process that produced it. The generation is what tells a reconnect from an
emptied corpus, but it is *not* a monotonic sequence across a source's own
restarts -- a process that restarts counts its generations from one again while
its connector id stays the same. Ordering on the counter would therefore refuse
every upload from a restarted source forever. So observation time orders, and
the generation refuses only what time cannot: a late upload from a session that
has already been replaced.

**Ingestion is atomic.** A snapshot is one transaction: the connector row, the
runs it carries, the absence of the runs it does not, the incident evidence,
and the retention pass all commit together or none of them do. A half-applied
snapshot is the one state a cockpit cannot describe honestly, because its row
set would belong to no observation that ever happened.

**Omission is not replacement evidence.** A complete view marks omitted rows
absent; a partial one retains them as ``unconfirmed``. A positively newer run
for the same connector and plan can retire its predecessor even in a partial
view. None of these proves completion; terminal outcomes arrive through
history. The same rule governs the catalog, where it has one extra edge: a
generation is delivered in pages, so a generation that claims to be complete
still cannot retire anything until its *final* page has arrived. Retiring at
page one would empty the catalog on every refresh of a corpus large enough to
need two pages.

**Catalog storage is separate from run history.** They answer different
questions -- what plans exist, and what runs ended -- and the Companion
conflated them by keeping plan labels in a JSON blob in ``service_meta``
keyed by connector. Migration 4 creates a real ``catalog_entry`` table and
carries those blobs into it, so every label and title an installation already
had survives while project membership stops being a copy on every ending.

What is stored is bounded by :mod:`asl_workbench.observation`'s allowlists
rather than by these tables: every column here is named in one of them, or is
ingestion bookkeeping the source never sent.
``tests/test_workbench_store.py`` asserts exactly that against the schema.

Migrations 1 through 3 are the Companion's, statement for statement. An
existing ``companion.sqlite3`` at ``user_version = 3`` therefore skips straight
to migration 4 with every run, ending, operation and incident it holds, and
every identifier those rows are keyed by.
"""

from .store_ingest_catalog import HistoryCatalogIngest
from .store_ingest_runs import RunIngest
from .store_journal import OperationJournal
from .store_reads_catalog import CatalogReads
from .store_reads_runs import RunReads


class WorkbenchStore(
    RunIngest,
    HistoryCatalogIngest,
    OperationJournal,
    RunReads,
    CatalogReads,
):
    """Every read and write this application performs, over one connection."""
