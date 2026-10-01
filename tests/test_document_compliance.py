"""Every document carries a render-contract verdict without anyone running it.

``reckon audit-doc`` finds a malformed document when someone remembers to run
it. Discovery meanwhile tolerates one silently: a truncated file lists as if
whole, and an empty one drops out of the listing without a word. These tests
pin the verdict store the server reads: one verdict per document, kept beside
the stat identity of every file it was computed from, and never served once
those files move.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import compliance, file_memo, metadata_index, serve

_PROJECT = "sample"


def _doc(slug: str, *, kind: str = "research", body: str = "<p>Prose.</p>") -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="{kind}">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="done">
<title>{slug}</title></head><body><main class="plan-doc">
<section id="s1"><h2 id="h1">§1 — Findings</h2>{body}</section>
</main></body></html>
"""


def _unclosed_doc(slug: str) -> str:
    return _doc(slug).replace("</section>", "")


def _bump(path: Path, text: str) -> None:
    previous = path.stat().st_mtime_ns
    path.write_text(text)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1)))


@pytest.fixture(autouse=True)
def _isolated_caches():
    file_memo.clear()
    metadata_index.clear()
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    yield
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture()
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    docs = tmp_path / "repository" / "docs"
    (docs / "research").mkdir(parents=True)
    (docs / "research" / "good-doc.html").write_text(_doc("good-doc"))
    (docs / "research" / "broken-doc.html").write_text(_unclosed_doc("broken-doc"))
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    return docs


def test_check_counts_errors_and_carries_their_findings(docs_dir: Path):
    good = compliance.check_document(docs_dir / "research" / "good-doc.html", _PROJECT)
    broken = compliance.check_document(
        docs_dir / "research" / "broken-doc.html", _PROJECT
    )

    assert good["errors"] == 0
    assert broken["errors"] >= 1
    assert "section-unclosed" in {f["code"] for f in broken["findings"]}
    assert all(f["severity"] in {"error", "warn"} for f in broken["findings"])


def test_an_unchecked_project_reports_every_document_pending(docs_dir: Path):
    summary = compliance.project_checks(docs_dir, _PROJECT)

    assert summary["checked"] == 0
    assert summary["pending"] == 2
    assert summary["documents"] == []


def test_a_refreshed_project_lists_only_its_failing_documents(docs_dir: Path):
    compliance.refresh(docs_dir, _PROJECT)
    summary = compliance.project_checks(docs_dir, _PROJECT)

    assert summary["checked"] == 2
    assert summary["pending"] == 0
    assert summary["failing"] == 1
    [failing] = summary["documents"]
    assert (failing["type"], failing["slug"]) == ("research", "broken-doc")
    assert failing["path"] == "research/broken-doc.html"
    assert failing["errors"] >= 1


def test_a_verdict_is_not_served_once_its_document_moves(docs_dir: Path):
    compliance.refresh(docs_dir, _PROJECT)
    _bump(docs_dir / "research" / "broken-doc.html", _doc("broken-doc"))

    summary = compliance.project_checks(docs_dir, _PROJECT)
    assert summary["pending"] == 1
    assert summary["documents"] == []

    compliance.refresh(docs_dir, _PROJECT)
    assert compliance.project_checks(docs_dir, _PROJECT)["failing"] == 0


def test_verdicts_survive_a_new_process(docs_dir: Path, monkeypatch):
    compliance.refresh(docs_dir, _PROJECT)
    calls: list[Path] = []
    original = compliance.check_document

    def counted(path, project):
        calls.append(Path(path))
        return original(path, project)

    monkeypatch.setattr(compliance, "check_document", counted)
    file_memo.clear()

    assert compliance.refresh(docs_dir, _PROJECT).checked == 0
    assert compliance.project_checks(docs_dir, _PROJECT)["failing"] == 1
    assert calls == []


def test_a_verdict_holds_on_another_host_of_the_shared_filesystem(
    docs_dir: Path, monkeypatch
):
    # A shared filesystem reports a different device number for the same file
    # on each host; the server and a command run on another node share one
    # store, so the verdict must not depend on it.
    compliance.refresh(docs_dir, _PROJECT)
    original = compliance.file_signature

    def other_host(path):
        device, *rest = original(path)
        return (device + 24, *rest)

    monkeypatch.setattr(compliance, "file_signature", other_host)

    summary = compliance.project_checks(docs_dir, _PROJECT)
    assert summary["pending"] == 0
    assert summary["failing"] == 1


def test_a_removed_document_leaves_the_store(docs_dir: Path):
    compliance.refresh(docs_dir, _PROJECT)
    (docs_dir / "research" / "broken-doc.html").unlink()
    compliance.refresh(docs_dir, _PROJECT)

    stored = compliance.stored_verdicts(docs_dir, _PROJECT)
    assert set(stored) == {"research/good-doc.html"}


def test_a_record_verdict_moves_when_a_fragment_does(docs_dir: Path):
    evidence = docs_dir / "evidence"
    fragments = evidence / "fragments" / "work"
    fragments.mkdir(parents=True)
    record = evidence / "work-landed.html"
    record.write_text(
        _doc("work-landed", kind="evidence").replace(
            '<meta name="plan-status" content="done">',
            '<meta name="plan-status" content="done">\n'
            '<meta name="plan-evidence-for" content="work">',
        )
    )
    compliance.refresh(docs_dir, _PROJECT)
    assert compliance.project_checks(docs_dir, _PROJECT)["pending"] == 0

    (fragments / "a.html").write_text('<section id="s9"><h2>§9</h2><p>x</p>')
    assert compliance.project_checks(docs_dir, _PROJECT)["pending"] == 1


def test_document_check_computes_once_and_reuses(docs_dir: Path, monkeypatch):
    path = docs_dir / "research" / "broken-doc.html"
    calls: list[Path] = []
    original = compliance.check_document

    def counted(target, project):
        calls.append(Path(target))
        return original(target, project)

    monkeypatch.setattr(compliance, "check_document", counted)
    first = compliance.document_check(docs_dir, _PROJECT, path)
    second = compliance.document_check(docs_dir, _PROJECT, path)

    assert first == second
    assert first["errors"] >= 1
    assert calls == [path]
    assert compliance.project_checks(docs_dir, _PROJECT)["pending"] == 1


@contextmanager
def _served():
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(port: int, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture()
def refresh_starts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    started: list[list[str]] = []

    class _Process:
        def poll(self):
            return None

    def spawn(argv):
        started.append(list(argv))
        return _Process()

    monkeypatch.setattr(serve, "_spawn_check_refresh", spawn)
    monkeypatch.setattr(serve, "_CHECK_REFRESH_ENABLED", True)
    serve._CHECK_REFRESHES.clear()
    yield started
    serve._CHECK_REFRESHES.clear()


def test_project_route_starts_one_refresh_while_documents_are_pending(
    docs_dir: Path, refresh_starts
):
    with _served() as port:
        status, first = _get(port, f"/_checks/{_PROJECT}")
        _, second = _get(port, f"/_checks/{_PROJECT}")

    assert status == 200
    assert first["pending"] == 2
    assert first["refreshing"] is True
    assert second["refreshing"] is True
    assert len(refresh_starts) == 1
    argv = refresh_starts[0]
    assert argv[argv.index("-m") + 1 : argv.index("-m") + 3] == [
        "reckon.compliance",
        "refresh",
    ]
    assert argv[argv.index("--project") + 1] == _PROJECT


def test_a_library_handler_never_starts_a_refresh(docs_dir: Path, monkeypatch):
    # Only the served process opts in; a handler under test or in another
    # caller must not start a child that writes a cache outside its sandbox.
    started: list[list[str]] = []
    monkeypatch.setattr(serve, "_spawn_check_refresh", started.append)
    with _served() as port:
        status, body = _get(port, f"/_checks/{_PROJECT}")

    assert status == 200
    assert body["pending"] == 2
    assert body["refreshing"] is False
    assert started == []


def test_project_route_lists_failures_and_starts_nothing_when_current(
    docs_dir: Path, refresh_starts
):
    compliance.refresh(docs_dir, _PROJECT)
    with _served() as port:
        status, body = _get(port, f"/_checks/{_PROJECT}")

    assert status == 200
    assert body["pending"] == 0
    assert body["refreshing"] is False
    assert [d["slug"] for d in body["documents"]] == ["broken-doc"]
    assert refresh_starts == []


def test_document_route_answers_one_documents_verdict(docs_dir: Path, refresh_starts):
    with _served() as port:
        status, body = _get(port, f"/_checks/{_PROJECT}/research/broken-doc")
        missing, _ = _get(port, f"/_checks/{_PROJECT}/research/no-such-doc")

    assert status == 200
    assert (body["type"], body["slug"]) == ("research", "broken-doc")
    assert body["errors"] >= 1
    assert missing == 404


def test_unknown_project_is_refused(docs_dir: Path, refresh_starts):
    with _served() as port:
        status, _ = _get(port, "/_checks/no-such-project")
    assert status == 404
