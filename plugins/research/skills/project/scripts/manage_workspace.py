#!/usr/bin/env python3
"""Initialize, commit, index, and migrate agentic workspace projects safely."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workspace_lib import (
    EVIDENCE_TAIL_LINES,
    MEMORY_KINDS,
    ROOT_SEARCH_MAX_DEPTH,
    WORKSPACE_ROOT_ENV_VAR,
    WorkspaceError,
    allocate_project,
    amend_memory_topic,
    apply_migration,
    check_candidate,
    commit_candidate,
    find_workspace_roots,
    load_memory_topics,
    migration_candidate,
    read_text,
    rebuild_index,
    record_evidence,
    resolve_workspace_root,
    search_memory,
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
    # Required for a real commit, because the revision a caller claims to have read is what lets
    # the transaction detect a concurrent write. A dry run claims nothing and changes nothing, so
    # it may default to whatever the project currently holds.
    commit.add_argument("--expected-revision", type=int)
    commit.add_argument(
        "--dry-run",
        action="store_true",
        help="report every problem the commit would hit and change nothing; takes no lock",
    )
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

    # `search-memory` takes its root as an option rather than a leading positional, unlike every
    # other command here. With an optional positional root, `search-memory uv` is ambiguous —
    # argparse cannot tell a root from a query — and a query silently read as a root would search
    # nothing and report no hits, which reads exactly like a topic that does not exist.
    search = subparsers.add_parser(
        "search-memory",
        help="Print where a query appears in memory topics and project post-mortems",
        epilog=(
            "Prints locations, never contents: a wide search costs the reader in proportion to the "
            "number of hits rather than the size of the files hit. Topic frontmatter is searched "
            "before post-mortem bodies, because a frontmatter hit means the topic is about the query."
        ),
    )
    search.add_argument("query", help="case-insensitive substring to look for")
    search.add_argument(
        "--workspace-root",
        type=Path,
        help=f"workspace root; defaults to ${WORKSPACE_ROOT_ENV_VAR}",
    )

    promote = subparsers.add_parser(
        "promote-memory",
        help="Amend or create memory/<slug>.md under the memory lock, then regenerate MEMORY.md",
        epilog=(
            "Amends rather than replaces: sources accumulate and the new body lands below what is "
            "already there, because a rewrite loses the incident that made the lesson credible. "
            "A new topic needs --description, --kind, and --scope; amending an existing one does not."
        ),
    )
    promote.add_argument("slug", help="topic filename stem, lowercase and hyphenated")
    body_source = promote.add_mutually_exclusive_group(required=True)
    body_source.add_argument("--body", help="the lesson text")
    body_source.add_argument("--body-file", type=Path, help="read the lesson text from a file, or '-' for stdin")
    promote.add_argument("--description", default="", help="one line; rendered as the pointer in MEMORY.md")
    promote.add_argument("--kind", default="", choices=("", *MEMORY_KINDS), help="which MEMORY.md group it joins")
    promote.add_argument("--scope", default="", help="where the lesson applies, so a reader can rule it out")
    promote.add_argument(
        "--source",
        action="append",
        default=[],
        dest="sources",
        metavar="PROJECT_ID",
        help="YYYY-MM-DD-NNN project this lesson came from; repeat for several",
    )
    promote.add_argument("--updated", default="", help="YYYY-MM-DD; defaults to today")
    promote.add_argument(
        "--workspace-root",
        type=Path,
        help=f"workspace root; defaults to ${WORKSPACE_ROOT_ENV_VAR}",
    )
    promote.add_argument("--lock-timeout", type=float, default=5.0)

    migrate = subparsers.add_parser("migrate", help="Preview or explicitly apply a v1/v2-to-v3 migration")
    migrate.add_argument("project_directory", type=Path)
    migrate.add_argument("--apply", action="store_true", help="Apply the migration; default is a read-only preview")
    migrate.add_argument("--lock-timeout", type=float, default=5.0)

    return parser


def _read_body(path: Path) -> str:
    """Read a lesson body from a file, or from stdin when the path is `-`."""
    if str(path) == "-":
        return sys.stdin.read()
    return read_text(path)


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
            if args.dry_run:
                report = check_candidate(
                    args.project_directory,
                    args.candidate_json,
                    expected_revision=args.expected_revision,
                )
                for warning in report.warnings:
                    print(f"WARNING: {warning}", file=sys.stderr)
                if report.errors:
                    print("Dry run: the commit would fail:", file=sys.stderr)
                    for error in report.errors:
                        print(f"- {error}", file=sys.stderr)
                    return 1
                print(f"Dry run: the candidate would commit cleanly; {args.project_directory.resolve()} is unchanged")
                return 0
            if args.expected_revision is None:
                raise WorkspaceError("commit requires --expected-revision; pass --dry-run to check without committing")
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
            # A malformed topic file is skipped by generation rather than raised, so that a bad
            # memory file can never block the commit this command ends. Skipping silently would
            # leave a lesson invisible with nothing said, so the skip is reported here.
            for problem in load_memory_topics(workspace_root)[1]:
                print(f"WARNING: memory topic skipped: {problem}", file=sys.stderr)
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

        if args.command == "search-memory":
            workspace_root = resolve_workspace_root(args.workspace_root)
            hits = search_memory(workspace_root, args.query)
            for path, number, line in hits:
                print(f"{path.relative_to(workspace_root)}:{number}: {line}")
            if not hits:
                # Not an error. A query with no hits is the answer to "is there a lesson about this",
                # and exiting non-zero would make an honest "nothing recorded" look like a failure.
                print(f"No memory matches {args.query!r} under {workspace_root}", file=sys.stderr)
            return 0

        if args.command == "promote-memory":
            workspace_root = resolve_workspace_root(args.workspace_root)
            body = args.body if args.body is not None else _read_body(args.body_file)
            path = amend_memory_topic(
                workspace_root,
                args.slug,
                body=body,
                description=args.description,
                kind=args.kind,
                scope=args.scope,
                sources=args.sources,
                updated=args.updated,
                lock_timeout=args.lock_timeout,
            )
            # Regenerated after the memory lock is released, never inside it: the rebuild takes the
            # index lock, and these mkdir-based locks are not reentrant across each other's holders.
            rebuild_index(workspace_root, lock_timeout=args.lock_timeout)
            print(path)
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
