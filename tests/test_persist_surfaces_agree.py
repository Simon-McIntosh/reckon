"""The CLI and the crew HTTP sub-route persist through one implementation.

Both write surfaces end in ``reckon.crew.persist.persist_item``, so the same
figure persisted through either answers the same record. The cases here drive
one synthetic figure through the CLI and through the HTTP sub-route, each
against its own temporary store, and compare the records apart from the fields
a store's own root spells and the moment of the write.

The served listing of a plan's report items answers each item's retention class
from the classifier the retention sweep reads, so a listing and the sweep can
never disagree about which material is kept. The negative-control case routes
the HTTP endpoint through a second implementation that drops the data records
and shows the agreement case failing against it, so the comparison is known to
see its own subject rather than to pass on anything.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import mcp_views, serve
from reckon.cli import main
from tests.conftest import real_store_home, real_store_is_not_a_fixture_target

PROJECT = "persist-surface-project"
PLAN = "a-persist-surface-plan"
SECTION = "s3"
FIGURE_NAME = "trace.svg"
DATA_NAME = "trace.nc"
CITED_NAME = "report.html"
WORKING_NAME = "notes.txt"


def _plan_doc(docs: Path, slug: str, citation: Path) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        '<meta name="plan-status" content="active">'
        f"<title>{slug}</title></head><body>"
        f'<a href="{citation}">cited report</a>'
        "</body></html>",
        encoding="utf-8",
    )
    return path


def _build_store(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """One synthetic project: config home, docs tree, persist root, reports.

    Every environment-resolved directory points inside ``root``: the config
    home, the state root and the mounts file. The reports tree holds a
    persisted candidate, a cited item and an unreferenced one, and the plan
    document cites the cited item by its absolute path.
    """
    config = root / "config"
    config.mkdir(parents=True)
    docs = root / "repo" / "docs"
    docs.mkdir(parents=True)
    (docs / ".reckon").mkdir()
    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps({"status": "complete", "format": "distributed"})
    )

    persist_root = root / "work" / "persist"
    flight_yaml = docs / "state" / PROJECT / "flight.yaml"
    flight_yaml.parent.mkdir(parents=True)
    flight_yaml.write_text(f"reports:\n  persist_root: {persist_root}\n")

    plan_dir = config / "crew" / "reports" / PROJECT / PLAN
    persisted_item = plan_dir / "run-1"
    persisted_item.mkdir(parents=True)
    figure = persisted_item / FIGURE_NAME
    figure.write_text("<svg>trace</svg>\n", encoding="utf-8")
    data_file = persisted_item / DATA_NAME
    data_file.write_text("data-one\n", encoding="utf-8")

    cited_item = plan_dir / "run-2"
    cited_item.mkdir(parents=True)
    cited = cited_item / CITED_NAME
    cited.write_text("<html>cited</html>\n", encoding="utf-8")

    working_item = plan_dir / "run-3"
    working_item.mkdir(parents=True)
    (working_item / WORKING_NAME).write_text("notes\n", encoding="utf-8")

    _plan_doc(docs, PLAN, cited)

    mounts_file = config / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(config / "state"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", config / "state")
    serve._DISC_CACHE.clear()

    return {
        "root": root,
        "docs": docs,
        "persist_root": persist_root,
        "figure": figure,
        "data": data_file,
        "plan_dir": plan_dir,
    }


def _persist_through_cli(store: dict[str, Path]) -> list[dict]:
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "persist",
            str(store["figure"]),
            "--data",
            str(store["data"]),
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--section",
            SECTION,
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["records"]


@contextlib.contextmanager
def _served() -> Iterator[int]:
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _persist_through_http(store: dict[str, Path]) -> list[dict]:
    body = json.dumps(
        {
            "figure": str(store["figure"]),
            "data": [str(store["data"])],
            "plan": PLAN,
            "section": SECTION,
        }
    ).encode("utf-8")
    with _served() as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "POST",
                f"/crew/{PROJECT}/reports",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
        finally:
            connection.close()
    assert payload.get("ok") is True, payload
    return payload["records"]


def _store_relative(path: str | None, root: Path) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return str(candidate.relative_to(root))
    except ValueError:
        return str(candidate)


def _comparable(records: list[dict], store: dict[str, Path]) -> list[dict]:
    """Records with the stamp and the store's own root taken out.

    Two temporary stores cannot spell one absolute source, so the comparison is
    made on what the record means: its kind, the figure a data file belongs to,
    its plan and section, who persisted it, its byte count and hash, and each
    path as the store itself spells it. The one thing genuinely different
    between two writes is when each happened.
    """
    root = Path(store["root"])
    comparable: list[dict] = []
    for record in records:
        view = dict(record)
        view.pop("persisted_at", None)
        view["source"] = _store_relative(view.get("source"), root)
        view["persisted"] = _store_relative(view.get("persisted"), root)
        view["figure"] = _store_relative(view.get("figure"), root)
        comparable.append(view)
    return comparable


def _agreement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_store = _build_store(tmp_path / "cli-store", monkeypatch)
    cli_records = _persist_through_cli(cli_store)

    http_store = _build_store(tmp_path / "http-store", monkeypatch)
    http_records = _persist_through_http(http_store)

    assert len(cli_records) == 2, cli_records
    assert len(http_records) == 2, http_records
    assert _comparable(cli_records, cli_store) == _comparable(http_records, http_store)


def test_the_cli_and_the_http_sub_route_persist_the_same_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_store_is_not_a_fixture_target(tmp_path)
    real_reports = real_store_home() / "crew" / "reports" / PROJECT
    assert not real_reports.exists()

    _agreement(tmp_path, monkeypatch)

    assert not real_reports.exists(), "a persist escaped into the real crew home"


def test_the_http_write_copies_the_figure_and_its_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _build_store(tmp_path / "http-copy", monkeypatch)
    records = _persist_through_http(store)

    assert sorted(record["kind"] for record in records) == ["data", "figure"]
    for record in records:
        copied = Path(record["persisted"])
        assert copied.is_file()
        assert copied.read_bytes() == Path(record["source"]).read_bytes()
    figure_record = next(r for r in records if r["kind"] == "figure")
    data_record = next(r for r in records if r["kind"] == "data")
    assert data_record["figure"] == figure_record["source"]


def test_the_records_view_lists_a_persisted_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _build_store(tmp_path / "records-view", monkeypatch)
    records = _persist_through_cli(store)

    answer = mcp_views.serve_crew_view(
        view="records",
        project=PROJECT,
        checkout_path=str(Path(store["root"]) / "repo"),
        plan=PLAN,
    )

    assert answer.get("ok") is True, answer
    persisted = answer["persisted"]
    assert [record["sha256"] for record in persisted] == [
        record["sha256"] for record in records
    ]
    assert all(record["plan"] == PLAN for record in persisted)


def test_the_listing_classes_each_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _build_store(tmp_path / "listing", monkeypatch)
    _persist_through_cli(store)

    items = serve.crew_plan_reports(PROJECT, PLAN)
    by_name = {item["name"]: item for item in items}

    assert by_name["run-1"]["class"] == "persisted"
    assert by_name["run-1"]["expires"] is None
    assert by_name["run-2"]["class"] == "cited"
    assert by_name["run-2"]["expires"] is None
    assert by_name["run-3"]["class"] == "working"
    assert by_name["run-3"]["expires"] is not None


def test_a_second_implementation_that_drops_data_records_fails_the_agreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: the comparison sees a route that omits data."""

    def _figure_only(project: str, payload) -> dict:
        from reckon.crew import persist

        return {
            "records": persist.persist_item(
                str(payload["figure"]),
                (),
                project=project,
                plan=payload.get("plan") or None,
                section=payload.get("section") or None,
            )
        }

    monkeypatch.setattr(serve, "crew_persist_from_reports", _figure_only)

    with pytest.raises(AssertionError):
        _agreement(tmp_path, monkeypatch)
