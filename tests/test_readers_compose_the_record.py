"""The served record and the audited record are the composed form.

Two readers of a plan's cumulative evidence record — the file route in
``reckon/serve.py`` and ``reckon.doccheck.audit_file`` — must see the record
composed with its fragments, because a fragment carries the anchors a reader
is expected to find. A record with no fragments is served and audited exactly
as the raw file, so nothing is rewritten for the records that predate
fragments.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reckon import doccheck, ledger, serve

PROJECT = "proj"
PLAN = "composed-plan"
BARE_PLAN = "bare-plan"

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
    b'<article class="landed-fragment" data-node="only-node">\n'
    b'<p>anchor</p><img src="fig.png" alt="relative">\n'
    b"</article>\n"
)


@pytest.fixture(autouse=True)
def _clear_serve_caches():
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()
    yield
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)

    fragment_dir = docs / "evidence" / "fragments" / PLAN
    fragment_dir.mkdir(parents=True)
    (fragment_dir / "only-node.html").write_bytes(FRAGMENT_BYTES)

    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="run-only-node",
            plan=PLAN,
            section="delivery",
            node="only-node",
            gate="passed",
            completed_at="2026-08-24T19:01:00Z",
            completed_at_source="provided",
        ),
        root=root,
    )

    archive = docs / "evidence" / "archive"
    archive.mkdir(parents=True)
    (archive / f"{PLAN}-landed.html").write_bytes(RECORD_BYTES)
    (archive / f"{BARE_PLAN}-landed.html").write_bytes(RECORD_BYTES)

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


def _record_route(plan: str) -> str:
    # The canonical typed-resource route; the ``.html`` spelling 308s to it.
    return f"/{PROJECT}/evidence/archive/{plan}-landed"


def test_the_served_record_is_the_composed_bytes_and_its_etag_tracks_a_fragment(
    repository: Path,
) -> None:
    server, thread = _serve(repository)
    try:
        status, body, etag = _get(server.server_port, _record_route(PLAN))
        assert status == 200
        assert body == RECORD_BYTES + FRAGMENT_BYTES
        assert etag

        # A fragment landing after the record was written must be seen, and the
        # ETag must change with it, or a revalidating reader is left on the
        # stale bytes. A stat-derived tag over the record alone would not move.
        fragment = (
            repository / "docs" / "evidence" / "fragments" / PLAN / "only-node.html"
        )
        fragment.write_bytes(FRAGMENT_BYTES + b"<!-- a later fragment edit -->\n")

        status, revalidated, new_etag = _get(
            server.server_port, _record_route(PLAN), etag=etag
        )
        assert status == 200
        assert revalidated == RECORD_BYTES + fragment.read_bytes()
        assert new_etag != etag

        # The new tag revalidates instead of re-downloading.
        status, _, _ = _get(server.server_port, _record_route(PLAN), etag=new_etag)
        assert status == 304
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_audit_file_audits_the_composed_record(repository: Path) -> None:
    record = repository / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"

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
        status, body, _ = _get(server.server_port, _record_route(BARE_PLAN))
        assert status == 200
        assert body == RECORD_BYTES
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    record = repository / "docs" / "evidence" / "archive" / f"{BARE_PLAN}-landed.html"
    composed = [
        finding.fmt() for finding in doccheck.audit_file(record, project=PROJECT)
    ]
    raw = [
        finding.fmt() for finding in doccheck.audit_html(RECORD_BYTES.decode("utf-8"))
    ]
    assert composed == raw
