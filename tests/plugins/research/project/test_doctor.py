"""`research-project doctor`: rules R1-R6, each shown failing first, on real temporary git repos."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import manage_workspace
import pytest
import workspace_doctor
from workspace_doctor import Finding, attributes_line, ignore_lines, run_doctor
from workspace_lib import WorkspaceError

from tests.conftest import MANAGER

MIB = 1024 * 1024
PROJECT = "2026-11-13-001"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env={**os.environ, **GIT_ENV}
    )
    return result.stdout


def write(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def fake_lfs(directory: Path, *, works: bool = True) -> Path:
    """A `git-lfs` executable that shadows only that tool; `install --local` writes the filter config."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "git-lfs"
    if works:
        body = (
            "#!/bin/sh\n"
            'if [ "$1" = install ]; then\n'
            "  git config --local filter.lfs.clean 'git-lfs clean -- %f'\n"
            "  git config --local filter.lfs.smudge 'git-lfs smudge -- %f'\n"
            "  git config --local filter.lfs.process 'git-lfs filter-process'\n"
            "fi\n"
            "exit 0\n"
        )
    else:
        body = "#!/bin/sh\nexit 1\n"
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return directory


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", f"{fake_lfs(tmp_path / 'lfs-bin')}{os.pathsep}{os.environ['PATH']}")


def configure_lfs(repo: Path) -> None:
    for key in ("clean", "smudge", "process"):
        git(repo, "config", f"filter.lfs.{key}", "git-lfs")


def lay_out(repo: Path, workspace: Path, prefix: str) -> None:
    """A complete layout: marker staged, ignore and attribute lines present, LFS configured."""
    (workspace / "workspace-root-placeholder").unlink(missing_ok=True)
    (workspace / PROJECT).mkdir(parents=True, exist_ok=True)
    (workspace / ".workspace-root").write_text('{"layout":1}', encoding="utf-8")
    (repo / ".gitignore").write_text("\n".join(ignore_lines(prefix)) + "\n", encoding="utf-8")
    (repo / ".gitattributes").write_text(attributes_line(prefix) + "\n", encoding="utf-8")
    configure_lfs(repo)
    git(repo, "add", ".gitignore", ".gitattributes", str((workspace / ".workspace-root")))


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    top = (tmp_path / "repo").resolve()
    top.mkdir()
    git(top, "init", "-q")
    workspace = top / "workspace"
    workspace.mkdir()
    lay_out(top, workspace, "workspace")
    return top, workspace


def rules(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(item.rule, item.path) for item in findings]


def test_a_clean_layout_has_no_findings(repo: tuple[Path, Path]) -> None:
    assert run_doctor(repo[1]) == []


def test_r1_a_staged_non_lfs_file_over_4_mib_is_an_error(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    assert run_doctor(workspace) == []
    write(workspace / PROJECT / "big.bin", 5 * MIB)
    git(top, "add", "-f", "workspace")
    [finding] = run_doctor(workspace)
    assert (finding.severity, finding.rule, finding.path) == ("error", "R1", f"{PROJECT}/big.bin")
    assert "artifacts/large" in finding.fix


def test_r1_an_untracked_file_over_4_mib_is_an_error_and_an_ignored_one_is_not(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    write(workspace / PROJECT / "big.bin", 5 * MIB)
    assert rules(run_doctor(workspace)) == [("R1", f"{PROJECT}/big.bin")]
    (top / ".gitignore").write_text(
        "\n".join(ignore_lines("workspace")) + f"\n/workspace/{PROJECT}/big.bin\n", encoding="utf-8"
    )
    assert run_doctor(workspace) == []


def test_r1_ignores_a_blob_already_in_head_and_catches_one_that_grows(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    big = write(workspace / PROJECT / "big.bin", 3 * MIB)
    git(top, "add", "workspace")
    git(top, "commit", "-qm", "base")
    assert run_doctor(workspace) == []
    big.write_bytes(b"y" * (5 * MIB))
    git(top, "add", "workspace")
    assert rules(run_doctor(workspace)) == [("R1", f"{PROJECT}/big.bin")]


def test_r1_ignores_a_deleted_file_and_a_gitlink(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    write(workspace / PROJECT / "a.txt")
    git(top, "add", "workspace")
    git(top, "commit", "-qm", "base")
    git(top, "rm", "-q", "workspace/" + PROJECT + "/a.txt")
    sub = workspace / "sub"
    sub.mkdir()
    git(sub, "init", "-q")
    git(sub, "commit", "-q", "--allow-empty", "-m", "c")
    git(top, "add", "workspace/sub")
    assert run_doctor(workspace) == []


def test_r2_a_new_file_of_1_to_4_mib_outside_large_is_a_warning(repo: tuple[Path, Path]) -> None:
    workspace = repo[1]
    write(workspace / PROJECT / "mid.bin", 2 * MIB)
    write(workspace / PROJECT / "artifacts" / "large" / "kept.bin", 2 * MIB)
    write(workspace / PROJECT / "small.bin", MIB - 1)
    [finding] = run_doctor(workspace)
    assert (finding.severity, finding.rule, finding.path) == ("warning", "R2", f"{PROJECT}/mid.bin")


def test_r3_missing_filter_config_is_an_error_naming_the_fix(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    git(top, "config", "--unset", "filter.lfs.process")
    [finding] = run_doctor(workspace)
    assert (finding.severity, finding.rule) == ("error", "R3")
    assert "git lfs install --local" in finding.fix and "filter.lfs.process" in finding.message


def test_r3_absent_git_lfs_is_an_error(
    repo: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{fake_lfs(tmp_path / 'broken', works=False)}{os.pathsep}{os.environ['PATH']}")
    [finding] = run_doctor(repo[1])
    assert finding.rule == "R3" and "git lfs version" in finding.message


def test_r4_leftover_temporaries_warn_at_root_and_in_projects(repo: tuple[Path, Path]) -> None:
    _, workspace = repo
    write(workspace / ".INDEX.md.abc123.tmp")
    write(workspace / PROJECT / ".project.json.zzz.tmp")
    write(workspace / "notes" / ".x.y.tmp")
    write(workspace / ".keep")
    assert rules(run_doctor(workspace)) == [("R4", ".INDEX.md.abc123.tmp"), ("R4", f"{PROJECT}/.project.json.zzz.tmp")]


def test_r5_reports_each_missing_part_of_the_layout(tmp_path: Path) -> None:
    top = (tmp_path / "bare").resolve()
    workspace = top / "workspace"
    (workspace / PROJECT).mkdir(parents=True)
    git(top, "init", "-q")
    configure_lfs(top)
    found = run_doctor(workspace)
    assert {(item.rule, item.path) for item in found} == {
        ("R5", ".workspace-root"),
        *{("R5", name) for name in workspace_doctor.VIEW_NAMES},
        ("R5", ".gitignore"),
        ("R5", ".gitattributes"),
    }
    assert all(item.severity == "error" and "migrate --layout" in item.fix for item in found)
    assert len(found) == 7


def test_r5_a_tracked_view_is_an_error(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    (workspace / "INDEX.md").write_text("x\n", encoding="utf-8")
    git(top, "add", "-f", "workspace/INDEX.md")
    [finding] = run_doctor(workspace)
    assert (finding.rule, finding.path, finding.message) == ("R5", "INDEX.md", "derived view is tracked by git")


def test_r5_a_workspace_at_the_repository_root_anchors_without_a_prefix(tmp_path: Path) -> None:
    top = (tmp_path / "flat").resolve()
    top.mkdir()
    git(top, "init", "-q")
    lay_out(top, top, "")
    assert (top / ".gitignore").read_text(encoding="utf-8").splitlines()[0] == "/INDEX.md"
    assert attributes_line("") == "*/artifacts/large/** filter=lfs diff=lfs merge=lfs -text"
    assert run_doctor(top) == []


def test_r6_an_ignored_file_over_1_mib_needs_a_manifest_entry(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    (top / ".gitignore").write_text(
        "\n".join(ignore_lines("workspace")) + f"\n/workspace/{PROJECT}/artifacts/cache/\n", encoding="utf-8"
    )
    write(workspace / PROJECT / "artifacts" / "cache" / "deep" / "a.bin", 2 * MIB)
    write(workspace / PROJECT / "artifacts" / "cache" / "b.bin", 2 * MIB)
    write(workspace / PROJECT / "artifacts" / "cache" / "tiny.bin", 10)
    found = run_doctor(workspace)
    assert [(i.severity, i.rule) for i in found] == [("warning", "R6")] * 2
    assert {i.path for i in found} == {
        f"{PROJECT}/artifacts/cache/deep/a.bin",
        f"{PROJECT}/artifacts/cache/b.bin",
    }
    manifest = workspace / PROJECT / "artifacts" / "MANIFEST.json"
    manifest.write_text(
        '{"files": [{"path": "cache/deep/a.bin", "size": 1}, {"path": "cache/b.bin"}, "junk"]}', encoding="utf-8"
    )
    assert [i.path for i in run_doctor(workspace)] == [f"{PROJECT}/artifacts/cache/b.bin"]
    manifest.write_text(
        '[{"path": "cache/b.bin", "size": 2}, {"path": "cache/deep/a.bin", "size": 2}]', encoding="utf-8"
    )
    assert run_doctor(workspace) == []
    manifest.write_text('{"files": 3}', encoding="utf-8")
    assert len(run_doctor(workspace)) == 2
    manifest.write_text("not json", encoding="utf-8")
    assert len(run_doctor(workspace)) == 2


def test_r6_skips_projects_without_artifacts_and_files_that_vanish(repo: tuple[Path, Path]) -> None:
    _, workspace = repo
    (workspace / "2026-11-13-002").mkdir()
    with patch.object(workspace_doctor, "_file_size", return_value=None):
        assert run_doctor(workspace) == []


def test_a_non_git_directory_skips_r1_r3_and_r5_but_reports_r4(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    write(workspace / PROJECT / "big.bin", 5 * MIB)
    write(workspace / ".INDEX.md.q.tmp")
    assert rules(run_doctor(workspace)) == [("R4", ".INDEX.md.q.tmp")]


def test_an_unreadable_or_missing_directory_is_a_workspace_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="does not exist"):
        run_doctor(tmp_path / "missing")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("permissions are not enforced")
        with pytest.raises(WorkspaceError):
            run_doctor(locked)
    finally:
        locked.chmod(0o755)


def test_r4_and_r6_wrap_directory_read_failures(repo: tuple[Path, Path]) -> None:
    _, workspace = repo
    with patch.object(Path, "iterdir", side_effect=OSError("boom")):
        with pytest.raises(WorkspaceError, match="cannot read"):
            workspace_doctor._temporary_findings(workspace)
        with pytest.raises(WorkspaceError, match="cannot read"):
            workspace_doctor._manifest_findings(workspace)


def test_missing_git_is_a_workspace_error(repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(repo[0]))
    with pytest.raises(WorkspaceError, match="cannot run git"):
        run_doctor(repo[1])


def test_a_failing_git_query_is_a_workspace_error(repo: tuple[Path, Path]) -> None:
    with pytest.raises(WorkspaceError, match="failed in"):
        workspace_doctor.run_git(repo[1], "no-such-subcommand")


def test_unreadable_ignore_files_count_as_missing_lines(repo: tuple[Path, Path]) -> None:
    top, workspace = repo
    (top / ".gitignore").write_bytes(b"\xff\xfe")
    found = run_doctor(workspace)
    assert ("R5", ".gitignore") in rules(found)


def test_file_size_of_a_vanished_file_is_none(tmp_path: Path) -> None:
    assert workspace_doctor._file_size(tmp_path, "gone") is None


def test_finding_line_format() -> None:
    finding = Finding("error", "R1", "a/b", "too big", "move it")
    assert finding.line() == "error R1 a/b: too big; fix: move it"


def manage(*arguments: object) -> int:
    with patch.object(sys, "argv", ["research-project", *[str(item) for item in arguments]]):
        return manage_workspace.main()


def test_cli_exit_codes_and_line_format(repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    workspace = repo[1]
    assert manage("doctor", workspace) == 0
    assert capsys.readouterr().out == ""
    write(workspace / PROJECT / "mid.bin", 2 * MIB)
    assert manage("doctor", workspace) == 0
    assert capsys.readouterr().out.startswith(f"warning R2 {PROJECT}/mid.bin: ")
    write(workspace / PROJECT / "big.bin", 5 * MIB)
    assert manage("doctor", workspace) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"error R1 {PROJECT}/big.bin: ") and "; fix: " in out[0]
    assert out[1].startswith("warning R2")


def test_cli_exit_2_when_unreadable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert manage("doctor", tmp_path / "missing") == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_uses_the_environment_root(repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_WORKSPACE", str(repo[1]))
    assert manage("doctor") == 0


HOOK = '#!/bin/sh\nexec research-project doctor "$(git rev-parse --show-toplevel)/workspace"\n'


def install_hook(top: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "research-project"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{MANAGER}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    hook = top / ".git" / "hooks" / "pre-commit"
    hook.write_text(HOOK, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def test_the_pre_commit_snippet_rejects_a_staged_5_mib_file(
    repo: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top, workspace = repo
    install_hook(top, tmp_path, monkeypatch)
    write(workspace / PROJECT / "notes.md", 100)
    git(top, "add", "-f", "workspace")
    git(top, "commit", "-qm", "small is fine")
    write(workspace / PROJECT / "big.bin", 5 * MIB)
    git(top, "add", "-f", "workspace")
    rejected = subprocess.run(
        ["git", "-C", str(top), "commit", "-qm", "big"], capture_output=True, text=True, env={**os.environ, **GIT_ENV}
    )
    assert rejected.returncode != 0
    assert f"error R1 {PROJECT}/big.bin" in rejected.stderr
    assert git(top, "log", "--oneline").count("\n") == 1
    git(top, "commit", "-qm", "bypass", "--no-verify")
    assert git(top, "log", "--oneline").count("\n") == 2
