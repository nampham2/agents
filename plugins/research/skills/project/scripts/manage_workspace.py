#!/usr/bin/env python3
"""Initialize, commit, index, and migrate agentic workspace projects safely."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workspace_lib import (
    WORKSPACE_ROOT_ENV_VAR,
    WorkspaceError,
    allocate_project,
    apply_migration,
    commit_candidate,
    migration_candidate,
    rebuild_index,
    resolve_workspace_root,
    validate_v3_state,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Atomically allocate and initialize a v3 project")
    initialize.add_argument(
        "workspace_root",
        nargs="?",
        type=Path,
        help=f"workspace root; defaults to ${WORKSPACE_ROOT_ENV_VAR}",
    )
    initialize.add_argument("--title", required=True)
    initialize.add_argument("--working-directory", required=True, type=Path)
    initialize.add_argument(
        "--create-root",
        action="store_true",
        help="create the workspace root when it does not exist; without this a missing root is an error",
    )
    initialize.add_argument("--lock-timeout", type=float, default=5.0)

    commit = subparsers.add_parser("commit", help="Commit a complete candidate project.json transactionally")
    commit.add_argument("project_directory", type=Path)
    commit.add_argument("candidate_json", type=Path)
    commit.add_argument("--expected-revision", required=True, type=int)
    commit.add_argument("--lock-timeout", type=float, default=5.0)

    index = subparsers.add_parser("rebuild-index", help="Regenerate INDEX.md from canonical state")
    index.add_argument(
        "workspace_root",
        nargs="?",
        type=Path,
        help=f"workspace root; defaults to ${WORKSPACE_ROOT_ENV_VAR}",
    )
    index.add_argument("--lock-timeout", type=float, default=5.0)

    migrate = subparsers.add_parser("migrate", help="Preview or explicitly apply a v1/v2-to-v3 migration")
    migrate.add_argument("project_directory", type=Path)
    migrate.add_argument("--apply", action="store_true", help="Apply the migration; default is a read-only preview")
    migrate.add_argument("--lock-timeout", type=float, default=5.0)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init":
            project_dir = allocate_project(
                resolve_workspace_root(args.workspace_root),
                title=args.title,
                working_directory=args.working_directory,
                lock_timeout=args.lock_timeout,
                create_root=args.create_root,
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
            workspace_root = resolve_workspace_root(args.workspace_root)
            # An index for a workspace that does not exist is never what the caller wanted, and
            # building one would leave a stray root behind exactly as a mistyped init does.
            if not workspace_root.is_dir():
                raise WorkspaceError(f"workspace root does not exist: {workspace_root}")
            print(rebuild_index(workspace_root, lock_timeout=args.lock_timeout))
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
