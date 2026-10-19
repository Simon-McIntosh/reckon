"""The index row carries each plan's progress figure, so the bars paint at once.

The page paints from ``/_index/<project>`` before ``/_discover/<project>``
answers. If the index rows carried no progress, every bar would paint empty and
fill only when discovery landed. This serves a temporary project and asserts
that every plan's ``impl`` in the index rows equals its value in the discovery
rows, so the first paint and the derived merge show the same figure.

Two plans exercise the derivation's edges: one declared but not yet recorded
(no section records at all), and one whose hours are partial, so the section
without hours takes the mean of the ones present. Both payloads derive from the
same progress function, so neither is a hand-set figure the other could drift
from. The negative control, armed by
``RECKON_TEST_INDEX_DROPS_PLAN_PROGRESS=1``, drops ``impl`` from the index row;
the index-equals-discovery assertion then fails.
"""

from __future__ import annotations

import html as html_module
import http.client
import json
import os
import threading
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, metadata_index, serve

_PROJECT = "sample"
_DROP_PROGRESS_ENV = "RECKON_TEST_INDEX_DROPS_PLAN_PROGRESS"

#: One section record, matching the shape the parser reads.
_RECORD = (
    '<section data-reckon="section" data-id="{sid}" data-effort-hours="{hours}"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard" data-capability-verification="strict"'
    ' data-capability-risk="low" data-attempts="0" data-status="{status}"'
    ' data-links=""></section>'
)


def _plan_text(
    slug: str,
    title: str,
    *,
    declarations: dict | None,
    records: list[tuple[str, float, str]],
    impl: str | None,
) -> str:
    body: list[str] = []
    for sid, hours, status in records:
        body.append(f'<h2 id="{sid}">The {sid} work</h2>')
        body.append(_RECORD.format(sid=sid, hours=hours, status=status))
    recorded_ids = {sid for sid, _, _ in records}
    body.extend(
        f'<h2 id="{sid}">The {sid} work</h2>'
        for sid in (declarations or {})
        if sid not in recorded_ids
    )
    head = [
        '<!doctype html>\n<html lang="en"><head>\n',
        '<meta charset="utf-8">\n',
        f'<meta name="docs-project" content="{_PROJECT}">\n',
        '<meta name="reckon-type" content="plan">\n',
        f'<meta name="plan-slug" content="{slug}">\n',
        f'<meta name="plan-title" content="{title}">\n',
        '<meta name="plan-status" content="active">\n',
    ]
    if impl is not None:
        head.append(f'<meta name="plan-impl" content="{impl}">\n')
    # A recorded section's identity lives in the body; a declared one with no
    # record is named by a heading so the document still carries its section.
    if declarations is not None:
        escaped = html_module.escape(json.dumps(declarations), quote=True)
        head.append(f'<meta name="plan-section-declarations" content="{escaped}">\n')
    return (
        "".join(head)
        + "</head><body><main>\n"
        + "\n".join(body)
        + "\n</main></body></html>\n"
    )


# The plans the temporary project carries. Two pin the derivation's edges: a
# plan declared but not yet recorded (no section records), and one whose hours
# are partial (a declared section carries none and takes the present mean).
_PLANS = [
    (
        "all-hours",
        _plan_text(
            "all-hours",
            "All hours",
            declarations={"s1": "done", "s2": "implementable"},
            records=[("s1", 4, "done"), ("s2", 6, "implementable")],
            impl="0.9",
        ),
    ),
    (
        "partial-hours",
        _plan_text(
            "partial-hours",
            "Partial hours",
            declarations={"s1": "done", "s2": "implementable", "s3": "implementable"},
            records=[("s1", 2, "done"), ("s3", 4, "implementable")],
            impl="0.1",
        ),
    ),
    (
        "declared-unrecorded",
        _plan_text(
            "declared-unrecorded",
            "Declared, unrecorded",
            declarations={"s1": "done", "s2": "done", "s3": "implementable"},
            records=[],
            impl="0.08",
        ),
    ),
    (
        "authored",
        _plan_text(
            "authored",
            "Authored",
            declarations=None,
            records=[],
            impl="0.3",
        ),
    ),
]

_EXPECTED_IMPL = {
    "all-hours": 0.4,
    "partial-hours": 2 / 9,
    "declared-unrecorded": 2 / 3,
    "authored": 0.3,
}


def _clear_caches() -> None:
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _isolated_caches():
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: drop impl from the index row."""

    if os.environ.get(_DROP_PROGRESS_ENV) != "1":
        return

    original = metadata_index._resource_row

    def dropped(path, docs_dir, project, signature, stamp):
        row, plan, discovery = original(path, docs_dir, project, signature, stamp)
        if row is not None:
            row.pop("impl", None)
        return row, plan, discovery

    monkeypatch.setattr(metadata_index, "_resource_row", dropped)


@pytest.fixture()
def served_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A temporary project of sectioned plans, served on a loopback port."""

    docs_dir = tmp_path / "repository" / "docs"
    plans_dir = docs_dir / "plans"
    plans_dir.mkdir(parents=True)
    for slug, text in _PLANS:
        (plans_dir / f"{slug}.html").write_text(text, encoding="utf-8")

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "load_mounts", lambda: {_PROJECT: docs_dir})
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    _clear_caches()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, docs_dir
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(port: int, path: str) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_every_plan_impl_matches_between_the_index_and_discovery(
    served_project,
) -> None:
    port, docs_dir = served_project

    index_status, index_rows = _get(port, f"/_index/{_PROJECT}")
    discovery_status, discovery = _get(port, f"/_discover/{_PROJECT}")

    assert index_status == 200
    assert discovery_status == 200, discovery

    # A row that carried no impl reads as None, so the equality below is the
    # assertion that a missing figure fails on rather than a KeyError.
    index_impl = {
        row["slug"]: row.get("impl") for row in index_rows if row["type"] == "plan"
    }
    discovery_impl = {
        row["slug"]: row.get("impl")
        for row in discovery["inventory"]
        if row.get("type") == "plan"
    }

    # Every plan appears on both, and the two rows agree on the figure.
    assert set(index_impl) == {slug for slug, _ in _PLANS}
    assert discovery_impl == index_impl

    # The cases the measure names are present and reduced by the derivation:
    # a plan declared with no section records, and one with partial hours.
    assert index_impl["declared-unrecorded"] == pytest.approx(2 / 3)
    assert index_impl["partial-hours"] == pytest.approx(2 / 9)

    # Both rows take the figure from the one progress function, so neither is
    # a hand-set figure the other could drift from.
    for slug, _text in _PLANS:
        derived = _plan_html.parse_meta(docs_dir / "plans" / f"{slug}.html")["impl"]
        assert derived == pytest.approx(_EXPECTED_IMPL[slug])
        assert index_impl[slug] == pytest.approx(derived)
        assert discovery_impl[slug] == pytest.approx(derived)


def test_the_index_row_declares_impl_for_a_plan(served_project) -> None:
    port, _docs_dir = served_project

    status, rows = _get(port, f"/_index/{_PROJECT}")

    assert status == 200
    plan_rows = [row for row in rows if row["type"] == "plan"]
    assert plan_rows
    # The figure rides the row the first paint reads, so the bar draws without
    # waiting for discovery.
    assert all(row.get("impl") is not None for row in plan_rows)
    assert "impl" in metadata_index.ROW_FIELDS
