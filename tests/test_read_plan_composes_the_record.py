"""The read path returns the composed record, keyed by the record's own metas.

A cumulative evidence record is composed with its fragments, and ``read_plan``
reads it through ``reckon.mcp._typed_resource_text``. Whether a document is a
record — and which plan it composes for — is decided by the document's own
``reckon-type`` and ``plan-evidence-for`` metas, not by a ``-landed.html`` name:
a document merely named like a record whose metas name no plan is served,
audited and read as an ordinary file. When the meta names a plan the filename
does not, the meta wins, so the fragments composed are those of the plan the
record actually documents. A record with no fragments reads byte-identically.

Each case carries the fragment it would otherwise be blind to, so a resolver
that decided by name would produce composed bytes and fail here — the fragment
is the observable difference, not the resolver's return value.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reckon import doccheck, ledger, serve
from reckon.mcp import _typed_resource_text
from reckon.mcp_views import ResourceSelector

PROJECT = "proj"
COMPOSED_PLAN = "composed-plan"
BARE_PLAN = "bare-plan"
#: Names a record by its filename, but a plan only by the filename: its metas
#: carry no ``plan-evidence-for``, so it documents no plan.
MISNAMED = "misnamed-rollup"

COMPOSED_NODE = "only-node"
MISNAMED_NODE = "misnamed-node"

RECORD_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="docs-project" content="proj">\n'
    b'  <meta name="reckon-type" content="evidence">\n'
    b'  <meta name="plan-slug" content="RECORD_PLAN-landed">\n'
    b'  <meta name="plan-evidence-for" content="RECORD_PLAN">\n'
    b'</head><body><main class="plan-doc"><h1>Record</h1></main></body></html>\n'
)

#: A record whose metas name no plan — the whole of what makes it not a record.
MISNAMED_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="docs-project" content="proj">\n'
    b'  <meta name="reckon-type" content="evidence">\n'
    b'  <meta name="plan-slug" content="misnamed-rollup-landed">\n'
    b'</head><body><main class="plan-doc"><h1>Rollup</h1></main></body></html>\n'
)

#: A relative <img src> is an audit ERROR and would 404 in the SPA, and it lives
#: only in the fragment, so it is present iff the fragment was composed in.
FRAGMENT_BYTES = (
    b'<article class="landed-fragment" data-node="NODE">\n'
    b'<p>fragment-marker</p><img src="fig.png" alt="relative">\n'
    b"</article>\n"
)


def _record_bytes(plan: str) -> bytes:
    return RECORD_BYTES.replace(b"RECORD_PLAN", plan.encode())


def _fragment_bytes(node: str) -> bytes:
    return FRAGMENT_BYTES.replace(b"NODE", node.encode())


def _record_path(docs: Path, plan: str) -> Path:
    return docs / "evidence" / "archive" / f"{plan}-landed.html"


def _fragment_path(docs: Path, plan: str, node: str) -> Path:
    return docs / "evidence" / "fragments" / plan / f"{node}.html"


def _fragment_written(docs: Path, plan: str, node: str) -> None:
    fragment = _fragment_path(docs, plan, node)
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_bytes(_fragment_bytes(node))


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
    (docs / "evidence" / "archive").mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)

    # A promoted record with one fragment: read_plan must return the fragment.
    _fragment_written(docs, COMPOSED_PLAN, COMPOSED_NODE)
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=f"run-{COMPOSED_NODE}",
            plan=COMPOSED_PLAN,
            section="delivery",
            node=COMPOSED_NODE,
            gate="passed",
            completed_at="2026-08-24T19:01:00Z",
            completed_at_source="provided",
        ),
        root=root,
    )
    _record_path(docs, COMPOSED_PLAN).write_bytes(_record_bytes(COMPOSED_PLAN))

    # A record with no fragments at all: read_plan must return its own bytes.
    _record_path(docs, BARE_PLAN).write_bytes(_record_bytes(BARE_PLAN))

    # A document named like a record whose metas name no plan, with a fragment
    # parked under the pseudo-slug its filename would suggest. A resolver that
    # decided by name would compose that fragment in; the fragment is what makes
    # the ordinary-file assertion observable rather than tautological.
    _record_path(docs, MISNAMED).write_bytes(MISNAMED_BYTES)
    _fragment_written(docs, MISNAMED, MISNAMED_NODE)

    mounts_file = tmp_path / "config" / "mounts.json"
    mounts_file.parent.mkdir(parents=True, exist_ok=True)
    mounts_file.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    return root


def _select(resource_id: str) -> ResourceSelector:
    return ResourceSelector(project=PROJECT, type="evidence", id=resource_id, archived=True)


def _read(repository: Path, resource_id: str) -> str:
    return _typed_resource_text(_select(resource_id), str(repository))


def test_a_record_read_through_read_plan_carries_its_fragment(repository: Path) -> None:
    text = _read(repository, f"{COMPOSED_PLAN}-landed")

    assert text == (_record_bytes(COMPOSED_PLAN) + _fragment_bytes(COMPOSED_NODE)).decode()
    assert "fragment-marker" in text


def test_a_document_named_like_a_record_whose_metas_name_no_plan_is_ordinary(
    repository: Path,
) -> None:
    docs = repository / "docs"
    record = _record_path(docs, MISNAMED)
    raw = MISNAMED_BYTES.decode()
    # The fragment exists and is audibly different, so "ordinary" is a claim
    # about what was NOT composed in.
    assert _fragment_path(docs, MISNAMED, MISNAMED_NODE).is_file()

    assert _read(repository, f"{MISNAMED}-landed") == raw

    served = _served_bytes(repository, f"{MISNAMED}-landed")
    assert served == MISNAMED_BYTES

    audited = [finding.fmt() for finding in doccheck.audit_file(record, project=PROJECT)]
    assert audited == [
        finding.fmt() for finding in doccheck.audit_html(raw, project=PROJECT)
    ]


def test_a_record_with_no_fragments_reads_byte_identically(repository: Path) -> None:
    assert not _fragment_path(repository / "docs", BARE_PLAN, "anything").parent.exists()
    assert _read(repository, f"{BARE_PLAN}-landed").encode() == _record_bytes(BARE_PLAN)


def _served_bytes(repository: Path, resource_id: str) -> bytes:
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", f"/{PROJECT}/evidence/archive/{resource_id}")  # noqa: E501
            response = connection.getresponse()
            assert response.status == 200
            return response.read()
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)