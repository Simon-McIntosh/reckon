"""Persisting a report item copies it out and records what was kept.

The cases run against a synthetic crew home and a temporary persist root, and
read the operator's real crew home before and after so a copy or a record that
leaked out of the temporary home fails the matrix rather than colliding with a
live fleet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon.crew import persist
from reckon.crew.persist import PersistError, persist_item, read_persisted
from tests.conftest import real_store_home, real_store_is_not_a_fixture_target

PROJECT = "persist-probe-project"
PLAN = "a-persist-probe-plan"
OTHER_PLAN = "another-persist-probe-plan"
SECTION = "s2"
PERSISTED_BY = "persist-probe-operator"
NEEDLE = "persist-probe"


def _plan_doc(docs: Path, slug: str, *, project: str = PROJECT) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        '<meta name="plan-status" content="active">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    return path


@pytest.fixture()
def store(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A mounted synthetic project with its own flight layer and persist root."""
    docs = tmp_path / "sample-repo" / "docs"
    docs.mkdir(parents=True)
    (docs / ".reckon").mkdir()
    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps({"status": "complete", "format": "distributed"})
    )
    _plan_doc(docs, PLAN)
    _plan_doc(docs, OTHER_PLAN)

    persist_root = tmp_path / "work" / "persist"
    flight_yaml = docs / "state" / PROJECT / "flight.yaml"
    flight_yaml.parent.mkdir(parents=True)
    flight_yaml.write_text(f"reports:\n  persist_root: {persist_root}\n")

    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))

    reports = home / "crew" / "reports" / PROJECT
    item = reports / PLAN / "run-1"
    item.mkdir(parents=True)
    figure = item / "trace.svg"
    figure.write_text("<svg>trace</svg>\n")
    first_data = item / "trace.nc"
    first_data.write_text("data-one\n")
    second_data = item / "trace.h5"
    second_data.write_text("data-two\n")
    return {
        "home": home,
        "docs": docs,
        "persist_root": persist_root,
        "figure": figure,
        "data": [first_data, second_data],
    }


def real_store_traces_now() -> list[str]:
    """Names in the operator's own crew store that this node could have written."""
    crew = real_store_home() / "crew"
    hits: list[str] = []
    for subtree in ("reports", "runs"):
        root = crew / subtree
        if not root.is_dir():
            continue
        hits.extend(str(path) for path in root.iterdir() if NEEDLE in path.name)
    return hits


@pytest.fixture(autouse=True)
def real_crew_home_untouched(home: Path) -> Iterator[None]:
    """The operator's own crew home holds no trace of this node, before or after."""
    real_store_is_not_a_fixture_target(home)
    before = real_store_traces_now()
    yield
    assert real_store_traces_now() == before == []


def _resource(store: dict) -> Path:
    return store["docs"] / "state" / PROJECT / "persisted-items.html"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_persist_writes_one_verified_copy_and_one_record(store):
    records = persist_item(
        store["figure"],
        project=PROJECT,
        section=SECTION,
        persisted_by=PERSISTED_BY,
    )

    assert len(records) == 1
    record = records[0]
    copy = Path(record["persisted"])
    assert copy.is_file()
    assert copy.parent.is_relative_to(store["persist_root"])
    assert record["sha256"] == _sha256(store["figure"]) == _sha256(copy)
    assert record["bytes"] == store["figure"].stat().st_size
    assert record["kind"] == "figure"
    assert record["figure"] is None
    assert record["plan"] == PLAN
    assert record["section"] == SECTION
    assert record["persisted_by"] == PERSISTED_BY
    assert record["persisted_at"]
    assert read_persisted(PROJECT) == [record]


def test_a_figure_with_two_data_files_records_all_three(store):
    records = persist_item(
        store["figure"],
        store["data"],
        project=PROJECT,
        section=SECTION,
        persisted_by=PERSISTED_BY,
    )

    assert len(records) == 3
    assert [record["kind"] for record in records] == ["figure", "data", "data"]
    figure_record = records[0]
    for data_record, source in zip(records[1:], store["data"], strict=True):
        assert data_record["figure"] == figure_record["source"]
        assert Path(data_record["persisted"]).is_file()
        assert data_record["sha256"] == _sha256(source)
    assert len(read_persisted(PROJECT)) == 3


def test_a_project_with_no_persist_root_cannot_persist(store):
    flight_yaml = store["docs"] / "state" / PROJECT / "flight.yaml"
    flight_yaml.write_text("reports:\n  staging_root: /work/staging\n")

    with pytest.raises(PersistError) as refused:
        persist_item(store["figure"], project=PROJECT)

    assert "reports.persist_root" in str(refused.value)
    assert not _resource(store).exists()
    assert not store["persist_root"].exists()


def test_an_unmounted_project_cannot_persist(store):
    with pytest.raises(PersistError) as refused:
        persist_item(store["figure"], project="persist-probe-unmounted")

    assert "not mounted" in str(refused.value)
    assert not _resource(store).exists()
    assert not store["persist_root"].exists()


def test_a_repeat_persist_writes_no_second_record(store):
    first = persist_item(store["figure"], project=PROJECT, persisted_by=PERSISTED_BY)
    resource = _resource(store)
    settled = resource.read_bytes()

    second = persist_item(store["figure"], project=PROJECT, persisted_by=PERSISTED_BY)

    assert second == first
    assert resource.read_bytes() == settled
    assert len(read_persisted(PROJECT)) == 1


def test_a_corrupt_copy_is_refused_and_leaves_no_record(
    store, monkeypatch: pytest.MonkeyPatch
):
    real_copy = persist._copy_bytes

    def copy_then_alter(source: Path, destination: Path) -> None:
        real_copy(source, destination)
        # Same byte count as the source, so only the content hash can catch it.
        content = bytearray(destination.read_bytes())
        content[0] = ord("X")
        destination.write_bytes(bytes(content))

    monkeypatch.setattr(persist, "_copy_bytes", copy_then_alter)

    with pytest.raises(PersistError) as refused:
        persist_item(store["figure"], project=PROJECT, persisted_by=PERSISTED_BY)

    assert "hashes" in str(refused.value)
    assert not _resource(store).exists()
    assert read_persisted(PROJECT) == []
    assert [path for path in store["persist_root"].rglob("*") if path.is_file()] == []


def test_reads_filter_by_plan_and_kind(store):
    persist_item(
        store["figure"],
        store["data"][:1],
        project=PROJECT,
        section=SECTION,
        persisted_by=PERSISTED_BY,
    )
    other = store["figure"].parent / "other.svg"
    other.write_text("<svg>other</svg>\n")
    persist_item(
        other,
        project=PROJECT,
        plan=OTHER_PLAN,
        section="s1",
        persisted_by=PERSISTED_BY,
    )

    assert [record["plan"] for record in read_persisted(PROJECT, plan=OTHER_PLAN)] == [
        OTHER_PLAN
    ]
    assert len(read_persisted(PROJECT, plan=PLAN)) == 2
    assert len(read_persisted(PROJECT, kind="figure")) == 2
    assert len(read_persisted(PROJECT, kind="data")) == 1
    assert all(
        record["persisted_by"] == PERSISTED_BY for record in read_persisted(PROJECT)
    )
