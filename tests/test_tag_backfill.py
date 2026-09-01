from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon._plan_html import read_state, write_state
from reckon.cli import main
from reckon.tags import backfill_tags_from_preimage


def _resource(path: Path, resource_type: str, slug: str, tags: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    state: dict[str, object] = {
        "type": resource_type,
        "slug": slug,
        "title": slug,
        "tags": tags,
    }
    if resource_type == "plan":
        state["status"] = "active"
    path.write_text(write_state(bare, state), encoding="utf-8")


def test_backfill_preserves_every_group_from_synthetic_move_preimage(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "corpus" / "docs"
    compose = docs / "research" / "Standard_Names" / "compose.html"
    overview = docs / "archive" / "research" / "Standard_Names" / "standard-names.html"
    control = docs / "plans" / "Plasma_Control" / "control.html"
    flat = docs / "delivery.html"
    _resource(compose, "research", "compose", ["language-models"])
    _resource(overview, "research", "standard-names", [])
    _resource(control, "plan", "control", ["operations"])
    _resource(flat, "plan", "delivery", [])

    preimage = tmp_path / "layout-moves-preimage.txt"
    preimage.write_text(
        "\n".join(
            [
                "  research/Standard_Names/compose.html -> research/compose.html",
                "  archive/research/Standard_Names/standard-names.html -> research/archive/standard-names.html",
                "  plans/Plasma_Control/control.html -> plans/control.html",
                "  delivery.html -> plans/delivery.html",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = backfill_tags_from_preimage(docs, preimage)

    expected_by_source = {
        "research/Standard_Names/compose.html": "standard-names",
        "archive/research/Standard_Names/standard-names.html": "standard-names",
        "plans/Plasma_Control/control.html": "plasma-control",
    }
    derived_by_source = {item["from"]: item["tag"] for item in report["resources"]}
    assert derived_by_source == expected_by_source
    assert report == {
        "moves": 4,
        "grouped_resources": 3,
        "changed": 3,
        "unchanged": 0,
        "grouping_loss": 0,
        "lost_resources": [],
        "resources": report["resources"],
    }

    assert read_state(compose.read_text(encoding="utf-8"))["tags"] == [
        "language-models",
        "standard-names",
    ]
    overview_tags = read_state(overview.read_text(encoding="utf-8"))["tags"]
    assert overview_tags == ["standard-names"]
    assert overview_tags.count("standard-names") == 1
    assert read_state(control.read_text(encoding="utf-8"))["tags"] == [
        "operations",
        "plasma-control",
    ]
    assert read_state(flat.read_text(encoding="utf-8"))["tags"] == []

    for item in report["resources"]:
        assert item["tag"] in item["tags"]


def test_backfill_command_dry_run_reports_census_without_writing(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "corpus"
    docs = checkout / "docs"
    compose = docs / "research" / "compose.html"
    overview = docs / "research" / "archive" / "overview.html"
    control = docs / "plans" / "control.html"
    flat = docs / "plans" / "delivery.html"
    _resource(compose, "research", "compose", ["language-models"])
    _resource(overview, "research", "overview", ["standard-names"])
    _resource(control, "plan", "control", [])
    _resource(flat, "plan", "delivery", [])

    preimage = tmp_path / "layout-moves-preimage.txt"
    preimage.write_text(
        "research/Standard_Names/compose.html -> research/compose.html\n"
        "archive/research/Standard_Names/overview.html -> research/archive/overview.html\n"
        "plans/Plasma_Control/control.html -> plans/control.html\n"
        "delivery.html -> plans/delivery.html\n",
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = CliRunner().invoke(
        main,
        [
            "tag",
            "backfill",
            "--project",
            "sample",
            "--checkout-path",
            str(checkout),
            "--preimage",
            str(preimage),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report == {
        "changed": 2,
        "dry_run": True,
        "grouped_resources": 3,
        "grouping_loss": 0,
        "lost_resources": [],
        "moves": 4,
        "resources": report["resources"],
        "unchanged": 1,
        "written": 0,
    }
    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
