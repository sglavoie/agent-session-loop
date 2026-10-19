"""The canonical plan catalog: one enumeration, split by what may be cached.

Three separate things in the two applications answer the question "what plans
exist": Workbench discovery plus its summary builder, the Companion's
``ProjectCatalog``, and the bridge's ``runs.snapshot`` projection. Only the
first is complete, only the second carries titles and project membership, and
only the third is shaped to cross a network. This module is the one enumeration
the unified application reads, and it is here rather than in
``agent_session_loop`` because the Pi has to be able to hold it.

Four decisions make that possible, and each is data in this module rather than
a convention a reviewer has to remember:

* **The cache-safe / private split is a total partition of the summary.**
  :data:`CACHE_SAFE_FIELDS` and :data:`PRIVATE_FIELDS` name every key a
  ``WorkbenchSummary`` payload carries, and
  :func:`unclassified_summary_fields` reports any key on neither list. A field
  added to the summary therefore fails a test until somebody decides whether it
  may be persisted, which is the opposite of the default that would let a new
  prose field reach the Pi by accident. ``searchable_text`` is the field that
  motivated the split: it concatenates the plan path, the pin label, the pause
  message, and the last-message preview, so it is useful locally and
  unpublishable. :attr:`CatalogEntry.search_text` is its cache-safe
  replacement, built from classified fields only.

* **Identity is a digest, not a path.** :func:`plan_key` and :func:`run_key`
  reproduce the Companion's ``identity`` module exactly -- same JSON encoding,
  same SHA-256, same sixteen characters -- so migrating to this catalog keeps
  every plan and run identity the Pi has already stored. A plan directory names
  a user account and a filesystem layout; the digest is what travels.

* **The catalog is uncapped and delivered in pages.** The bridge's projection
  bounds itself to ``MAX_ROWS`` rows of active and actionable work, which is
  right for an intervention frame and wrong for a catalog: it drops every
  healthy ``ready`` and ``complete`` plan. Nothing here truncates. A bounded
  transport calls :meth:`PlanCatalog.page` instead, and every page carries the
  generation digest it was cut from.

* **An incomplete generation says so.** When the source cannot project a
  discovered plan -- it vanished mid-scan, or its files stopped parsing --
  the catalog is built with ``complete=False``. A consumer must not read a plan
  missing from an incomplete generation as a plan that was deleted, so the flag
  travels on every page rather than being inferred from a warning list.

Nothing in this module touches the filesystem. It is given summary payloads and
manifest text by a provider (``agent_session_loop.workbench_catalog`` on the
Mac) and returns records, which is what keeps the catalog loadable on a host
that has no plans of its own.
"""

from __future__ import annotations

import hashlib as hashlib
import json as json
import re as re
from collections.abc import Iterable as Iterable
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass

from .catalog_labels import _counts as _counts
from .catalog_labels import _session_id as _session_id
from .catalog_labels import _slug as _slug
from .catalog_labels import _sources as _sources
from .catalog_labels import _timestamp as _timestamp
from .catalog_labels import _vocabulary_value as _vocabulary_value
from .catalog_labels import digest_of as digest_of
from .catalog_labels import display_label as display_label
from .catalog_labels import group_label as group_label
from .catalog_labels import manifest_title as manifest_title
from .catalog_labels import origin_name as origin_name
from .catalog_labels import partition_summary as partition_summary
from .catalog_labels import plan_key as plan_key
from .catalog_labels import project_names as project_names
from .catalog_labels import run_key as run_key
from .catalog_labels import safe_label as safe_label
from .catalog_labels import unclassified_summary_fields as unclassified_summary_fields
from .catalog_records import CatalogEntry as CatalogEntry
from .catalog_records import CatalogRecord as CatalogRecord
from .catalog_records import PlanDetail as PlanDetail
from .catalog_records import record_from_summary as record_from_summary
from .catalog_views import CatalogPage as CatalogPage
from .catalog_views import CatalogProject as CatalogProject
from .catalog_views import PlanCatalog as PlanCatalog
from .catalog_views import build_catalog as build_catalog
from .catalog_vocabulary import _PAUSE_REASON_RE as _PAUSE_REASON_RE
from .catalog_vocabulary import _SESSION_ID_RE as _SESSION_ID_RE
from .catalog_vocabulary import _TIMESTAMP_RE as _TIMESTAMP_RE
from .catalog_vocabulary import _TITLE_RE as _TITLE_RE
from .catalog_vocabulary import ACTIVITY_LABELS as ACTIVITY_LABELS
from .catalog_vocabulary import CACHE_SAFE_FIELDS as CACHE_SAFE_FIELDS
from .catalog_vocabulary import COUNT_KEYS as COUNT_KEYS
from .catalog_vocabulary import DEFAULT_PAGE_LIMIT as DEFAULT_PAGE_LIMIT
from .catalog_vocabulary import DISCOVERY_SOURCES as DISCOVERY_SOURCES
from .catalog_vocabulary import KEY_CHARS as KEY_CHARS
from .catalog_vocabulary import LAST_ACTIVITY_SOURCES as LAST_ACTIVITY_SOURCES
from .catalog_vocabulary import MAX_LABEL_CHARS as MAX_LABEL_CHARS
from .catalog_vocabulary import MAX_PROJECTS_PER_PLAN as MAX_PROJECTS_PER_PLAN
from .catalog_vocabulary import PRIMARY_STATES as PRIMARY_STATES
from .catalog_vocabulary import PRIVATE_FIELDS as PRIVATE_FIELDS
from .catalog_vocabulary import RECOMMENDED_ACTIONS as RECOMMENDED_ACTIONS
from .catalog_vocabulary import UNKNOWN_PROJECT as UNKNOWN_PROJECT
from .catalog_vocabulary import UNNAMED_PLAN as UNNAMED_PLAN
from .catalog_vocabulary import URGENCY_GROUPS as URGENCY_GROUPS
from .catalog_vocabulary import CatalogError as CatalogError
