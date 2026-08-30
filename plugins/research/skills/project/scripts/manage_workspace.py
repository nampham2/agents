#!/usr/bin/env python3
"""Initialize, commit, index, and migrate agentic workspace projects safely."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workspace_lib import (
    EVIDENCE_TAIL_LINES,
    ROOT_SEARCH_MAX_DEPTH,
    WORKSPACE_ROOT_ENV_VAR,
    WorkspaceError,
    allocate_project,
    apply_migration,
    commit_candidate,
    find_workspace_roots,
    migration_candidate,
    rebuild_index,
    record_evidence,
    resolve_workspace_root,
    validate_v3_state,
    vcs_warnings,
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

    record = subparsers.add_parser(
        "record-evidence",
        help="Run a command and append its real exit code and output tail to evidence.md",
        usage="manage_workspace.py record-evidence <project-directory> --task <id> -- <command>",
        epilog=(
            "The command after '--' is executed verbatim with no shell. The separator is required: "
            "without it a command's own flags are indistinguishable from this script's."
        ),
    )
    record.add_argument("project_directory", type=Path)
    record.add_argument("--task", required=True, help="task ID the evidence belongs to")
    record.add_argument("--tail-lines", type=int, default=EVIDENCE_TAIL_LINES)
    record.add_argument("--timeout", type=float, default=None, help="seconds before the command is abandoned")

    roots = subparsers.add_parser(
        "find-roots",
        help="Search for established workspace roots instead of guessing one",
        epilog=(
            "Exits 0 only when exactly one root is found. Zero and several both mean the caller "
            "must ask the user, which is what the resolution order already requires."
        ),
    )
    roots.add_argument(
        "search_paths",
        nargs="*",
        type=Path,
        help="directories to search; defaults to $HOME",
    )
    roots.add_argument(
        "--max-depth",
        type=int,
        default=ROOT_SEARCH_MAX_DEPTH,
        help=f"how deep to descend below each search path (default {ROOT_SEARCH_MAX_DEPTH})",
    )

    migrate = subparsers.add_parser("migrate", help="Preview or explicitly apply a v1/v2-to-v3 migration")
    migrate.add_argument("project_directory", type=Path)
    migrate.add_argument("--apply", action="store_true", help="Apply the migration; default is a read-only preview")
    migrate.add_argument("--lock-timeout", type=float, default=5.0)

    return parser


def _split_at_separator(raw: list[str]) -> tuple[list[str], list[str]]:
    """Cut the argument list at the first bare `--`, returning (options, command).

    argparse cannot be trusted with this. `nargs=REMAINDER` swallows `--task` along with the
    command, and `nargs="*"` rejects a command that itself contains a bare `--` on Python 3.9,
    which is the floor the shipped scripts run on. Splitting first keeps the command verbatim.
    """
    if "--" not in raw:
        return raw, []
    separator = raw.index("--")
    return raw[:separator], raw[separator + 1 :]


def main() -> int:
    parser = _build_parser()
    options, command_argv = _split_at_separator(list(sys.argv[1:]))
    args = parser.parse_args(options)
    try:
        if args.command == "init":
            workspace_root = resolve_workspace_root(args.workspace_root)
            project_dir = allocate_project(
                workspace_root,
                title=args.title,
                working_directory=args.working_directory,
                lock_timeout=args.lock_timeout,
                create_root=args.create_root,
            )
            # Warned at init as well as at validation, because this is the moment someone chooses
            # where the record of the work will live. Checked after allocation, since --create-root
            # means the directory to check may not have existed a moment ago.
            for warning in vcs_warnings(workspace_root):
                print(f"WARNING: {warning}", file=sys.stderr)
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

        if args.command == "record-evidence":
            if not command_argv:
                print(
                    "ERROR: record-evidence needs the command after '--', for example: "
                    "record-evidence <project-directory> --task T01 -- uv run pytest",
                    file=sys.stderr,
                )
                return 1
            exit_code = record_evidence(
                args.project_directory,
                args.task,
                command_argv,
                tail_lines=args.tail_lines,
                timeout=args.timeout,
            )
            evidence_path = args.project_directory.resolve() / "evidence.md"
            if exit_code == 0:
                print(f"Recorded a passing result for {args.task} in {evidence_path}")
                return 0
            # The failure is written down, but it is never written down as a pass: a caller that
            # marks the task done from here has to do so against a non-zero exit it can see.
            print(
                f"Recorded exit code {exit_code} for {args.task} in {evidence_path}; not recording it as a pass",
                file=sys.stderr,
            )
            return 1

        if args.command == "find-roots":
            found = find_workspace_roots(args.search_paths or None, max_depth=args.max_depth)
            for root in found:
                print(root)
            if not found:
                where = ", ".join(str(path) for path in args.search_paths) or "$HOME"
                print(
                    f"ERROR: no workspace root found under {where}; ask the user for the path "
                    "rather than creating or assuming one",
                    file=sys.stderr,
                )
                return 1
            if len(found) > 1:
                print(
                    f"ERROR: {len(found)} workspace roots found; that is a defect to resolve, "
                    "not a choice to make silently. Ask the user which one is current.",
                    file=sys.stderr,
                )
                return 1
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
