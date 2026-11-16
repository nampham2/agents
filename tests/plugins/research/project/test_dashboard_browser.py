"""The dashboard's tag filter and wide layout, checked in a real headless Chrome.

Ported from the hand-run check of project 2026-11-16-001 (its `artifacts/dashboard-check/check.py`) by
project 2026-11-16-004. The test builds a workspace, takes the payload the page would fetch, loads a copy of
the real `dashboard.html` with that payload served by a `fetch` shim, clicks through the tag paths and
measures the layout at five widths.

With no browser the test skips, unless `REQUIRE_BROWSER=1`, which CI sets: then it fails and names every
place it looked. A browser that is found but exits non-zero, runs past its timeout or reports nothing fails;
it never skips. Chrome runs in its own process group, killed on every path, and writes to files, never to a
pipe, so neither a large DOM dump nor a helper process can hang the run.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from workspace_dashboard import Dashboard
from workspace_lib import allocate_project

REPOSITORY = Path(__file__).resolve().parents[4]
PAGE = REPOSITORY / "plugins" / "research" / "skills" / "project" / "scripts" / "dashboard.html"
MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">',
    re.compile(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com/[^"]*">'),
)
FIXTURE = [
    ("Tag filter design", "a", ["dashboard", "design", "tooling"]),
    ("Feed regression", "b", ["feed-generation", "airflow"]),
    ("Cost audit", "b", ["bigquery", "cost-reduction"]),
    ("Legacy untagged job", "a", None),
    ("Memory compaction", "a", ["research-plugin", "memory"]),
    ("Old research", "a", ["research-plugin", "tooling"]),
    ("Another legacy", "b", None),
]
CHECK_NAMES = [
    "starts with all 7 projects",
    "one tag narrows to 2",
    "two tags match all: 1 project",
    "counts follow the chosen tags",
    "a removable chip clears only its tag",
    "untagged shows the 2 untagged projects",
    "a tag replaces untagged",
    "clear filters restores all",
    "a card's tag filters and does not open the drawer",
    "a tagged project's drawer shows its tags",
    "a tag chosen in the drawer filters and closes it",
    "an untagged project's drawer says Untagged",
]
# width: (wrap width, card columns or None when not checked, sticky rail)
LAYOUT = {
    1440: (1240, 3, False),
    1679: (1240, 3, False),
    1680: (1680, None, True),
    2560: (1920, 4, True),
    3440: (1920, 4, True),
}
MATCH_ALL = "[...state.tags].every(t => tagsOf(p).includes(t))"
MATCH_ANY = "(!state.tags.size || [...state.tags].some(t => tagsOf(p).includes(t)))"

CLICKS = r"""<script>
setTimeout(() => {
  const out = [], q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  const cards = () => qa(".card").length;
  const pressed = () => qa("#tag-list .tagchip")
    .filter(b => b.getAttribute("aria-pressed") === "true").map(b => b.dataset.tag || "untagged");
  const counts = () => Object.fromEntries(
    qa("#tag-list .tagchip").map(b => [b.dataset.tag || "untagged", b.querySelector(".n")?.textContent]));
  const applied = () => q("#applied").textContent.replace(/\s+/g, " ").trim();
  const check = (name, ok, detail) => out.push({ name, ok: Boolean(ok), detail: String(detail) });
  check("starts with all 7 projects", cards() === 7, cards());
  q('#tag-list [data-tag="tooling"]').click();
  check("one tag narrows to 2", cards() === 2, applied());
  q('#tag-list [data-tag="research-plugin"]').click();
  check("two tags match all: 1 project", cards() === 1, applied());
  check("counts follow the chosen tags",
    counts().tooling === "1" && counts().airflow === "0", JSON.stringify(counts()));
  q('#applied [data-remove="tag:tooling"]').click();
  check("a removable chip clears only its tag",
    cards() === 2 && pressed().join() === "research-plugin", applied());
  q("#tag-list [data-untagged]").click();
  check("untagged shows the 2 untagged projects", cards() === 2 && pressed().join() === "untagged", applied());
  q('#tag-list [data-tag="memory"]').click();
  check("a tag replaces untagged", cards() === 1 && pressed().join() === "memory", applied());
  document.getElementById("clear").click();
  check("clear filters restores all", cards() === 7 && applied() === "", applied());
  q(".card .tagchip").click();
  check("a card's tag filters and does not open the drawer",
    !document.body.classList.contains("has-drawer") && pressed().length === 1, applied());
  document.getElementById("clear").click();
  qa(".card").find(c => c.querySelector(".tagchip")).click();
  check("a tagged project's drawer shows its tags",
    document.body.classList.contains("has-drawer") && qa("#drawer .tagchip").length >= 2,
    qa("#drawer .tagchip").length);
  q("#drawer .tagchip").click();
  check("a tag chosen in the drawer filters and closes it",
    !document.body.classList.contains("has-drawer") && pressed().length === 1, applied());
  document.getElementById("clear").click();
  qa(".card").find(c => !c.querySelector(".tagchip")).click();
  check("an untagged project's drawer says Untagged", Boolean(q("#drawer-title .untag")), "");
  document.body.insertAdjacentHTML("beforeend", '<pre id="__out">' + JSON.stringify(out) + "</pre>");
}, 800);
</script>"""

MEASURE = r"""<script>
setTimeout(() => {
  const q = s => document.querySelector(s);
  const wrap = q(".wrap").getBoundingClientRect();
  const r = {
    vw: innerWidth, wrapWidth: Math.round(wrap.width),
    columns: getComputedStyle(q(".cards")).gridTemplateColumns.split(" ").length,
    rail: getComputedStyle(q(".top")).position === "sticky",
    scrolls: document.documentElement.scrollWidth > innerWidth,
  };
  document.body.insertAdjacentHTML("beforeend", '<pre id="__out">' + JSON.stringify(r) + "</pre>");
}, 800);
</script>"""


# --- the harness ------------------------------------------------------------------------------------


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_browser(
    env: Mapping[str, str], path_lookup: Callable[[str], str | None], mac_path: Path
) -> tuple[str | None, list[str]]:
    """A browser to run and every source searched. An explicit `DASHBOARD_BROWSER` that does not work fails."""
    searched: list[str] = []
    chosen = env.get("DASHBOARD_BROWSER", "")
    if chosen:
        searched.append(f"DASHBOARD_BROWSER={chosen}")
        resolved = chosen if "/" in chosen else path_lookup(chosen)
        if resolved is None or not _executable(Path(resolved)):
            pytest.fail(f"DASHBOARD_BROWSER={chosen!r} is not an executable browser")
        return resolved, searched
    for name in PATH_NAMES:
        searched.append(f"PATH:{name}")
        found = path_lookup(name)
        if found:
            return found, searched
    searched.append(str(mac_path))
    if _executable(mac_path):
        return str(mac_path), searched
    return None, searched


def browser_or_skip(env: Mapping[str, str], path_lookup: Callable[[str], str | None], mac_path: Path) -> str:
    browser, searched = find_browser(env, path_lookup, mac_path)
    if browser is not None:
        return browser
    message = "no browser found; searched " + ", ".join(searched)
    if env.get("REQUIRE_BROWSER") == "1":
        pytest.fail(message + " (REQUIRE_BROWSER=1)")
    pytest.skip(message)


def _replace_once(source: str, old: "str | re.Pattern[str]", new: str) -> str:
    if isinstance(old, str):
        assert source.count(old) == 1, f"expected exactly one {old!r} in the page"
        return source.replace(old, new, 1)
    replaced, count = old.subn(new, source)
    assert count == 1, f"expected exactly one match of {old.pattern!r} in the page"
    return replaced


def build_page(source: str, payload: dict[str, Any], script: str) -> str:
    """The page with no web fonts, a `fetch` shim serving `payload`, and `script` run after load."""
    for link in FONT_LINKS:
        source = _replace_once(source, link, "")
    shim = (
        f"<script>window.__API = {json.dumps(payload)};\n"
        "window.fetch = async (path) => ({ ok: true, json: async () => String(path).includes(\"version\")"
        " ? { fingerprint: window.__API.fingerprint } : window.__API });</script>"
    )
    source = _replace_once(source, "</head>", shim + "</head>")
    return _replace_once(source, "</body>", script + "</body>")


def run_page(browser: str, page: Path, width: int, timeout: float = 60) -> Any:
    """Load `page` in headless Chrome at `width` and return the JSON of its last `__out` block."""
    work = page.parent
    command = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--no-default-browser-check", "--disable-breakpad", "--virtual-time-budget=6000",
        f"--window-size={width},900", f"--user-data-dir={work / f'profile-{width}'}", "--dump-dom",
    ]
    if sys.platform.startswith("linux"):
        command.append("--no-sandbox")
    command.append(page.as_uri())
    out_path, err_path = work / f"out-{width}.html", work / f"err-{width}.txt"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pytest.fail(f"the browser ran past {timeout}s at width {width}")
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
    if code != 0:
        tail = err_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        pytest.fail(f"the browser exited {code} at width {width}: {tail}")
    dom = out_path.read_text(encoding="utf-8", errors="replace")
    blocks = [block for block in re.findall(r'<pre id="__out">(.*?)</pre>', dom, re.S) if block[:1] in ("[", "{")]
    if not blocks:
        pytest.fail(f"no result from the browser at width {width}")
    return json.loads(html.unescape(blocks[-1]))


# --- the page --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser() -> str:
    return browser_or_skip(os.environ, shutil.which, MAC_CHROME)


@pytest.fixture(scope="module")
def payload(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("dashboard-workspace")
    root, repos = work / "ws", {"a": work / "repo-a", "b": work / "repo-b"}
    for directory in (root, *repos.values()):
        directory.mkdir()
    for title, repo, tags in FIXTURE:
        allocate_project(root, title=title, working_directory=repos[repo], tags=tags)
    data = Dashboard(root, work / ".claude").payload()
    assert len(data["projects"]) == len(FIXTURE)
    return data


def _page(tmp_path: Path, payload: dict[str, Any], script: str, source: str | None = None) -> Path:
    page = tmp_path / "page.html"
    page.write_text(build_page(source or PAGE.read_text(encoding="utf-8"), payload, script), encoding="utf-8")
    return page


def test_the_tag_filter_behaves_in_a_real_browser(browser: str, payload: dict[str, Any], tmp_path: Path) -> None:
    results = run_page(browser, _page(tmp_path, payload, CLICKS), 1440)
    assert [item["name"] for item in results] == CHECK_NAMES  # every check ran, before any verdict
    failed = [f"{item['name']} [{item['detail']}]" for item in results if not item["ok"]]
    assert failed == []


def test_the_layout_holds_at_every_width(browser: str, payload: dict[str, Any], tmp_path: Path) -> None:
    measured = {width: run_page(browser, _page(tmp_path, payload, MEASURE), width) for width in LAYOUT}
    assert sorted(measured) == sorted(LAYOUT)
    problems = []
    for width, (wrap, columns, rail) in LAYOUT.items():
        got = measured[width]
        ok = got["wrapWidth"] == wrap and got["rail"] == rail and not got["scrolls"]
        if columns is not None:
            ok = ok and got["columns"] == columns
        if not ok:
            problems.append(f"{width}px: {got}")
    assert problems == []


def test_the_check_fails_a_page_whose_tag_filter_matches_any(
    browser: str, payload: dict[str, Any], tmp_path: Path
) -> None:
    # Negative control: match-any instead of match-all. The guard keeps the no-tag case showing every card, so
    # the click script runs to the end; the test must see failed checks, and so passes.
    broken = _replace_once(PAGE.read_text(encoding="utf-8"), MATCH_ALL, MATCH_ANY)
    results = run_page(browser, _page(tmp_path, payload, CLICKS, broken), 1440)
    assert [item["name"] for item in results] == CHECK_NAMES
    failed = {item["name"] for item in results if not item["ok"]}
    assert "two tags match all: 1 project" in failed


# --- the harness's own failure modes ---------------------------------------------------------------


def _stub(tmp_path: Path, body: str) -> str:
    script = tmp_path / "stub-browser"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _blank_page(tmp_path: Path) -> Path:
    page = tmp_path / "blank.html"
    page.write_text("<html></html>", encoding="utf-8")
    return page


def test_no_browser_skips_by_default_and_names_what_it_searched(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match="no browser found; searched PATH:google-chrome"):
        browser_or_skip({}, lambda name: None, tmp_path / "missing")


def test_no_browser_fails_when_one_is_required(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match=r"PATH:chromium-browser, .*missing \(REQUIRE_BROWSER=1\)"):
        browser_or_skip({"REQUIRE_BROWSER": "1"}, lambda name: None, tmp_path / "missing")


def test_an_explicit_browser_that_does_not_exist_fails_rather_than_skips(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="not an executable browser"):
        find_browser({"DASHBOARD_BROWSER": str(tmp_path / "nope")}, lambda name: None, tmp_path / "missing")
    with pytest.raises(pytest.fail.Exception, match="not an executable browser"):
        find_browser({"DASHBOARD_BROWSER": "no-such-browser"}, lambda name: None, tmp_path / "missing")


def test_discovery_order(tmp_path: Path) -> None:
    stub = _stub(tmp_path, "exit 0")
    assert find_browser({"DASHBOARD_BROWSER": stub}, lambda name: None, tmp_path / "missing")[0] == stub
    assert find_browser({"DASHBOARD_BROWSER": "stub"}, lambda name: stub, tmp_path / "missing")[0] == stub
    # Empty counts as unset; PATH comes before the macOS app.
    found, searched = find_browser({"DASHBOARD_BROWSER": ""}, {"chromium": stub}.get, Path(stub))
    assert found == stub and searched == ["PATH:google-chrome", "PATH:google-chrome-stable", "PATH:chromium"]
    assert find_browser({}, lambda name: None, Path(stub)) == (stub, [*(f"PATH:{n}" for n in PATH_NAMES), stub])


def test_a_browser_that_exits_non_zero_fails(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="exited 3"):
        run_page(_stub(tmp_path, "echo crashed >&2; exit 3"), _blank_page(tmp_path), 800)


def test_a_browser_that_reports_nothing_fails(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="no result"):
        run_page(_stub(tmp_path, "echo '<pre id=\"__out\"></pre>'; exit 0"), _blank_page(tmp_path), 800)


def _gone(pid: int) -> bool:
    """Whether `pid` has exited (a zombie left for init to reap counts as gone)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
        if state.stdout.strip().startswith("Z"):
            return True
        time.sleep(0.1)
    return False


def test_a_hung_browser_and_its_child_are_killed_at_the_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="ran past 1"):
        stub = _stub(tmp_path, f"sleep 30 & echo $! > {pid_file}\nexec sleep 30")
        run_page(stub, _blank_page(tmp_path), 800, timeout=1)
    assert time.monotonic() - started < 10
    assert _gone(int(pid_file.read_text(encoding="utf-8")))


def test_a_helper_left_running_after_success_is_killed_and_the_result_kept(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    body = f"sleep 30 & echo $! > {pid_file}\necho '<pre id=\"__out\">[{{&quot;ok&quot;: true}}]</pre>'\nexit 0"
    assert run_page(_stub(tmp_path, body), _blank_page(tmp_path), 800, timeout=20) == [{"ok": True}]
    assert time.monotonic() - started < 10
    assert _gone(int(pid_file.read_text(encoding="utf-8")))
