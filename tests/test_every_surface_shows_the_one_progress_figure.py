"""One project, one figure: every served surface answers the same progress.

The plan list, the project index and the discovery payload carry a plan's
effort-weighted progress figure; a graph endpoint and the fleet summary carry
a roll-up of them. This
file serves one temporary project and reads the figure back through every
served payload — the index row (``/_index``), the discovery row
(``/_discover``), the graph endpoint and the schedule bar the discovery payload
carries, and the fleet summary (``/_projects/index.json``) — and asserts each
one equals a reference computed here from the section's own progress and
roll-up functions (``derive_impl_imputing_effort`` and
``derive_fleet_impl_imputing_effort``). A surface that invents its own figure
diverges from the reference and fails.

The negative control restores the shipped-over-total endpoint figure: making
the endpoint report shipped/total instead of the roll-up makes the endpoint
assertion fail, which is the divergence this file exists to catch.
"""

from __future__ import annotations

import html as html_module
import http.client
import json
import subprocess
import threading
from pathlib import Path

import pytest

from reckon import serve
from reckon._plan_html import (
    derive_fleet_impl_imputing_effort,
    derive_impl_imputing_effort,
    parse_meta,
    read_state_and_text_file,
)

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs" / "ui" / "graph.jsx"
CREW = ROOT / "docs" / "ui" / "crew.jsx"

RECORD = (
    '<section data-reckon="section" data-id="{sid}" data-effort-hours="{hours}"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard" data-capability-verification="strict"'
    ' data-capability-risk="low" data-attempts="0" data-status="{status}"'
    ' data-links=""></section>'
)


def _plan_text(
    slug: str,
    *,
    status: str,
    effort_hours: float,
    declarations: dict[str, str],
    records: list[tuple[str, float, str]],
    depends_on: str | None = None,
) -> str:
    body = []
    for sid, hours, record_status in records:
        body.append(f'<h2 id="{sid}">The {sid} work</h2>')
        body.append(RECORD.format(sid=sid, hours=hours, status=record_status))
    head = [
        '<!doctype html>\n<html lang="en"><head>\n',
        '<meta charset="utf-8">\n',
        '<meta name="docs-project" content="sample">\n',
        '<meta name="reckon-type" content="plan">\n',
        f'<meta name="plan-slug" content="{slug}">\n',
        f'<meta name="plan-title" content="{slug}">\n',
        f'<meta name="plan-status" content="{status}">\n',
        f'<meta name="plan-effort-hours" content="{effort_hours}">\n',
        '<meta name="plan-wall-clock-hours" content="4">\n',
    ]
    if depends_on:
        head.append(f'<meta name="plan-depends-on" content="{depends_on}">\n')
    escaped = html_module.escape(json.dumps(declarations), quote=True)
    head.append(f'<meta name="plan-section-declarations" content="{escaped}">\n')
    return (
        "".join(head)
        + "</head><body><main>\n"
        + "\n".join(body)
        + "\n</main></body></html>\n"
    )


def _write_project(root: Path) -> None:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    # foundation is shipped; capped carries 9 h of work it has not started. The
    # two figures differ enough that shipped-over-total (1/2) cannot be mistaken
    # for the effort-weighted roll-up (1/10).
    (plans / "foundation.html").write_text(
        _plan_text(
            "foundation",
            status="shipped",
            effort_hours=1,
            declarations={"s1": "done"},
            records=[("s1", 1, "done")],
        ),
        encoding="utf-8",
    )
    (plans / "capped.html").write_text(
        _plan_text(
            "capped",
            status="pending",
            effort_hours=9,
            declarations={"s1": "implementable"},
            records=[("s1", 9, "implementable")],
            depends_on="foundation",
        ),
        encoding="utf-8",
    )
    git = [
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
    ]
    subprocess.run([*git, "init", "-q"], check=True, capture_output=True, text=True)
    subprocess.run(
        [*git, "add", "docs/plans"], check=True, capture_output=True, text=True
    )
    subprocess.run(
        [*git, "commit", "-q", "-m", "fixture"],
        check=True,
        capture_output=True,
        text=True,
    )


def _plan_reference(path: Path) -> float:
    """A plan's figure from the section's own function, with its authored fallback.

    The reference is computed here from the plan file — never read from the
    payload under test — so a surface that computes its own figure is compared
    against the section's rule rather than against itself.
    """
    state, _text = read_state_and_text_file(path)
    derived = derive_impl_imputing_effort(
        state.get("sections"), state.get("section_declarations")
    )
    if derived is not None:
        return derived
    return float(parse_meta(path).get("impl") or 0.0)


def _get(port: int, path: str):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture()
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    _write_project(repo)
    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({"sample": str(repo / "docs")}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", config_home / "state")
    monkeypatch.setattr(serve.crew, "list_live", lambda **_kwargs: [])
    serve._DISC_CACHE.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "docs": repo / "docs",
            "port": server.server_port,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve._DISC_CACHE.clear()


def _read_surfaces(port: int) -> tuple[list[dict], dict, dict]:
    index_status, index_rows = _get(port, "/_index/sample")
    discovery_status, discovery = _get(port, "/_discover/sample")
    fleet_status, fleet = _get(port, "/_projects/index.json")
    assert index_status == 200, index_rows
    assert discovery_status == 200, discovery
    assert fleet_status == 200, fleet
    return index_rows, discovery, fleet


def test_every_served_surface_answers_the_one_figure(served) -> None:
    docs = served["docs"]
    reference = {
        slug: _plan_reference(docs / "plans" / f"{slug}.html")
        for slug in ("foundation", "capped")
    }
    # foundation lands 1 h of 1, capped 0 h of 9: the roll-up is 1/10, while
    # shipped-over-total reads 1/2. The two cannot be confused.
    rollup_reference = derive_fleet_impl_imputing_effort(
        [
            {"impl": reference["foundation"], "effort_hours": 1.0},
            {"impl": reference["capped"], "effort_hours": 9.0},
        ]
    )
    assert rollup_reference == pytest.approx(0.1)

    index_rows, discovery, fleet = _read_surfaces(served["port"])

    # ── the index row ───────────────────────────────────────────────────────
    index_by_slug = {
        row["slug"]: row for row in index_rows if row.get("type") == "plan"
    }
    for slug, figure in reference.items():
        assert index_by_slug[slug]["impl"] == pytest.approx(figure), slug

    # ── the discovery row ───────────────────────────────────────────────────
    discovery_by_slug = {
        row["slug"]: row for row in discovery["inventory"] if row.get("type") == "plan"
    }
    for slug, figure in reference.items():
        assert discovery_by_slug[slug]["impl"] == pytest.approx(figure), slug

    # The index and the discovery row are the same figure, from the one rule.
    for slug in reference:
        assert index_by_slug[slug]["impl"] == pytest.approx(
            discovery_by_slug[slug]["impl"]
        ), slug

    # ── the graph endpoint ──────────────────────────────────────────────────
    endpoints = discovery["endpoints"]
    endpoint = next(row for row in endpoints if row["slug"] == "capped")
    assert {member["slug"] for member in endpoint["members"]} == {
        "foundation",
        "capped",
    }
    assert endpoint["progress"] == pytest.approx(rollup_reference)
    # The counts underneath stay counts.
    assert endpoint["completion"] == {"shipped": 1, "total": 2}

    # ── the schedule bar ────────────────────────────────────────────────────
    schedule = discovery["schedule"]
    assert schedule is not None
    bars = {bar["plan"]: bar for bar in schedule["bars"]}
    assert set(bars) == {"foundation", "capped"}
    for bar in schedule["bars"]:
        assert "slug" not in bar, "the bar names its plan once, under `plan`"
        assert "decisions" not in json.dumps(bar)
    # The bar's slug resolves against the inventory to the one figure.
    for slug, bar in bars.items():
        assert bar["plan"] in discovery_by_slug
        assert discovery_by_slug[bar["plan"]]["impl"] == pytest.approx(reference[slug])

    # ── the fleet summary ───────────────────────────────────────────────────
    project_row = next(row for row in fleet["projects"] if row["project"] == "sample")[
        "data"
    ]["projects"][0]
    assert project_row["progress"] == pytest.approx(rollup_reference)


def test_the_surfaces_state_the_figure_and_never_derive_it() -> None:
    """The SPA renders a served figure; it computes none of its own.

    The graph renders the endpoint's served ``progress``; the crew flow bar
    renders the plan's served ``impl``. Neither divides shipped by total.
    """

    graph = GRAPH.read_text(encoding="utf-8")
    crew = CREW.read_text(encoding="utf-8")
    assert "endpoint.progress" in graph
    assert "endpoint.shipped_fraction" not in graph
    assert "(shipped / total) * 100" not in graph
    assert "plan.impl" in crew
