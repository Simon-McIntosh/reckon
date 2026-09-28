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

When the ledger cannot be read the record's own bytes are returned and the gap
is visible: a warning naming the record and the failure is logged and carried
on the response, so an uncomposed read never looks like a record with no
fragments.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reckon import doccheck, ledger, serve
from reckon.evidence import EvidenceSynthesisError
from reckon.mcp import (
    _append_read_warning,
    _typed_resource_text,
    _typed_resource_text_and_warning,
)
from reckon.mcp_views import ResourceSelector

PROJECT = "proj"
COMPOSED_PLAN = "composed-plan"
BARE_PLAN = "bare-plan"
#: Names a record by its filename, but a plan only by the filename: its metas
#: carry no ``plan-evidence-for``, so it documents no plan.
MISNAMED = "misnamed-rollup"
#: Filename and meta disagree: the file is named for ``NAMING_PLAN`` and its
#: ``plan-evidence-for`` names ``META_PLAN``. The meta must win.
NAMING_PLAN = "naming-plan"
META_PLAN = "meta-plan"

COMPOSED_NODE = "only-node"
MISNAMED_NODE = "misnamed-node"
META_NODE = "meta-node"
NAMING_NODE = "naming-node"

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

#: A record whose filename (and ``plan-slug``, which keys its typed identity)
#: names ``NAMING_PLAN`` while its ``plan-evidence-for`` names ``META_PLAN``.
#: The file is read as ``NAMING_PLAN``'s record, and the plan it composes for
#: is the one its own meta names.
NAMING_RECORD_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="docs-project" content="proj">\n'
    b'  <meta name="reckon-type" content="evidence">\n'
    b'  <meta name="plan-slug" content="naming-plan-landed">\n'
    b'  <meta name="plan-evidence-for" content="meta-plan">\n'
    b'</head><body><main class="plan-doc"><h1>Record</h1></main></body></html>\n'
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

    # The filename and the meta disagree about the plan. The record is named for
    # NAMING_PLAN and its metas name META_PLAN, and each plan has its own
    # fragment: only the meta's plan's fragment may be composed in.
    _record_path(docs, NAMING_PLAN).write_bytes(NAMING_RECORD_BYTES)
    _fragment_written(docs, META_PLAN, META_NODE)
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=f"run-{META_NODE}",
            plan=META_PLAN,
            section="delivery",
            node=META_NODE,
            gate="passed",
            completed_at="2026-08-24T19:02:00Z",
            completed_at_source="provided",
        ),
        root=root,
    )
    _fragment_written(docs, NAMING_PLAN, NAMING_NODE)

    mounts_file = tmp_path / "config" / "mounts.json"
    mounts_file.parent.mkdir(parents=True, exist_ok=True)
    mounts_file.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    return root


def _select(resource_id: str) -> ResourceSelector:
    return ResourceSelector(
        project=PROJECT, type="evidence", id=resource_id, archived=True
    )


def _read(repository: Path, resource_id: str) -> str:
    return _typed_resource_text(_select(resource_id), str(repository))


def test_a_record_read_through_read_plan_carries_its_fragment(repository: Path) -> None:
    text = _read(repository, f"{COMPOSED_PLAN}-landed")

    assert (
        text == (_record_bytes(COMPOSED_PLAN) + _fragment_bytes(COMPOSED_NODE)).decode()
    )
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

    audited = [
        finding.fmt() for finding in doccheck.audit_file(record, project=PROJECT)
    ]
    assert audited == [
        finding.fmt() for finding in doccheck.audit_html(raw, project=PROJECT)
    ]


def test_a_record_with_no_fragments_reads_byte_identically(repository: Path) -> None:
    assert not _fragment_path(
        repository / "docs", BARE_PLAN, "anything"
    ).parent.exists()
    assert _read(repository, f"{BARE_PLAN}-landed").encode() == _record_bytes(BARE_PLAN)


def test_the_meta_names_the_plan_where_the_filename_names_another(
    repository: Path,
) -> None:
    docs = repository / "docs"
    # Both plans carry a fragment, so the composition tells them apart by which
    # fragment came in — not by the resolver's return value.
    assert _fragment_path(docs, META_PLAN, META_NODE).is_file()
    assert _fragment_path(docs, NAMING_PLAN, NAMING_NODE).is_file()

    text = _read(repository, f"{NAMING_PLAN}-landed")

    assert text == (NAMING_RECORD_BYTES + _fragment_bytes(META_NODE)).decode()
    assert f'data-node="{META_NODE}"' in text
    # The plan the filename suggests contributes nothing.
    assert f'data-node="{NAMING_NODE}"' not in text


def _composition_fails(*args, **kwargs):
    raise EvidenceSynthesisError("ledger unavailable")


def test_a_composition_failure_reads_the_record_and_says_so(
    repository: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("reckon.evidence.compose_landed_record", _composition_fails)

    with caplog.at_level("WARNING", logger="reckon.mcp"):
        text, warning = _typed_resource_text_and_warning(
            _select(f"{COMPOSED_PLAN}-landed"), str(repository)
        )

    # The read does not fail: the record's own bytes come back.
    assert text == _record_bytes(COMPOSED_PLAN).decode()
    # And the uncomposed read is observable: the warning names the record and
    # the failure, and it is logged, not swallowed.
    assert warning is not None
    assert f"{COMPOSED_PLAN}-landed.html" in warning
    assert "ledger unavailable" in warning
    logged = [record.getMessage() for record in caplog.records]
    assert any(f"{COMPOSED_PLAN}-landed.html" in message for message in logged)
    assert any("ledger unavailable" in message for message in logged)


def test_a_healthy_read_carries_no_warning(repository: Path) -> None:
    text, warning = _typed_resource_text_and_warning(
        _select(f"{COMPOSED_PLAN}-landed"), str(repository)
    )
    assert warning is None
    assert (
        text == (_record_bytes(COMPOSED_PLAN) + _fragment_bytes(COMPOSED_NODE)).decode()
    )


def test_a_read_fallback_warning_reaches_the_response() -> None:
    result = _append_read_warning({"warnings": ["existing"]}, "the read fell back")
    assert result["warnings"] == ["existing", "the read fell back"]
    # A healthy read leaves the response's warnings untouched.
    assert _append_read_warning({"warnings": ["existing"]}, None)["warnings"] == [
        "existing"
    ]


def _served_bytes(repository: Path, resource_id: str) -> bytes:
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=5
        )
        try:
            connection.request("GET", f"/{PROJECT}/evidence/archive/{resource_id}")
            response = connection.getresponse()
            assert response.status == 200
            return response.read()
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
