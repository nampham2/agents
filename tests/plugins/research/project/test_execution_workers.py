"""Tests for subprocess-backed Claude and Codex worktree workers."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from execution_adapter import OBSERVE_FINISHED, OBSERVE_RUNNING
from execution_workers import (
    NESTING_ENV,
    TargetSnapshot,
    WorkerCapability,
    WorkerError,
    WorkerLaunch,
    WorktreeSession,
    _run,
    assert_target_unchanged,
    build_worker_command,
    changed_paths,
    cleanup_worktree,
    commit_worker_result,
    create_worktree,
    discover_worker,
    inspect_worker_result,
    prepare_worker,
    snapshot_target,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _initialise_repository(root: Path) -> Path:
    repo = root / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    return repo


def _project(root: Path) -> Path:
    project = root / "2026-09-16-001"
    (project / "execution").mkdir(parents=True)
    return project


def _plan(repo: Path, kind: str = "claude", arguments: list[str] | None = None) -> dict:
    return {
        "task_id": "T01",
        "cwd": str(repo),
        "instruction": "Create result.txt containing a short result.",
        "reads": [str(repo / "README.md")],
        "writes": [str(repo / "result.txt")],
        "outputs": [{"root": "target", "path": "result.txt", "required": True}],
        "worker": {"kind": kind, "arguments": arguments or []},
    }


def _fake_worker(root: Path, *, extra_write: bool = False) -> Path:
    script = root / ("fake-worker-extra" if extra_write else "fake-worker")
    extra = "Path('extra.txt').write_text('extra\\n', encoding='utf-8')" if extra_write else ""
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('fake-worker 1.0')\n"
        "    raise SystemExit(0)\n"
        f"assert os.environ.get('{NESTING_ENV}') == '1'\n"
        "Path('result.txt').write_text('done\\n', encoding='utf-8')\n"
        f"{extra}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _script(root: Path, name: str, body: str) -> Path:
    script = root / name
    script.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _wait_finished(launch: WorkerLaunch, handle: str) -> None:
    adapter = launch.adapter
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if adapter.observe(handle) == OBSERVE_FINISHED:
            return
        time.sleep(0.02)
    adapter.terminate(handle)
    raise AssertionError("worker did not finish")


class WorkerDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_discover_worker_reports_real_executable_version(self) -> None:
        executable = _fake_worker(self.root)
        capability = discover_worker("claude", executable=str(executable))
        self.assertEqual("claude", capability.kind)
        self.assertEqual("fake-worker 1.0", capability.version)
        self.assertEqual(executable.resolve(), Path(capability.executable))

    def test_discover_worker_rejects_unknown_kind(self) -> None:
        with self.assertRaisesRegex(WorkerError, "unsupported worker"):
            discover_worker("kimi")

    def test_discover_worker_rejects_missing_executable(self) -> None:
        with self.assertRaisesRegex(WorkerError, "failed to run"):
            discover_worker("codex", executable=str(self.root / "missing"))

    def test_discover_worker_rejects_unavailable_default_executable(self) -> None:
        previous = os.environ.get("PATH")
        os.environ["PATH"] = ""
        try:
            with self.assertRaisesRegex(WorkerError, "unavailable"):
                discover_worker("codex")
        finally:
            if previous is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = previous

    def test_discover_worker_rejects_empty_version(self) -> None:
        executable = _script(self.root, "empty-version", "raise SystemExit(0)")
        with self.assertRaisesRegex(WorkerError, "did not report"):
            discover_worker("claude", executable=str(executable))

    def test_run_wraps_nonzero_exit_and_timeout(self) -> None:
        with self.assertRaisesRegex(WorkerError, "exit 7"):
            _run([sys.executable, "-c", "raise SystemExit(7)"], cwd=self.root)
        with self.assertRaisesRegex(WorkerError, "failed to run"):
            _run([sys.executable, "-c", "import time; time.sleep(1)"], cwd=self.root, timeout=0.01)


class WorkerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        snapshot = TargetSnapshot(self.repo, "a" * 40, "")
        self.session = WorktreeSession(root, "T01", "attempt-1", "branch", root / "worktree", snapshot)

    def test_claude_command_is_noninteractive_and_edit_bounded(self) -> None:
        plan = _plan(self.repo, "claude", ["--model=sonnet"])
        capability = WorkerCapability("claude", "/bin/echo", "fake")
        command = build_worker_command(plan, capability, self.session)
        self.assertEqual("--print", command[1])
        self.assertIn("acceptEdits", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--model=sonnet", command)
        self.assertIn("README.md", command[-1])
        self.assertIn("result.txt", command[-1])

    def test_prompt_preserves_declared_reads_outside_the_repository(self) -> None:
        plan = _plan(self.repo)
        outside = self.repo.parent / "external.txt"
        plan["reads"] = [str(outside)]
        capability = WorkerCapability("claude", "/bin/echo", "fake")

        command = build_worker_command(plan, capability, self.session)

        self.assertIn(str(outside), command[-1])

    def test_codex_command_uses_workspace_write_and_explicit_worktree(self) -> None:
        plan = _plan(self.repo, "codex", ["--model", "gpt-5"])
        capability = WorkerCapability("codex", "/bin/echo", "fake")
        command = build_worker_command(plan, capability, self.session)
        self.assertEqual("exec", command[1])
        self.assertIn("workspace-write", command)
        self.assertIn(str(self.session.worktree), command)
        self.assertIn("--ephemeral", command)

    def test_command_rejects_isolation_overrides(self) -> None:
        cases = [("claude", "--add-dir=/tmp"), ("codex", "--sandbox=danger-full-access")]
        for kind, argument in cases:
            with self.subTest(kind=kind, argument=argument):
                plan = _plan(self.repo, kind, [argument])
                capability = WorkerCapability(kind, "/bin/echo", "fake")
                with self.assertRaisesRegex(WorkerError, "override isolation"):
                    build_worker_command(plan, capability, self.session)

    def test_command_rejects_missing_argument_values(self) -> None:
        for arguments in (["--model"], ["--model="], ["--model", "--effort", "high"]):
            with self.subTest(arguments=arguments):
                plan = _plan(self.repo, "claude", arguments)
                capability = WorkerCapability("claude", "/bin/echo", "fake")
                with self.assertRaisesRegex(WorkerError, "has no value"):
                    build_worker_command(plan, capability, self.session)

    def test_command_rejects_missing_or_outside_writes(self) -> None:
        capability = WorkerCapability("claude", "/bin/echo", "fake")
        no_writes = _plan(self.repo)
        no_writes["writes"] = []
        outside = _plan(self.repo)
        outside["writes"] = [str(self.repo.parent / "outside.txt")]
        for plan, message in ((no_writes, "no declared writes"), (outside, "outside")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(WorkerError, message):
                    build_worker_command(plan, capability, self.session)

    def test_command_rejects_capability_mismatch(self) -> None:
        capability = WorkerCapability("codex", "/bin/echo", "fake")
        with self.assertRaisesRegex(WorkerError, "does not match"):
            build_worker_command(_plan(self.repo, "claude"), capability, self.session)


class WorktreeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = _initialise_repository(self.root)
        self.project = _project(self.root)
        self.executable = _fake_worker(self.root)

    def test_claude_and_codex_worker_lifecycle_preserves_target(self) -> None:
        baseline = snapshot_target(self.repo)
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                attempt = f"attempt-{kind}"
                launch = prepare_worker(_plan(self.repo, kind), self.project, attempt, executable=str(self.executable))
                attempt_dir = self.project / "execution" / "attempts" / attempt
                attempt_dir.mkdir(parents=True)
                handle = launch.adapter.start(_plan(self.repo, kind), str(attempt_dir))
                self.assertIn(launch.adapter.observe(handle), {OBSERVE_RUNNING, OBSERVE_FINISHED})
                _wait_finished(launch, handle)
                attestation = launch.adapter.seal(handle)
                self.assertEqual(0, attestation["exit_code"])
                self.assertEqual(["result.txt"], inspect_worker_result(launch.session, _plan(self.repo, kind)))
                commit = commit_worker_result(launch.session, _plan(self.repo, kind))
                self.assertEqual(40, len(commit))
                self.assertFalse((self.repo / "result.txt").exists())
                assert_target_unchanged(baseline)
                cleanup_worktree(launch.session, commit)
                self.assertFalse(launch.session.worktree.exists())

    def test_undeclared_write_is_rejected_and_worktree_is_preserved(self) -> None:
        executable = _fake_worker(self.root, extra_write=True)
        plan = _plan(self.repo)
        launch = prepare_worker(plan, self.project, "attempt-extra", executable=str(executable))
        attempt_dir = self.project / "execution" / "attempts" / "attempt-extra"
        attempt_dir.mkdir(parents=True)
        handle = launch.adapter.start(plan, str(attempt_dir))
        _wait_finished(launch, handle)
        launch.adapter.seal(handle)
        with self.assertRaisesRegex(WorkerError, "undeclared paths: extra.txt"):
            inspect_worker_result(launch.session, plan)
        self.assertTrue(launch.session.worktree.exists())
        self.assertFalse((self.repo / "extra.txt").exists())

    def test_changed_paths_rejects_renames(self) -> None:
        plan = _plan(self.repo)
        session = create_worktree(plan, self.project, "attempt-rename")
        _git(session.worktree, "mv", "README.md", "RENAMED.md")
        with self.assertRaisesRegex(WorkerError, "renames"):
            changed_paths(session)

    def test_clean_target_is_required(self) -> None:
        (self.repo / "dirty.txt").write_text("user change\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkerError, "must be clean"):
            create_worktree(_plan(self.repo), self.project, "attempt-dirty")

    def test_existing_worktree_path_is_rejected(self) -> None:
        path = self.project / "execution" / "worktrees" / "attempt-existing"
        path.mkdir(parents=True)
        with self.assertRaisesRegex(WorkerError, "already exists"):
            create_worktree(_plan(self.repo), self.project, "attempt-existing")

    def test_target_change_blocks_acceptance(self) -> None:
        session = create_worktree(_plan(self.repo), self.project, "attempt-target-change")
        (self.repo / "user.txt").write_text("changed concurrently\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkerError, "changed while"):
            assert_target_unchanged(session.target)

    def test_cleanup_refuses_uncommitted_worktree(self) -> None:
        session = create_worktree(_plan(self.repo), self.project, "attempt-not-accepted")
        (session.worktree / "result.txt").write_text("pending\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkerError, "unaccepted or dirty"):
            cleanup_worktree(session, session.target.head)

    def test_result_requires_mutations_and_required_output(self) -> None:
        plan = _plan(self.repo)
        unchanged = create_worktree(plan, self.project, "attempt-unchanged")
        with self.assertRaisesRegex(WorkerError, "no repository mutations"):
            inspect_worker_result(unchanged, plan)

        missing_plan = _plan(self.repo)
        missing_plan["writes"].append(str(self.repo / "README.md"))
        missing = create_worktree(missing_plan, self.project, "attempt-missing")
        (missing.worktree / "README.md").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkerError, "required worker output"):
            inspect_worker_result(missing, missing_plan)

    def test_optional_output_does_not_need_to_exist(self) -> None:
        plan = _plan(self.repo)
        plan["outputs"].append({"root": "target", "path": "optional.txt", "required": False})
        plan["writes"].append(str(self.repo / "optional.txt"))
        session = create_worktree(plan, self.project, "attempt-optional")
        (session.worktree / "result.txt").write_text("done\n", encoding="utf-8")
        self.assertEqual(["result.txt"], inspect_worker_result(session, plan))

    def test_invalid_worker_arguments_discard_setup_worktree(self) -> None:
        plan = _plan(self.repo, arguments=["--add-dir", "/tmp"])
        with self.assertRaisesRegex(WorkerError, "override isolation"):
            prepare_worker(plan, self.project, "attempt-discard", executable=str(self.executable))
        worktree = self.project / "execution" / "worktrees" / "attempt-discard"
        self.assertFalse(worktree.exists())
        branches = _git(self.repo, "branch", "--list", "research/*/T01/attempt-discard")
        self.assertEqual("", branches)

    def test_nested_worker_is_rejected_before_worktree_creation(self) -> None:
        previous = os.environ.get(NESTING_ENV)
        os.environ[NESTING_ENV] = "1"
        try:
            with self.assertRaisesRegex(WorkerError, "nested"):
                prepare_worker(_plan(self.repo), self.project, "attempt-nested", executable=str(self.executable))
        finally:
            if previous is None:
                os.environ.pop(NESTING_ENV, None)
            else:
                os.environ[NESTING_ENV] = previous


if __name__ == "__main__":
    unittest.main()
