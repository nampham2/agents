"""End-to-end tests for automatic task-specific parallel execution."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import execution_adapter
import execution_ops
import manage_workspace
from execution_adapter import OBSERVE_FINISHED, OBSERVE_RUNNING, FakeAdapter, SubprocessAdapter
from execution_ops import (
    AutomaticRunReport,
    OperationError,
    _automatic_baseline_matches,
    _automatic_checks,
    _digest_automatic_path,
    _git_automatic,
    _integrate_automatic_attempts,
    _mapped_check_cwd,
    _plan_refusal,
    _plans_conflict,
    _publish_automatic_integration,
    _resolve_automatic_plan,
    _run_plan_checks,
    _snapshot_automatic_plan,
    _wait_for_automatic_attempt,
    run_automatic_tasks,
    run_parallel_tasks,
)
from execution_plans import PlanError
from execution_workers import NESTING_ENV, WorkerError, commit_worker_result, create_worktree
from manage_workspace import _build_parser

TIMESTAMP = "2026-09-16T10:00:00Z"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _repository(root: Path) -> Path:
    target = root / "target"
    target.mkdir()
    _git(target, "init", "-b", "main")
    (target / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(target, "add", "README.md")
    _git(
        target,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    return target


def _task(
    task_id: str,
    output: str,
    *,
    verification: str | None = None,
    effect: str = "local_write",
    root: str = "target",
    depends_on: list[str] | None = None,
    reads: list[str] | None = None,
) -> dict:
    return {
        "id": task_id,
        "name": f"Create {output}",
        "status": "TODO",
        "depends_on": depends_on or [],
        "reads": reads if reads is not None else [],
        "outputs": [{"root": root, "path": output, "required": True}],
        "success_criteria": f"{output} exists",
        "verification": verification or f"test -f {output}",
        "effect": {"kind": effect, "description": "test effect"},
        "authorization": {
            "required": effect in {"destructive", "external"},
            "status": "pending" if effect in {"destructive", "external"} else "not_required",
            "scope": "test" if effect in {"destructive", "external"} else None,
            "source": None,
            "authorized_at": None,
        },
        "receipts": [],
        "evidence": [],
        "skip_reason": None,
        "block_reason": None,
    }


def _project(root: Path, target: Path, tasks: list[dict], *, schema_version: int = 4) -> Path:
    project = root / "workspace" / "2026-09-16-001"
    project.mkdir(parents=True)
    (project / "evidence.md").write_text("# Evidence\n", encoding="utf-8")
    state = {
        "schema_version": schema_version,
        "project": project.name,
        "title": "Automatic execution fixture",
        "status": "EXECUTING",
        "created": TIMESTAMP,
        "updated": TIMESTAMP,
        "working_directory": str(target),
        "revision": 0,
        "current_tasks": [],
        "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
        "cancellation_reason": None,
        "tasks": tasks,
        "execution": {
            "protocol_version": 1,
            "coordinator_run": None,
            "ownership_generation": 0,
            "attempts": {},
        },
    }
    (project / "project.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return project


def _worker(root: Path, *, mode: str = "write", delay: float = 0.0) -> Path:
    script = root / f"worker-{mode}-{str(delay).replace('.', '_')}"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('fixture-worker 1.0')\n"
        "    raise SystemExit(0)\n"
        "print(f'START {time.time()}', flush=True)\n"
        f"time.sleep({delay!r})\n"
        f"mode = {mode!r}\n"
        "prompt = sys.argv[-1]\n"
        "assert os.environ.get('RESEARCH_PROJECT_WORKER') == '1'\n"
        "marker = 'You may modify only these repository-relative files:\\n'\n"
        "output = prompt.split(marker, 1)[1].splitlines()[0].removeprefix('- ')\n"
        "if mode == 'write':\n"
        "    path = Path(output)\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_text('completed\\n', encoding='utf-8')\n"
        "print(f'END {time.time()}', flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class AutomaticExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = _repository(self.root)

    def test_two_tasks_overlap_verify_integrate_and_preserve_clean_target(self) -> None:
        project = _project(
            self.root,
            self.target,
            [_task("T01", "one.txt"), _task("T02", "two.txt")],
        )
        worker = _worker(self.root, delay=0.8)
        baseline = _git(self.target, "rev-parse", "HEAD")

        report = run_automatic_tasks(
            project,
            worker_executables={"claude": str(worker), "codex": str(worker)},
        )

        self.assertEqual(["T01", "T02"], sorted(report.completed))
        self.assertEqual({}, report.blocked)
        self.assertEqual("completed\n", (self.target / "one.txt").read_text(encoding="utf-8"))
        self.assertEqual("completed\n", (self.target / "two.txt").read_text(encoding="utf-8"))
        self.assertEqual("", _git(self.target, "status", "--porcelain=v1"))
        self.assertEqual(2, int(_git(self.target, "rev-list", "--count", f"{baseline}..HEAD")))

        state = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual({"T01": "DONE", "T02": "DONE"}, {t["id"]: t["status"] for t in state["tasks"]})
        self.assertIsNone(state["execution"]["coordinator_run"])
        self.assertTrue(all(task["evidence"] for task in state["tasks"]))
        recorded = json.loads((project / "execution" / "runtime" / "automatic-run.json").read_text())
        self.assertEqual(["T01", "T02"], recorded["completed"])
        intervals = []
        for log in (project / "execution" / "attempts").glob("*/stdout.txt"):
            lines = log.read_text(encoding="utf-8").splitlines()
            intervals.append((float(lines[0].split()[1]), float(lines[-1].split()[1])))
        self.assertEqual(2, len(intervals))
        self.assertLess(max(start for start, _end in intervals), min(end for _start, end in intervals))

    def test_missing_output_blocks_task_without_changing_target(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "missing.txt")])
        worker = _worker(self.root, mode="skip")
        baseline = _git(self.target, "rev-parse", "HEAD")

        report = run_automatic_tasks(project, worker_executables={"claude": str(worker)})

        self.assertIn("T01", report.blocked)
        self.assertEqual(baseline, _git(self.target, "rev-parse", "HEAD"))
        self.assertFalse((self.target / "missing.txt").exists())
        state = json.loads((project / "project.json").read_text())
        self.assertEqual("BLOCKED", state["tasks"][0]["status"])
        self.assertIsNone(state["execution"]["coordinator_run"])

    def test_failed_check_blocks_task(self) -> None:
        task = _task("T01", "file.txt", verification="test -d file.txt")
        project = _project(self.root, self.target, [task])
        worker = _worker(self.root)

        report = run_automatic_tasks(project, worker_executables={"codex": str(worker)})

        self.assertIn("check check-01 failed", report.blocked["T01"])
        self.assertFalse((self.target / "file.txt").exists())

    def test_changed_declared_baseline_blocks_task(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "file.txt")])
        worker = _worker(self.root)
        with patch("execution_ops._automatic_baseline_matches", return_value=False):
            report = run_automatic_tasks(project, worker_executables={"claude": str(worker)})

        self.assertIn("declared task inputs or outputs changed", report.blocked["T01"])
        self.assertFalse((self.target / "file.txt").exists())

    def test_ineligible_tasks_get_precise_fallback_reasons(self) -> None:
        directory = self.target / "directory"
        directory.mkdir()
        tasks = [
            _task("T01", "destructive.txt", effect="destructive"),
            _task("T02", "publish:test", effect="external", root="external"),
            _task("T03", "directory"),
            _task("T04", "generic.txt", verification="python -c pass"),
            _task("T05", "publish:local-effect", root="external"),
            _task("T06", "workspace.txt", root="workspace"),
        ]
        project = _project(self.root, self.target, tasks)
        worker = _worker(self.root)
        baseline = _git(self.target, "rev-parse", "HEAD")

        report = run_automatic_tasks(project, worker_executables={"claude": str(worker)})

        self.assertEqual(
            {
                "T01": "R-EFFECT-NOT-CONFINED",
                "T02": "R-EFFECT-NOT-CONFINED",
                "T03": "R-DIRECTORY-SUBJECT",
                "T04": "R-UNENUMERABLE",
                "T05": "R-EXTERNAL-REFERENCE",
                "T06": "R-UNENUMERABLE",
            },
            report.fallbacks,
        )
        self.assertEqual(baseline, _git(self.target, "rev-parse", "HEAD"))

    def test_conflict_and_capacity_are_deferred(self) -> None:
        tasks = [
            _task("T01", "same.txt"),
            _task("T02", "same.txt"),
            _task("T03", "third.txt"),
        ]
        project = _project(self.root, self.target, tasks)
        worker = _worker(self.root)

        report = run_automatic_tasks(
            project,
            max_concurrent=1,
            worker_executables={"claude": str(worker)},
        )

        self.assertEqual(["T01"], report.completed)
        self.assertEqual("R-CLAIM-CONFLICT", report.deferred["T02"])
        self.assertEqual("R-CAPACITY", report.deferred["T03"])

    def test_no_adapter_and_no_ready_task_are_reported_without_coordination(self) -> None:
        waiting = _task("T02", "later.txt", depends_on=["T01"])
        predecessor = _task("T01", "first.txt")
        predecessor["status"] = "RUNNING"
        project = _project(self.root, self.target, [predecessor, waiting])
        report = run_automatic_tasks(project, worker_executables={})
        self.assertEqual({}, report.fallbacks)

        state = json.loads((project / "project.json").read_text())
        state["tasks"][0]["status"] = "TODO"
        (project / "project.json").write_text(json.dumps(state, indent=2) + "\n")
        report = run_automatic_tasks(project, worker_executables={})
        self.assertEqual({"T01": "R-NO-ADAPTER"}, report.fallbacks)

    def test_only_executing_tasks_with_done_dependencies_and_authorization_are_ready(self) -> None:
        predecessor = _task("T01", "first.txt")
        dependent = _task("T02", "second.txt", depends_on=["T01"])
        project = _project(self.root, self.target, [predecessor, dependent])
        state = json.loads((project / "project.json").read_text())

        for status in ("SKIPPED", "BLOCKED", "TODO"):
            state["tasks"][0]["status"] = status
            self.assertNotIn("T02", [task["id"] for task in execution_ops._ready_tasks(state)])

        state["tasks"][0]["status"] = "DONE"
        state["tasks"][1]["authorization"] = {
            "required": True,
            "status": "pending",
            "scope": "test",
            "source": None,
            "authorized_at": None,
        }
        self.assertNotIn("T02", [task["id"] for task in execution_ops._ready_tasks(state)])
        state["tasks"][1]["authorization"].update(
            status="explicit",
            source="test",
            authorized_at=TIMESTAMP,
        )
        self.assertIn("T02", [task["id"] for task in execution_ops._ready_tasks(state)])
        state["status"] = "REVIEW"
        self.assertEqual([], execution_ops._ready_tasks(state))

    def test_nested_invalid_capacity_and_legacy_schema_are_refused(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "one.txt")])
        with self.assertRaisesRegex(OperationError, "at least one"):
            run_automatic_tasks(project, max_concurrent=0)

        previous = os.environ.get(NESTING_ENV)
        os.environ[NESTING_ENV] = "1"
        try:
            with self.assertRaisesRegex(OperationError, "nested"):
                run_automatic_tasks(project)
        finally:
            if previous is None:
                os.environ.pop(NESTING_ENV, None)
            else:
                os.environ[NESTING_ENV] = previous

        legacy = _project(self.root / "legacy", self.target, [_task("T01", "legacy.txt")], schema_version=3)
        with self.assertRaisesRegex(OperationError, "schema-v4"):
            run_automatic_tasks(legacy)

    def test_empty_composed_invalid_and_uncovered_checks_are_refused(self) -> None:
        task = _task("T01", "one.txt")
        cases = [
            ("", "verification is empty"),
            ("test -f one.txt && test -s one.txt", "composed"),
            ("test 'unterminated", "cannot parse"),
        ]
        for verification, message in cases:
            with self.subTest(verification=verification):
                task["verification"] = verification
                with self.assertRaisesRegex(PlanError, message):
                    _automatic_checks(task, self.target)

        task["verification"] = "\ntest -f one.txt\n\ntest -s one.txt"
        self.assertEqual(2, len(_automatic_checks(task, self.target)))

        task["verification"] = "test -f another.txt"
        project = _project(self.root, self.target, [task])
        state = json.loads((project / "project.json").read_text())
        plan, refusal = _resolve_automatic_plan(state, task, project, "claude")
        self.assertIsNone(plan)
        self.assertEqual("R-CHECK-UNCOVERED", refusal)

    def test_empty_outputs_are_unenumerable(self) -> None:
        task = _task("T01", "one.txt")
        task["outputs"] = []
        self.assertEqual("R-UNENUMERABLE", _plan_refusal(task, self.target))

    def test_missing_exhaustive_reads_declaration_falls_back(self) -> None:
        task = _task("T01", "one.txt")
        del task["reads"]
        project = _project(self.root, self.target, [task])
        state = json.loads((project / "project.json").read_text())

        plan, refusal = _resolve_automatic_plan(state, task, project, "claude")

        self.assertIsNone(plan)
        self.assertEqual("R-UNENUMERABLE", refusal)

        task["reads"] = ["README.md", "README.md"]
        plan, refusal = _resolve_automatic_plan(state, task, project, "claude")
        self.assertIsNone(plan)
        self.assertEqual("R-UNENUMERABLE", refusal)

    def test_wait_handles_timeout_unknown_and_nonzero_exit(self) -> None:
        cases = [
            (OBSERVE_RUNNING, -1.0, 1, "deadline"),
            ("unknown", 1.0, 1, "unknown"),
            (OBSERVE_FINISHED, 1.0, 7, "code 7"),
        ]
        for observation, timeout, exit_code, message in cases:
            with self.subTest(observation=observation):
                adapter = FakeAdapter()
                adapter.enqueue_observe("handle", observation)
                adapter.enqueue_seal("handle", {"variant": "exited", "exit_code": exit_code})
                attempt = cast(
                    Any, SimpleNamespace(launch=SimpleNamespace(adapter=adapter), handle="handle", attestation=None)
                )
                with self.assertRaisesRegex(WorkerError, message):
                    _wait_for_automatic_attempt(attempt, timeout=timeout)
                if observation != OBSERVE_FINISHED:
                    self.assertEqual(["handle"], adapter.terminated)

    def test_check_mapping_and_git_failures_are_explicit(self) -> None:
        outside = {"cwd": str(self.target.parent), "kind": "command", "argv": ["test", "-f", "x"]}
        with self.assertRaisesRegex(WorkerError, "outside"):
            _mapped_check_cwd(outside, self.target, self.target)

        unsupported = {"checks": [{"kind": "criteria"}]}
        with self.assertRaisesRegex(WorkerError, "unsupported"):
            _run_plan_checks(unsupported, self.target, self.target)

        with self.assertRaisesRegex(WorkerError, "not-a-command"):
            _git_automatic(self.target, "not-a-command")

    def test_plan_snapshot_digests_files_absence_and_rejects_nonfiles(self) -> None:
        read = self.target / "README.md"
        missing = self.target / "missing.txt"
        plan = {"reads": [str(read)], "writes": [str(missing)], "checks": []}
        baseline = _snapshot_automatic_plan(plan)
        self.assertEqual(64, len(baseline[str(read)]))
        self.assertEqual("absent", baseline[str(missing)])
        attempt = cast(Any, SimpleNamespace(plan=plan, baseline=baseline))
        self.assertTrue(_automatic_baseline_matches(attempt))

        read.write_text("changed\n", encoding="utf-8")
        self.assertFalse(_automatic_baseline_matches(attempt))
        with self.assertRaisesRegex(WorkerError, "not a regular file"):
            _digest_automatic_path(self.target)
        link = self.target / "link.txt"
        link.symlink_to(read)
        with self.assertRaisesRegex(WorkerError, "not a regular file"):
            _digest_automatic_path(link)

    def test_integration_rejects_missing_commits_and_target_races(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "one.txt")])
        plan = {
            "task_id": "T01",
            "cwd": str(self.target),
            "instruction": "Create one.txt",
            "writes": [str((self.target / "one.txt").resolve())],
            "outputs": [{"root": "target", "path": "one.txt", "required": True}],
            "worker": {"kind": "claude", "arguments": []},
            "checks": [],
        }
        session = create_worktree(plan, project, "attempt-race")
        missing = cast(
            Any,
            SimpleNamespace(launch=SimpleNamespace(session=session), commit=None, task={"id": "T01"}, plan=plan),
        )
        with self.assertRaisesRegex(WorkerError, "no worker commit"):
            _integrate_automatic_attempts(project, [missing])
        with self.assertRaisesRegex(WorkerError, "no accepted commit"):
            _publish_automatic_integration([missing], "bad", project, "branch")

        (session.worktree / "one.txt").write_text("done\n", encoding="utf-8")
        commit = commit_worker_result(session, plan)
        staged = cast(
            Any,
            SimpleNamespace(launch=SimpleNamespace(session=session), commit=commit, task={"id": "T01"}, plan=plan),
        )
        (self.target / "concurrent.txt").write_text("user change\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkerError, "changed before integration"):
            _integrate_automatic_attempts(project, [staged])

    def test_unavailable_worker_and_dirty_target_dispatch_are_fallbacks(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "one.txt")])
        report = run_automatic_tasks(project, worker_executables={"claude": str(self.root / "missing-worker")})
        self.assertEqual({"T01": "R-NO-ADAPTER"}, report.fallbacks)

        worker = _worker(self.root)
        (self.target / "dirty.txt").write_text("user change\n", encoding="utf-8")
        report = run_automatic_tasks(project, worker_executables={"claude": str(worker)})
        self.assertIn("target repository must be clean", report.fallbacks["T01"])

    def test_failure_after_worktree_preparation_requires_recovery(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "one.txt")])
        worker = _worker(self.root)
        with patch("execution_ops._dispatch_automatic_attempt", side_effect=OperationError("interrupted")):
            with self.assertRaisesRegex(OperationError, "run recovery"):
                run_automatic_tasks(project, worker_executables={"claude": str(worker)})

        state = json.loads((project / "project.json").read_text())
        self.assertIsNotNone(state["execution"]["coordinator_run"])
        self.assertTrue(any((project / "execution" / "worktrees").iterdir()))

    def test_integration_failure_blocks_staged_task(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "one.txt")])
        worker = _worker(self.root)
        with patch("execution_ops._integrate_automatic_attempts", side_effect=WorkerError("conflict")):
            report = run_automatic_tasks(project, worker_executables={"claude": str(worker)})
        self.assertIn("integration failed: conflict", report.blocked["T01"])
        state = json.loads((project / "project.json").read_text())
        self.assertEqual("BLOCKED", state["tasks"][0]["status"])

    def test_legacy_parallel_tolerates_missing_seal_attestation(self) -> None:
        project = _project(self.root, self.target, [_task("T01", "one.txt")])
        done = run_parallel_tasks(project, FakeAdapter)
        self.assertEqual(1, done)

    def test_adapter_observe_handles_missing_process(self) -> None:
        attempt_dir = self.root / "attempt"
        attempt_dir.mkdir()
        adapter = SubprocessAdapter(["/usr/bin/true"])
        handle = adapter.start({}, str(attempt_dir))
        try:
            with patch.object(execution_adapter.os, "kill", side_effect=ProcessLookupError):
                self.assertEqual(OBSERVE_FINISHED, adapter.observe(handle))
        finally:
            adapter.terminate(handle)

    def test_plan_conflict_and_explicit_reads_are_detected(self) -> None:
        first = _task("T01", "shared.txt")
        first["status"] = "DONE"
        first["evidence"] = [{"root": "workspace", "path": "evidence.md", "anchor": "T01"}]
        second = _task("T02", "second.txt", depends_on=["T01"], reads=["shared.txt"])
        third = _task("T03", "shared.txt")
        project = _project(self.root, self.target, [first, second, third])
        state = json.loads((project / "project.json").read_text())

        second_plan, refusal = _resolve_automatic_plan(state, second, project, "claude")
        third_plan, _ = _resolve_automatic_plan(state, third, project, "codex")

        self.assertIsNone(refusal)
        assert second_plan is not None and third_plan is not None
        self.assertIn(str((self.target / "shared.txt").resolve()), second_plan["reads"])
        self.assertTrue(_plans_conflict(second_plan, third_plan))

    def test_cli_exposes_run_auto_with_default_concurrency_two(self) -> None:
        args = _build_parser().parse_args(["run-auto", "/tmp/project"])
        self.assertEqual("run-auto", args.command)
        self.assertEqual(2, args.concurrency)

    def test_cli_run_auto_maps_completed_blocked_and_idle_results(self) -> None:
        reports = [
            (AutomaticRunReport(["T01"], {}, {}, {}), 0),
            (AutomaticRunReport([], {"T01": "failed"}, {}, {}), 1),
            (AutomaticRunReport([], {}, {"T01": "R-UNENUMERABLE"}, {}), 2),
        ]
        for report, expected in reports:
            with self.subTest(expected=expected):
                with patch.object(manage_workspace, "run_automatic_tasks", return_value=report):
                    with patch("sys.argv", ["manage", "run-auto", "/tmp/project"]):
                        with patch("sys.stdout", new_callable=io.StringIO) as output:
                            self.assertEqual(expected, manage_workspace.main())
                self.assertIn("completed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
