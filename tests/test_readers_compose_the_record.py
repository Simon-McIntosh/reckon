"""The served record and the audited record are the composed form.

Two readers of a plan's cumulative evidence record — the file route in
``reckon/serve.py`` and ``reckon.doccheck.audit_file`` — must see the record
composed with its fragments, because a fragment carries the anchors a reader
is expected to find. A record with no fragments is served and audited exactly
as the raw file, so nothing is rewritten for the records that predate
fragments. A record is spelled at either the archived or the live path, and
both spellings compose. When the ledger cannot be read, both readers fall back
to the record's own bytes and say so, because a record whose fragments are
hidden by a read failure otherwise looks exactly like one with none.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
from pathlib import Path

import pytest

from reckon import doccheck, ledger, serve
from reckon.evidence import EvidenceSynthesisError

PROJECT = "proj"
ARCHIVE_PLAN = "composed-plan"
LIVE_PLAN = "live-plan"
BARE_PLAN = "bare-plan"

ARCHIVE_NODE = "only-node"
LIVE_NODE = "live-node"

RECORD_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="docs-project" content="proj">\n'
    b'  <meta name="reckon-type" content="evidence">\n'
    b'  <meta name="plan-evidence-for" content="composed-plan">\n'
    b'</head><body><main class="plan-doc"><h1>Record</h1></main></body></html>\n'
)

# A relative <img src> is an audit ERROR (it 404s in the SPA), and it lives
# only in the fragment, so it is reported iff the fragment was composed in.
FRAGMENT_BYTES = (
    b'<article class="landed-fragment" data-node="NODE">\n'
    b'<p>anchor</p><img src="fig.png" alt="relative">\n'
    b"</article>\n"
)

# The two record spellings, each with the plan it names and its fragment node.
SPELLINGS = [
    pytest.param(ARCHIVE_PLAN, "archive", ARCHIVE_NODE, id="archive"),
    pytest.param(LIVE_PLAN, "live", LIVE_NODE, id="live"),
]


@pytest.fixture(autouse=True)
def _clear_serve_caches():
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()
    yield
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()


def _fragment_bytes(node: str) -> bytes:
    return FRAGMENT_BYTES.replace(b"NODE", node.encode())


def _record_path(docs: Path, plan: str, spelling: str) -> Path:
    if spelling == "archive":
        return docs / "evidence" / "archive" / f"{plan}-landed.html"
    return docs / "evidence" / f"{plan}-landed.html"


def _fragment_path(docs: Path, plan: str, node: str) -> Path:
    return docs / "evidence" / "fragments" / plan / f"{node}.html"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)

    for index, (plan, spelling, node) in enumerate(
        [(ARCHIVE_PLAN, "archive", ARCHIVE_NODE), (LIVE_PLAN, "live", LIVE_NODE)]
    ):
        fragment = _fragment_path(docs, plan, node)
        fragment.parent.mkdir(parents=True, exist_ok=True)
        fragment.write_bytes(_fragment_bytes(node))
        ledger.append_run(
            PROJECT,
            ledger.build_record(
                run_id=f"run-{node}",
                plan=plan,
                section="delivery",
                node=node,
                gate="passed",
                completed_at=f"2026-08-24T19:0{index + 1}:00Z",
                completed_at_source="provided",
            ),
            root=root,
        )
        record = _record_path(docs, plan, spelling)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_bytes(RECORD_BYTES)

    bare = _record_path(docs, BARE_PLAN, "archive")
    bare.parent.mkdir(parents=True, exist_ok=True)
    bare.write_bytes(RECORD_BYTES)

    mounts_file = tmp_path / "config" / "mounts.json"
    mounts_file.parent.mkdir(parents=True, exist_ok=True)
    mounts_file.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    return root


def _serve(repository: Path):
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(port: int, path: str, etag: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"If-None-Match": etag} if etag else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("ETag")
    finally:
        connection.close()


def _record_route(plan: str, spelling: str) -> str:
    # The canonical typed-resource route; the ``.html`` spelling 308s to it.
    if spelling == "archive":
        return f"/{PROJECT}/evidence/archive/{plan}-landed"
    return f"/{PROJECT}/evidence/{plan}-landed"


def _composition_fails(*args, **kwargs):
    raise EvidenceSynthesisError("ledger unavailable")


@pytest.mark.parametrize("plan,spelling,node", SPELLINGS)
def test_the_served_record_is_the_composed_bytes_and_its_etag_tracks_a_fragment(
    repository: Path, plan: str, spelling: str, node: str
) -> None:
    server, thread = _serve(repository)
    try:
        status, body, etag = _get(server.server_port, _record_route(plan, spelling))
        assert status == 200
        assert body == RECORD_BYTES + _fragment_bytes(node)
        assert etag

        # A fragment landing after the record was written must be seen, and the
        # ETag must change with it, or a revalidating reader is left on the
        # stale bytes. A stat-derived tag over the record alone would not move.
        fragment = _fragment_path(repository / "docs", plan, node)
        fragment.write_bytes(
            _fragment_bytes(node) + b"<!-- a later fragment edit -->\n"
        )

        status, revalidated, new_etag = _get(
            server.server_port, _record_route(plan, spelling), etag=etag
        )
        assert status == 200
        assert revalidated == RECORD_BYTES + fragment.read_bytes()
        assert new_etag != etag

        # The new tag revalidates instead of re-downloading.
        status, _, _ = _get(
            server.server_port, _record_route(plan, spelling), etag=new_etag
        )
        assert status == 304
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("plan,spelling,node", SPELLINGS)
def test_audit_file_audits_the_composed_record(
    repository: Path, plan: str, spelling: str, node: str
) -> None:
    record = _record_path(repository / "docs", plan, spelling)

    codes = {finding.code for finding in doccheck.audit_file(record, project=PROJECT)}
    assert "img-relative-src" in codes

    # The same finding is absent from the raw record, so its presence is the
    # composition and not the record's own content. This is the negative
    # control for the assertion above.
    raw_codes = {
        finding.code for finding in doccheck.audit_html(RECORD_BYTES.decode("utf-8"))
    }
    assert "img-relative-src" not in raw_codes


def test_a_record_with_no_fragments_is_served_and_audited_byte_identically(
    repository: Path,
) -> None:
    server, thread = _serve(repository)
    try:
        status, body, _ = _get(server.server_port, _record_route(BARE_PLAN, "archive"))
        assert status == 200
        assert body == RECORD_BYTES
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    record = _record_path(repository / "docs", BARE_PLAN, "archive")
    composed = [
        finding.fmt() for finding in doccheck.audit_file(record, project=PROJECT)
    ]
    raw = [
        finding.fmt() for finding in doccheck.audit_html(RECORD_BYTES.decode("utf-8"))
    ]
    assert composed == raw


def test_the_route_falls_back_to_the_record_and_says_so_when_composition_fails(
    repository: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(serve, "compose_landed_record", _composition_fails)
    server, thread = _serve(repository)
    try:
        with caplog.at_level(logging.WARNING, logger="reckon.serve"):
            status, body, _ = _get(
                server.server_port, _record_route(ARCHIVE_PLAN, "archive")
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # The read does not fail: the record's own bytes are served.
    assert status == 200
    assert body == RECORD_BYTES
    # And the uncomposed read is observable, naming the record and the error.
    record_name = f"{ARCHIVE_PLAN}-landed.html"
    messages = [record.getMessage() for record in caplog.records]
    assert any(record_name in message for message in messages)
    assert any("ledger unavailable" in message for message in messages)


def test_audit_file_falls_back_to_the_record_with_a_warning_when_composition_fails(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("reckon.evidence.compose_landed_record", _composition_fails)
    record = _record_path(repository / "docs", ARCHIVE_PLAN, "archive")

    findings = doccheck.audit_file(record, project=PROJECT)

    warned = [f for f in findings if f.code == "record-not-composed"]
    assert warned, [f.fmt() for f in findings]
    assert warned[0].severity == "warn"
    assert f"{ARCHIVE_PLAN}-landed.html" in warned[0].message
    assert "ledger unavailable" in warned[0].message
    # The fallback audited the record's own bytes, so the fragment-only error
    # is not reported — the warning is what carries its absence.
    assert "img-relative-src" not in {f.code for f in findings}
