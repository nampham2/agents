"""Workspace management library — core logic for the workbench skill."""

from agents.workspace.lib import (
    DirectoryLock,
    ValidationReport,
    WorkspaceConflict,
    WorkspaceError,
    allocate_project,
    apply_migration,
    commit_candidate,
    is_external_reference,
    migrate_v2_state,
    rebuild_index,
    validate_legacy_v1,
    validate_project,
    validate_v3_state,
)

__all__ = [
    "DirectoryLock",
    "ValidationReport",
    "WorkspaceConflict",
    "WorkspaceError",
    "allocate_project",
    "apply_migration",
    "commit_candidate",
    "is_external_reference",
    "migrate_v2_state",
    "rebuild_index",
    "validate_legacy_v1",
    "validate_project",
    "validate_v3_state",
]
