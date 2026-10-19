"""Compatibility façade for the focused :mod:`.workbench_archive` modules."""

from __future__ import annotations

# ruff: noqa: F401
import json
import secrets
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from asl_workbench import archive as portable_archive

from . import (
    durability,
    plans_errors,
    plans_files,
    state,
    state_locks,
    workbench_discovery,
    workbench_registry,
    workbench_registry_coerce,
    workbench_registry_models,
)
from .workbench_archive_commands import (
    ArchiveRefused,
    ArchiveView,
    BulkArchiveResult,
    RegistryUpdate,
    _mutate,
    _refuse_archive,
    _refuse_request,
    pin_plan,
    set_retention,
    unpin_plan,
)
from .workbench_archive_models import (
    ARCHIVE_DIR_NAME,
    ARCHIVE_METADATA_FILE_NAME,
    ARCHIVE_PLAN_DIR_NAME,
    ARCHIVE_STATE_DIR_NAME,
    ArchiveError,
    ArchiveListing,
    ArchiveMetadata,
    ArchivePlanResult,
    ArchivePurgeOutcome,
    ArchivePurgeResult,
    DeletePlanResult,
    PlanDeleteError,
    RestoreArchiveResult,
)
from .workbench_archive_operations import (
    archive_plan,
    purge_archive_entries,
    restore_archive,
)
from .workbench_archive_storage import (
    _coerce_archive_metadata,
    _is_discovered_plan,
    _is_valid_archive_id,
    _validate_plan_for_mutation,
    archive_entry_dir,
    archive_plan_path,
    archive_root,
    archive_state_path,
    compute_expires_at,
    delete_plan,
    list_archive_entries,
    load_archive_metadata,
    new_archive_id,
    remove_archive_entry,
    save_archive_metadata,
)
from .workbench_archive_workflows import (
    _canonical,
    archive_many,
    archive_one,
    purge_entries,
    restore_entry,
    review_archive,
    unlock,
    unlock_payload,
    workbench_pin_from_args,
    workbench_unpin_from_args,
)
