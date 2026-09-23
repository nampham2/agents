#!/usr/bin/env python3
"""Initialize, commit, index, and migrate agentic workspace projects safely."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from execution_adapter import FakeAdapter, SubprocessAdapter
from execution_ops import OperationError, run_automatic_tasks, run_parallel_tasks, run_sequential_task
from memory_search import rank_topics, search_postmortems
from workspace_documents import WHOLE_DOCUMENTS, append_record, document_snapshot, edit_document
from workspace_evidence import evidence_entries
from workspace_lib import (
    CLOSURE_STEPS,
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
    enable_execution,
    find_workspace_roots,
    load_memory_topics,
    migration_candidate,
    project_task_graph,
    read_text,
    rebuild_index,
    record_evidence_result,
    render_task_graph,
    resolve_workspace_root,
    validate_v3_state,
    vcs_warnings,
)
from workspace_operations import CloseOperationError, close_project, task_operation
from workspace_session import (
    list_projects,
    project_context,
    read_project_text,
    update_project,
    update_project_data,
    validated_project_context,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Atomically allocate and initialize a v4 project")
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
    initialize.add_argument("--briefing", action="store_true", help="also scaffold an optional discovery briefing")

    context = subparsers.add_parser("context", help="Print bounded resume context without completed task history")
    context.add_argument("project_directory", type=Path)
    context.add_argument("--limit", type=int, default=5)
    context.add_argument("--task", help="include one complete task definition")
    context.add_argument(
        "--worker", action="store_true", help="return only the selected task and dependency references"
    )
    context.add_argument(
        "--task-only", action="store_true", help="coordinator task view without repeating resume context"
    )
    context.add_argument("--validate", action="store_true", help="include full project validation findings")

    read = subparsers.add_parser("read", help="Read a bounded specification section or evidence excerpt")
    read.add_argument("project_directory", type=Path)
    read.add_argument("document", choices=("spec", "evidence", "reflection", "architecture", "notes"))
    read.add_argument("--section", help="exact specification heading; default excludes decision history")
    read_owner = read.add_mutually_exclusive_group()
    read_owner.add_argument("--task")
    read_owner.add_argument("--step", choices=CLOSURE_STEPS)
    read.add_argument("--offset", type=int, default=0, help="character offset within selected text")
    read.add_argument("--max-chars", type=int, default=4000)
    read.add_argument("--outline", action="store_true", help="return document headings without their bodies")
    read.add_argument("--entries", action="store_true", help="return structured evidence entry metadata")
    read.add_argument("--entry", help="select one generated evidence record ID")
    read.add_argument("--limit", type=int, default=20, help="maximum structured evidence entries")

    listing = subparsers.add_parser("list-projects", help="Find projects without loading the full workspace index")
    listing.add_argument("workspace_root", nargs="?", type=Path)
    listing.add_argument("--query", default="")
    listing.add_argument("--status")
    listing.add_argument("--limit", type=int, default=10)
    listing.add_argument("--offset", type=int, default=0)

    update = subparsers.add_parser("update", help="Merge a small JSON patch through the guarded commit path")
    update.add_argument("project_directory", type=Path)
    update.add_argument("patch_json", type=Path)
    update.add_argument("--expected-revision", type=int, required=True)
    update.add_argument("--lock-timeout", type=float, default=5.0)
    update.add_argument("--json", action="store_true", help="print a compact structured mutation result")

    edit = subparsers.add_parser("edit", help="Guardedly replace project-owned Markdown content")
    edit.add_argument("project_directory", type=Path)
    edit.add_argument("document", choices=("spec", "reflection", "architecture"))
    edit.add_argument("--section", help="exact specification heading to replace")
    edit.add_argument("--sections-json", type=Path, help="JSON heading-to-body mapping, or '-' for stdin")
    edit_body = edit.add_mutually_exclusive_group()
    edit_body.add_argument("--body")
    edit_body.add_argument("--body-file", type=Path)
    edit.add_argument("--expected-sha256", required=True)
    edit.add_argument("--lock-timeout", type=float, default=5.0)

    append = subparsers.add_parser("append", help="Append a decision or task finding idempotently")
    append.add_argument("project_directory", type=Path)
    append.add_argument("kind", choices=("decision", "finding"))
    append.add_argument("--task")
    append_body = append.add_mutually_exclusive_group(required=True)
    append_body.add_argument("--body")
    append_body.add_argument("--body-file", type=Path)
    append.add_argument("--entry-id")
    append.add_argument("--lock-timeout", type=float, default=5.0)

    task = subparsers.add_parser("task", help="Apply one guarded task transition")
    task.add_argument("project_directory", type=Path)
    task.add_argument("action", choices=("start", "finish", "block", "skip"))
    task.add_argument("task_id")
    task.add_argument("--expected-revision", type=int, required=True)
    task.add_argument("--reason")
    task.add_argument("--evidence", action="append", default=[], dest="evidence_records")
    task.add_argument("--start-next")
    task.add_argument("--lock-timeout", type=float, default=5.0)

    close = subparsers.add_parser("close", help="Write an optional reflection and guardedly close a project")
    close.add_argument("project_directory", type=Path)
    close.add_argument("--expected-revision", type=int, required=True)
    close_body = close.add_mutually_exclusive_group()
    close_body.add_argument("--reflection")
    close_body.add_argument("--reflection-file", type=Path)
    close.add_argument("--expected-reflection-sha256")
    close.add_argument("--lock-timeout", type=float, default=5.0)

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
        usage="manage_workspace.py record-evidence <project-directory> (--task <id> | --step <name>) -- <command>",
        epilog=(
            "The command after '--' is executed verbatim with no shell. The separator is required: "
            "without it a command's own flags are indistinguishable from this script's. Evidence "
            "belongs either to a task or to a closure step, which runs once every task is terminal "
            "and so has no task ID to record under."
        ),
    )
    record.add_argument("project_directory", type=Path)
    owner = record.add_mutually_exclusive_group(required=True)
    owner.add_argument("--task", help="task ID the evidence belongs to; must exist in project.json")
    owner.add_argument(
        "--step",
        choices=CLOSURE_STEPS,
        help="closure step the evidence belongs to, for evidence recorded after the last task",
    )
    record.add_argument("--tail-lines", type=int, default=EVIDENCE_TAIL_LINES)
    record.add_argument("--timeout", type=float, default=None, help="seconds before the command is abandoned")
    record.add_argument("--json", action="store_true", help="print the selectable evidence record as JSON")

    graph = subparsers.add_parser(
        "show-graph",
        help="Print a project's task graph, and what execution has done to it so far",
        epilog=(
            "Read-only: it opens project.json and evidence.md and writes nothing. One form serves "
            "both moments it is wanted at, because the state already says which is meaningful — "
            "before execution it describes a plan, and once anything has run it also carries each "
            "task's status and the span its recorded evidence covers."
        ),
    )
    graph.add_argument("project_directory", type=Path)

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
        help="Rank memory topics for a query and print bounded JSON with the best excerpt per hit",
        epilog=(
            "Ranks whole topics (name, description, scope, keywords and body) with BM25 and excerpts "
            "the best paragraph, so a hit says where to read. Post-mortem bodies are searched by "
            "substring only with --include-postmortems: on a real root they outnumber topic hits a "
            "hundred to one. Retired topics are skipped unless --include-retired."
        ),
    )
    search.add_argument("query", help="free text; a title, an objective, or a few distinctive terms")
    search.add_argument("--limit", type=int, default=5, help="hits per page, 1 to 20 (default 5)")
    search.add_argument("--offset", type=int, default=0)
    search.add_argument(
        "--include-postmortems", action="store_true", help="also list substring hits in reflection.md files"
    )
    search.add_argument("--include-retired", action="store_true", help="rank retired topics too")
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

    enable = subparsers.add_parser(
        "enable-execution",
        help="Activate the parallel execution protocol on a schema v3 project (irreversible)",
        epilog=(
            "Probes the store's filesystem, writes config.json with probe results and §15.1 "
            "settings, and commits schema_version 3 → 4. Idempotent: a second call on an "
            "already-enabled project prints 'already enabled at generation G' and exits zero. "
            "--legacy-writers-quiesced attests that every installation with write access to this "
            "workspace or working directory has been upgraded."
        ),
    )
    enable.add_argument("project_directory", type=Path)
    enable.add_argument(
        "--expected-revision",
        type=int,
        required=True,
        help="revision the caller read from project.json; checked before any filesystem work",
    )
    enable.add_argument(
        "--legacy-writers-quiesced",
        action="store_true",
        help="attest that no legacy (pre-v4) installation can mutate this workspace or working directory",
    )
    enable.add_argument("--lock-timeout", type=float, default=5.0)

    run_once = subparsers.add_parser(
        "run-once",
        help="Run one READY task to completion using the parallel execution protocol",
        epilog=(
            "Acquires the coordinator run (O19), finds the first READY task, executes the "
            "full O1-O16 lifecycle with a FakeAdapter, then relinquishes (O20). Exits 0 when "
            "a task ran, exits 2 when no READY task exists. The project must have been "
            "activated with enable-execution first."
        ),
    )
    run_once.add_argument("project_directory", type=Path)
    run_once.add_argument("--lock-timeout", type=float, default=5.0)

    run_auto = subparsers.add_parser(
        "run-auto",
        help="Automatically run eligible READY tasks with Claude or Codex workers",
        epilog=(
            "Resolves immutable task plans, dispatches up to --concurrency non-conflicting "
            "none/local_write tasks in isolated Git worktrees, verifies and serially integrates "
            "their commits, and reports precise fallback reasons for ineligible tasks."
        ),
    )
    run_auto.add_argument("project_directory", type=Path)
    run_auto.add_argument("--concurrency", type=int, default=2)
    run_auto.add_argument("--lock-timeout", type=float, default=5.0)

    run_parallel = subparsers.add_parser(
        "run-parallel",
        help="Run up to N READY tasks concurrently using real subprocess adapters",
        epilog=(
            "Acquires the coordinator run (O19), finds up to --concurrency READY tasks, "
            "executes each in a thread with its own SubprocessAdapter, then relinquishes (O20). "
            "Exits 0 when tasks ran, exits 2 when no READY task exists. The project must have "
            "been activated with enable-execution first. Pass -- <command> to specify the "
            "subprocess command; defaults to a no-op Python one-liner."
        ),
    )
    run_parallel.add_argument("project_directory", type=Path)
    run_parallel.add_argument("--concurrency", type=int, default=2)
    run_parallel.add_argument("--lock-timeout", type=float, default=5.0)
    run_parallel.add_argument(
        "subprocess_command",
        nargs="*",
        metavar="command",
        help="Command to run for each task (default: python -c 'pass')",
    )

    return parser


def _read_body(path: Path) -> str:
    """Read a lesson body from a file, or from stdin when the path is `-`."""
    if str(path) == "-":
        return sys.stdin.read()
    return read_text(path)


def _read_json_object(path: Path) -> dict[str, object]:
    """Read a JSON object from a file or stdin, normalizing parse/type failures."""
    try:
        value = json.loads(_read_body(path))
    except json.JSONDecodeError as error:
        raise WorkspaceError(f"cannot decode JSON input: {error}") from error
    if not isinstance(value, dict):
        raise WorkspaceError("JSON input must be an object")
    return value


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
                briefing=args.briefing,
            )
            # Warned at init as well as at validation, because this is the moment someone chooses
            # where the record of the work will live. Checked after allocation, since --create-root
            # means the directory to check may not have existed a moment ago.
            for warning in vcs_warnings(workspace_root):
                print(f"WARNING: {warning}", file=sys.stderr)
            print(project_dir)
            return 0

        if args.command == "context":
            if args.validate:
                if args.task is not None or args.worker or args.task_only:
                    raise WorkspaceError("context --validate cannot be combined with task projections")
                result = validated_project_context(args.project_directory, limit=args.limit)
                print(json.dumps(result, indent=2))
                return 0 if result["validation"]["valid"] else 1
            print(json.dumps(project_context(
                args.project_directory, limit=args.limit, task_id=args.task,
                worker=args.worker, task_only=args.task_only,
            ), indent=2))
            return 0

        if args.command == "read":
            if args.document == "evidence" and (args.entries or args.entry is not None):
                print(json.dumps(evidence_entries(
                    args.project_directory, task_id=args.task, step=args.step, record_id=args.entry,
                    offset=args.offset, limit=args.limit, include_text=args.entry is not None,
                ), indent=2))
                return 0
            if args.document in ("reflection", "architecture", "notes") or args.outline:
                if args.section is not None or args.step is not None:
                    raise WorkspaceError("whole-document and outline reads do not accept --section or --step")
                print(json.dumps(document_snapshot(
                    args.project_directory, args.document, task_id=args.task, outline=args.outline,
                    offset=args.offset, max_chars=args.max_chars,
                ), indent=2))
                return 0
            print(json.dumps(read_project_text(
                args.project_directory, args.document, section=args.section, task_id=args.task, step=args.step,
                offset=args.offset, max_chars=args.max_chars,
            ), indent=2))
            return 0

        if args.command == "list-projects":
            print(json.dumps(list_projects(
                resolve_workspace_root(args.workspace_root), query=args.query, status=args.status,
                limit=args.limit, offset=args.offset,
            ), indent=2))
            return 0

        if args.command == "update":
            if str(args.patch_json) == "-":
                patch = _read_json_object(args.patch_json)
                state = update_project_data(
                    args.project_directory, patch,
                    expected_revision=args.expected_revision, lock_timeout=args.lock_timeout,
                )
            else:
                state = update_project(
                    args.project_directory, args.patch_json,
                    expected_revision=args.expected_revision, lock_timeout=args.lock_timeout,
                )
            if args.json:
                print(json.dumps({
                    "operation": "project.update", "project_directory": str(args.project_directory.resolve()),
                    "committed": True, "revision": state["revision"], "status": state["status"],
                    "current_tasks": state["current_tasks"],
                }, indent=2))
                return 0
            print(f"Committed revision {state['revision']}: {args.project_directory.resolve()}")
            return 0

        if args.command == "edit":
            if args.sections_json is not None:
                if args.section is not None or args.body is not None or args.body_file is not None:
                    raise WorkspaceError("--sections-json cannot be combined with --section or body options")
                raw_sections = _read_json_object(args.sections_json)
                if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_sections.items()):
                    raise WorkspaceError("sections JSON must map heading strings to body strings")
                sections = {str(key): str(value) for key, value in raw_sections.items()}
                body = None
            else:
                if args.document == "spec" and args.section is None:
                    raise WorkspaceError("spec edit requires --section or --sections-json")
                if args.document in WHOLE_DOCUMENTS and args.section is not None:
                    raise WorkspaceError(f"{args.document} edit does not accept --section")
                if args.body is None and args.body_file is None:
                    raise WorkspaceError("edit requires --body or --body-file")
                body = args.body if args.body is not None else _read_body(args.body_file)
                sections = {args.section: body} if args.section is not None else None
            print(json.dumps(edit_document(
                args.project_directory, args.document, expected_sha256=args.expected_sha256,
                body=body if args.document in WHOLE_DOCUMENTS else None, sections=sections,
                lock_timeout=args.lock_timeout,
            ), indent=2))
            return 0

        if args.command == "append":
            if args.kind == "finding" and args.task is None:
                raise WorkspaceError("finding append requires --task")
            if args.kind == "decision" and args.task is not None:
                raise WorkspaceError("decision append does not accept --task")
            body = args.body if args.body is not None else _read_body(args.body_file)
            print(json.dumps(append_record(
                args.project_directory, args.kind, body, task_id=args.task, entry_id=args.entry_id,
                lock_timeout=args.lock_timeout,
            ), indent=2))
            return 0

        if args.command == "task":
            print(json.dumps(task_operation(
                args.project_directory, args.action, args.task_id, expected_revision=args.expected_revision,
                reason=args.reason, evidence_record_ids=args.evidence_records, start_next=args.start_next,
                lock_timeout=args.lock_timeout,
            ), indent=2))
            return 0

        if args.command == "close":
            reflection = args.reflection
            if args.reflection_file is not None:
                reflection = _read_body(args.reflection_file)
            result = close_project(
                args.project_directory, expected_revision=args.expected_revision, reflection=reflection,
                expected_reflection_sha256=args.expected_reflection_sha256, lock_timeout=args.lock_timeout,
            )
            print(json.dumps(result, indent=2))
            return 0 if result["validation"]["valid"] else 1

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
            owner = args.task or args.step
            result = record_evidence_result(
                args.project_directory,
                args.task,
                command_argv,
                step=args.step,
                tail_lines=args.tail_lines,
                timeout=args.timeout,
            )
            exit_code = result.exit_code
            if args.json:
                print(json.dumps(result.as_dict(), indent=2))
                return 0 if exit_code == 0 else 1
            evidence_path = args.project_directory.resolve() / "evidence.md"
            if exit_code == 0:
                print(f"Recorded a passing result for {owner} in {evidence_path}")
                return 0
            # The failure is written down, but it is never written down as a pass: a caller that
            # marks the task done from here has to do so against a non-zero exit it can see.
            print(
                f"Recorded exit code {exit_code} for {owner} in {evidence_path}; not recording it as a pass",
                file=sys.stderr,
            )
            return 1

        if args.command == "show-graph":
            print(render_task_graph(project_task_graph(args.project_directory)))
            return 0

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
            if args.offset < 0 or not 1 <= args.limit <= 20:
                raise WorkspaceError("limit must be between 1 and 20; offset must be nonnegative")
            workspace_root = resolve_workspace_root(args.workspace_root)
            hits = rank_topics(workspace_root, args.query, include_retired=args.include_retired)
            end = args.offset + args.limit
            result: dict[str, Any] = {
                "query": args.query,
                "matches": [hit.as_json(workspace_root) for hit in hits[args.offset:end]],
                "total": len(hits),
                "offset": args.offset,
                "next_offset": end if end < len(hits) else None,
            }
            if args.include_postmortems:
                postmortems = search_postmortems(workspace_root, args.query)
                result["postmortems"] = [
                    {"path": str(path.relative_to(workspace_root)), "line": number,
                     "excerpt": line[:300], "truncated": len(line) > 300}
                    for path, number, line in postmortems[args.offset:end]
                ]
                result["postmortem_total"] = len(postmortems)
            print(json.dumps(result, indent=2))
            if not hits and not result.get("postmortems"):
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

        if args.command == "enable-execution":
            enable_execution(
                args.project_directory,
                expected_revision=args.expected_revision,
                legacy_writers_quiesced=args.legacy_writers_quiesced,
                lock_timeout=args.lock_timeout,
            )
            return 0

        if args.command == "run-once":
            ran = run_sequential_task(
                args.project_directory,
                FakeAdapter(),
                lock_timeout=args.lock_timeout,
            )
            if ran:
                print(f"Task completed: {args.project_directory.resolve()}")
                return 0
            print("No READY task found", file=sys.stderr)
            return 2

        if args.command == "run-auto":
            report = run_automatic_tasks(
                args.project_directory,
                max_concurrent=args.concurrency,
                lock_timeout=args.lock_timeout,
            )
            print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
            if report.blocked:
                return 1
            if report.completed:
                return 0
            return 2

        if args.command == "run-parallel":
            cmd = (
                list(args.subprocess_command)
                if args.subprocess_command
                else [
                    "python",
                    "-c",
                    "pass",
                ]
            )
            done = run_parallel_tasks(
                args.project_directory,
                lambda: SubprocessAdapter(cmd),
                max_concurrent=args.concurrency,
                lock_timeout=args.lock_timeout,
            )
            if done:
                print(f"Tasks completed: {done} task(s) in {args.project_directory.resolve()}")
                return 0
            print("No READY task found", file=sys.stderr)
            return 2
    except CloseOperationError as error:
        print(json.dumps(error.result, indent=2))
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (WorkspaceError, OperationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
