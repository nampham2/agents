#!/usr/bin/env python3
"""CLI entry points for workspace management — manage and validate agentic workspace projects."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agents.workspace.lib import (
    WorkspaceError,
    allocate_project,
    apply_migration,
    commit_candidate,
    migration_candidate,
    rebuild_index,
    validate_project,
    validate_v3_state,
)


def manage() -> int:
    """Initialize, commit, index, and migrate agentic workspace projects safely."""
    parser = argparse.ArgumentParser(description=manage.__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_p = subparsers.add_parser("init", help="Atomically allocate and initialize a v3 project")
    init_p.add_argument("workspace_root", type=Path)
    init_p.add_argument("--title", required=True)
    init_p.add_argument("--working-directory", required=True, type=Path)
    init_p.add_argument("--lock-timeout", type=float, default=5.0)

    commit_p = subparsers.add_parser("commit", help="Commit a complete candidate project.json transactionally")
    commit_p.add_argument("project_directory", type=Path)
    commit_p.add_argument("candidate_json", type=Path)
    commit_p.add_argument("--expected-revision", required=True, type=int)
    commit_p.add_argument("--lock-timeout", type=float, default=5.0)

    index_p = subparsers.add_parser("rebuild-index", help="Regenerate INDEX.md from canonical state")
    index_p.add_argument("workspace_root", type=Path)
    index_p.add_argument("--lock-timeout", type=float, default=5.0)

    migrate_p = subparsers.add_parser("migrate", help="Preview or explicitly apply a v1/v2-to-v3 migration")
    migrate_p.add_argument("project_directory", type=Path)
    migrate_p.add_argument("--apply", action="store_true", help="Apply the migration; default is a read-only preview")
    migrate_p.add_argument("--lock-timeout", type=float, default=5.0)

    args = parser.parse_args()
    try:
        if args.command == "init":
            project_dir = allocate_project(
                args.workspace_root,
                title=args.title,
                working_directory=args.working_directory,
                lock_timeout=args.lock_timeout,
            )
            print(project_dir)
            return 0

        if args.command == "commit":
            state = commit_candidate(
                args.project_directory,
                args.candidate_json,
                expected_revision=args.expected_revision,
                lock_timeout=args.lock_timeout,
            )
            print(f"Committed revision {state['revision']}: {args.project_directory.resolve()}")
            return 0

        if args.command == "rebuild-index":
            print(rebuild_index(args.workspace_root, lock_timeout=args.lock_timeout))
            return 0

        if args.command == "migrate":
            if args.apply:
                print(apply_migration(args.project_directory, lock_timeout=args.lock_timeout))
                return 0
            candidate = migration_candidate(args.project_directory.resolve())
            report = validate_v3_state(
                candidate,
                args.project_directory.resolve(),
                close=candidate.get("status") == "DONE",
                check_files=True,
            )
            print(json.dumps(candidate, indent=2, ensure_ascii=False))
            if report.errors:
                print("\nMigration preview requires reconciliation:", file=sys.stderr)
                for error in report.errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            return 0
    except WorkspaceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


def validate() -> int:
    """Validate an agentic workspace project without mutating it."""
    parser = argparse.ArgumentParser(description=validate.__doc__)
    parser.add_argument("project_directory", type=Path)
    parser.add_argument("--close", action="store_true", help="Enforce completion invariants")
    parser.add_argument(
        "--check-index",
        action="store_true",
        help="Require the parent workspace INDEX.md to match canonical project state",
    )
    parser.add_argument(
        "--allow-legacy-close",
        action="store_true",
        help="Acknowledge the limited guarantees of closing an unmigrated schema-v1 project",
    )
    args = parser.parse_args()

    report = validate_project(
        args.project_directory,
        close=args.close,
        check_index=args.check_index,
        allow_legacy_close=args.allow_legacy_close,
    )
    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if report.errors:
        for error in report.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Workspace valid: {args.project_directory.expanduser().resolve()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(manage())
