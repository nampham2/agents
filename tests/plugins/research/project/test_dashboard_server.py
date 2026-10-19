"""The dashboard server and command line: three read-only routes on the loopback address."""

from __future__ import annotations

import errno
import http.client
import json
import re
import runpy
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import workspace_dashboard as dash
import workspace_lib as lib

from tests.conftest import SKILL_SCRIPTS
from tests.plugins.research.project.test_dashboard_model import ID, OTHER, bump, mentions, transcript, write_project

PAGE_TEXT = "<!doctype html><title>Research projects</title>"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    write_project(root, ID, title="First title")
    write_project(root, OTHER, status="DONE")
    return root


@pytest.fixture
def page(tmp_path: Path) -> Path:
    path = tmp_path / "dashboard.html"
    path.write_text(PAGE_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def dashboard(workspace: Path, page: Path, tmp_path: Path) -> dash.Dashboard:
    return dash.Dashboard(workspace, tmp_path / "claude", page)


@contextmanager
def running(board: dash.Dashboard) -> Iterator[dash.DashboardServer]:
    server = dash.make_server(board, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(
    server: dash.DashboardServer, path: str, *, method: str = "GET", host: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(dash.HOST, server.server_port, timeout=10)
    try:
        connection.request(
            method, path, body=b"x" if method == "POST" else None, headers={"Host": host} if host else {}
        )
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def get_json(server: dash.DashboardServer, path: str) -> dict[str, Any]:
    status, _, body = fetch(server, path)
    assert status == 200
    return json.loads(body)


# --- routes --------------------------------------------------------------------------------------


def test_the_page_and_both_api_routes(dashboard: dash.Dashboard) -> None:
    with running(dashboard) as server:
        status, headers, body = fetch(server, "/")
        assert (status, body.decode()) == (200, PAGE_TEXT)
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
        version = get_json(server, "/api/version")
        assert re.fullmatch(r"[0-9a-f]{64}", version["fingerprint"])
        projects = get_json(server, "/api/projects")
        assert projects["fingerprint"] == version["fingerprint"]
        assert projects["root"] == str(dashboard.root)
        assert [row["id"] for row in projects["projects"]] == [ID, OTHER]
        assert projects["projects"][0]["title"] == "First title"
        assert datetime.fromisoformat(projects["generated"]).tzinfo is not None
        assert fetch(server, "/api/projects")[1]["content-type"] == "application/json; charset=utf-8"


def test_a_query_string_does_not_change_the_route(dashboard: dash.Dashboard) -> None:
    with running(dashboard) as server:
        assert fetch(server, "/?x=1")[2].decode() == PAGE_TEXT
        assert fetch(server, "/api/version?ignored=1")[0] == 200


@pytest.mark.parametrize("path", ["/missing", "/api", "/api/projects/", "/../etc/passwd", "/index.html"])
def test_any_other_path_is_not_found(dashboard: dash.Dashboard, path: str) -> None:
    with running(dashboard) as server:
        assert fetch(server, path)[:1] == (404,)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_a_request_that_could_write_is_refused_and_writes_nothing(
    dashboard: dash.Dashboard, workspace: Path, method: str
) -> None:
    before = sorted(str(path) for path in workspace.rglob("*"))
    with running(dashboard) as server:
        assert fetch(server, "/api/projects", method=method)[0] == 501
    assert sorted(str(path) for path in workspace.rglob("*")) == before


def test_a_missing_page_is_a_server_error_that_names_the_file(dashboard: dash.Dashboard) -> None:
    dashboard.page.unlink()
    with running(dashboard) as server:
        status, _, body = fetch(server, "/")
        assert status == 500 and b"dashboard.html is missing" in body


# --- who may ask ---------------------------------------------------------------------------------


def test_only_our_own_address_is_answered(dashboard: dash.Dashboard) -> None:
    with running(dashboard) as server:
        port = server.server_port
        assert fetch(server, "/api/projects", host=f"localhost:{port}")[0] == 200
        assert fetch(server, "/api/projects", host=f"127.0.0.1:{port}")[0] == 200
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1", f"127.0.0.1:{port + 1}", "[::1]"):
            status, _, body = fetch(server, "/api/projects", host=host)
            assert (status, body) == (403, b"Forbidden host."), host


def test_a_request_without_a_host_header_is_refused(dashboard: dash.Dashboard) -> None:
    with running(dashboard) as server, socket.create_connection((dash.HOST, server.server_port), timeout=10) as client:
        client.sendall(b"GET /api/projects HTTP/1.0\r\n\r\n")
        assert client.recv(65536).startswith(b"HTTP/1.0 403")


def test_the_server_binds_the_loopback_address_only(dashboard: dash.Dashboard) -> None:
    with running(dashboard) as server:
        assert server.server_address[0] == "127.0.0.1"


# --- errors behind the routes --------------------------------------------------------------------


def test_a_root_that_disappears_is_a_server_error_on_both_api_routes(dashboard: dash.Dashboard) -> None:
    with running(dashboard) as server:
        assert get_json(server, "/api/projects")["projects"]
        shutil.rmtree(dashboard.root)
        for path in ("/api/projects", "/api/version"):
            status, _, body = fetch(server, path)
            assert status == 500 and b"cannot list" in body


# --- change detection ----------------------------------------------------------------------------


def test_a_changed_project_file_moves_the_version_and_the_model(dashboard: dash.Dashboard, workspace: Path) -> None:
    with running(dashboard) as server:
        before = get_json(server, "/api/version")["fingerprint"]
        assert get_json(server, "/api/projects")["projects"][0]["title"] == "First title"
        write_project(workspace, ID, title="Second title")
        bump(workspace / ID / "project.json")
        after = get_json(server, "/api/version")["fingerprint"]
        assert after != before
        changed = get_json(server, "/api/projects")
        assert changed["projects"][0]["title"] == "Second title" and changed["fingerprint"] == after


def test_the_model_is_built_once_until_something_changes(
    dashboard: dash.Dashboard, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[Path] = []
    original = dash.build_model

    def counting(root: Path) -> dict[str, Any]:
        builds.append(root)
        return original(root)

    monkeypatch.setattr(dash, "build_model", counting)
    dashboard.payload()
    dashboard.payload()
    assert len(builds) == 1
    bump(workspace / ID / "project.json")
    dashboard.payload()
    assert len(builds) == 2


def test_concurrent_requests_share_one_build(dashboard: dash.Dashboard, monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []
    original = dash.build_model

    def slow(root: Path) -> dict[str, Any]:
        builds.append(1)
        time.sleep(0.1)
        return original(root)

    monkeypatch.setattr(dash, "build_model", slow)
    results: list[dict[str, Any]] = []
    threads = [threading.Thread(target=lambda: results.append(dashboard.payload())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(builds) == 1
    assert len(results) == 8 and {result["fingerprint"] for result in results} == {results[0]["fingerprint"]}


def test_sessions_refresh_without_a_project_change(dashboard: dash.Dashboard, tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    assert dashboard.payload()["projects"][0]["sessions"] == []
    transcript(claude, "-git", "s1", mentions(ID, 5))
    first = dashboard.payload()
    assert [session["id"] for session in first["projects"][0]["sessions"]] == ["s1"]
    assert first["fingerprint"] == dashboard.version()["fingerprint"]
    transcript(claude, "-git", "s2", mentions(ID, 5), when=int(time.time()) + 100)
    assert [session["id"] for session in dashboard.payload()["projects"][0]["sessions"]] == ["s2", "s1"]


# --- command line --------------------------------------------------------------------------------


class FakeServer:
    server_port = 4321

    def __init__(self, *, interrupt: bool = True) -> None:
        self.interrupt = interrupt
        self.closed = False

    def serve_forever(self) -> None:
        if self.interrupt:
            raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    made: dict[str, Any] = {}

    def make(board: dash.Dashboard, port: int) -> FakeServer:
        made.update(dashboard=board, port=port, server=FakeServer())
        return made["server"]

    monkeypatch.setattr(dash, "make_server", make)
    monkeypatch.delenv(lib.WORKSPACE_ROOT_ENV_VAR, raising=False)
    return made


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(dash.webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


def test_the_url_is_printed_and_ctrl_c_exits_zero(
    fake_server: dict[str, Any], opened: list[str], workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dash.main([str(workspace), "--no-browser"]) == 0
    assert capsys.readouterr().out == "Dashboard: http://127.0.0.1:4321\n"
    assert opened == []
    assert fake_server["server"].closed is True
    assert fake_server["port"] == dash.DEFAULT_PORT
    assert fake_server["dashboard"].root == workspace.resolve()


def test_the_browser_opens_unless_told_not_to(fake_server: dict[str, Any], opened: list[str], workspace: Path) -> None:
    assert dash.main([str(workspace), "--port", "0"]) == 0
    assert opened == ["http://127.0.0.1:4321"]
    assert fake_server["port"] == 0


def test_the_server_ending_by_itself_is_a_clean_exit(
    fake_server: dict[str, Any], opened: list[str], workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeServer(interrupt=False)
    monkeypatch.setattr(dash, "make_server", lambda board, port: server)
    assert dash.main([str(workspace), "--no-browser"]) == 0
    assert server.closed is True


def test_the_claude_directory_is_passed_through(fake_server: dict[str, Any], workspace: Path, tmp_path: Path) -> None:
    dash.main([str(workspace), "--no-browser", "--claude-dir", str(tmp_path / "elsewhere")])
    assert fake_server["dashboard"]._index.claude_dir == tmp_path / "elsewhere"


def test_the_pi_directory_is_passed_through(fake_server: dict[str, Any], workspace: Path, tmp_path: Path) -> None:
    dash.main([str(workspace), "--no-browser", "--pi-dir", str(tmp_path / "elsewhere")])
    assert fake_server["dashboard"]._pi_index.pi_dir == tmp_path / "elsewhere"


def test_the_root_comes_from_the_environment(
    fake_server: dict[str, Any], workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(lib.WORKSPACE_ROOT_ENV_VAR, str(workspace))
    assert dash.main(["--no-browser"]) == 0
    assert fake_server["dashboard"].root == workspace.resolve()


def test_the_one_established_root_is_found_when_nothing_names_it(
    fake_server: dict[str, Any], workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dash, "find_workspace_roots", lambda: [workspace])
    assert dash.main(["--no-browser"]) == 0
    assert fake_server["dashboard"].root == workspace.resolve()


def test_no_root_exits_two_and_says_what_to_do(
    fake_server: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dash, "find_workspace_roots", lambda: [])
    assert dash.main(["--no-browser"]) == 2
    assert capsys.readouterr().err == "No workspace root. Pass ROOT or set RESEARCH_WORKSPACE.\n"
    assert "dashboard" not in fake_server


def test_several_roots_exit_two_and_list_them(
    fake_server: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = [tmp_path / "a", tmp_path / "b"]
    monkeypatch.setattr(dash, "find_workspace_roots", lambda: roots)
    assert dash.main(["--no-browser"]) == 2
    error = capsys.readouterr().err
    assert str(roots[0]) in error and str(roots[1]) in error and "Pass ROOT" in error


def test_a_root_that_is_not_a_directory_exits_two(
    fake_server: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dash.main([str(tmp_path / "missing"), "--no-browser"]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_a_port_in_use_exits_two_and_names_the_fix(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as blocker:
        blocker.bind((dash.HOST, 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        assert dash.main([str(workspace), "--no-browser", "--port", str(port)]) == 2
    assert capsys.readouterr().err == f"Port {port} in use. Use --port 0 for a free port.\n"


def test_any_other_bind_failure_exits_two_with_the_reason(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(board: dash.Dashboard, port: int) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(dash, "make_server", refuse)
    assert dash.main([str(workspace), "--no-browser", "--port", "80"]) == 2
    assert "Cannot listen on port 80" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["70000", "-1", "abc", ""])
def test_a_bad_port_is_refused_before_anything_starts(workspace: Path, port: str) -> None:
    with pytest.raises(SystemExit) as stop:
        dash.main([str(workspace), "--port", port])
    assert stop.value.code == 2


def test_running_the_file_as_a_script_reaches_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["research-dashboard", "--help"])
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(SKILL_SCRIPTS / "workspace_dashboard.py"), run_name="__main__")
    assert stop.value.code == 0


# --- the shipped page ----------------------------------------------------------------------------

PAGE_FILE = SKILL_SCRIPTS / "dashboard.html"


def test_the_shipped_page_is_served_from_its_default_location(workspace: Path, tmp_path: Path) -> None:
    with running(dash.Dashboard(workspace, tmp_path / "claude")) as server:
        status, headers, body = fetch(server, "/")
    assert status == 200 and headers["content-type"] == "text/html; charset=utf-8"
    assert body == PAGE_FILE.read_bytes()
    assert b"<title>Research projects</title>" in body


def test_the_page_script_only_reaches_for_elements_the_markup_has() -> None:
    markup, script = PAGE_FILE.read_text(encoding="utf-8").split("<script>", 1)
    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script)) | {"from", "repo", "q"}
    present = set(re.findall(r'\bid="([^"]+)"', markup))
    assert wanted <= present, sorted(wanted - present)


def test_the_page_reads_only_the_two_api_routes() -> None:
    script = PAGE_FILE.read_text(encoding="utf-8").split("<script>", 1)[1]
    assert set(re.findall(r'getJson\("(/[^"]+)"\)', script)) == {"/api/version", "/api/projects"}
    assert len(re.findall(r"\bfetch\(", script)) == 1 and "fetch(path," in script


def test_the_page_has_no_placeholder_no_local_file_link_and_no_inline_handler() -> None:
    text = PAGE_FILE.read_text(encoding="utf-8")
    assert "/*DATA*/" not in text
    assert "file://" not in text
    assert not re.search(r"\bon[a-z]+\s*=\s*[\"']", text)


def test_only_a_web_address_can_become_a_link_in_the_page() -> None:
    script = PAGE_FILE.read_text(encoding="utf-8").split("<script>", 1)[1]
    assert r'const safeUrl = u => /^https?:\/\//i.test(String(u || "")) ? String(u) : "";' in script
    # Every address the script writes into markup is the sanitised one or an icon in the page itself.
    assert sorted(re.findall(r'href="([^"]*)"', script)) == sorted(["${esc(safeUrl(u))}", "#i-${name}"])
