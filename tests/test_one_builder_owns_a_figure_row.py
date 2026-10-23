"""One public builder owns a figure's row.

The metadata index and the Figures inventory used to derive a figure's slug,
kind, title, dimensions and href in two places, so a change to how a title is
derived had to be made twice and the two readers could already disagree.
``reckon.figures.figure_row`` is now the single builder: the inventory reads it,
and the index reads it and adds only what the index alone carries — the file's
stamps, its revision, and the empty lifecycle fields — projected onto the index's
row fields.

Each case carries a control applied as a mutation, reverted after its run; the
mutations and their failing test ids are recorded in the node's negative-control
log:

1. the title is derived differently inside the index's call path only, and the
   agreement case fails; the index's projection drops ``width`` and ``height``,
   and the dimension case fails;
2. the revision is dropped from the index row, and the revision case fails;
3. the index builds its row from a private copy of the derivation, and the
   index row keeps the real title while the inventory carries the marker, so
   the marker case fails.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from reckon import figures, metadata_index

_PROJECT = "sample"

#: The keys an index figure row serves today. A figure row carries no ``impl``;
#: that field belongs to plans.
_FIGURE_ROW_KEYS = {
    "slug",
    "href",
    "type",
    "title",
    "status",
    "sprint",
    "archived",
    "created",
    "edited",
    "width",
    "height",
    "revision",
}

_CAPTION = "Control layout at reading width"
_MARKER = "marker title"


def _revision_of(path: Path) -> str:
    """The formula the server addresses a file's bytes by.

    Computed here rather than by calling the servant's own helper, so the
    assertion fails if the two ever disagree.
    """

    stat = os.stat(path)
    return f"{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}"


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def _gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A temporary config home and no cached build in or out of a test."""

    docs_dir = tmp_path / "repository" / "docs"
    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    metadata_index.clear()
    yield
    metadata_index.clear()


@pytest.fixture()
def docs(tmp_path) -> Path:
    """A docs tree with a PNG, a fractional SVG, a GIF and a captioned figure."""

    work = tmp_path / "repository" / "docs" / "figures" / "work"
    work.mkdir(parents=True)
    (work / "still.png").write_bytes(_png_bytes(320, 200))
    (work / "anim.gif").write_bytes(_gif_bytes(640, 480))
    # A viewBox is a coordinate system, not a pixel count, so a fractional
    # dimension must survive the row unrounded.
    (work / "wide.svg").write_text('<svg viewBox="0 0 1400.5 900.25"><rect/></svg>')
    (work / "captured.png").write_bytes(_png_bytes(100, 50))
    (work / "capture-index.json").write_text(
        json.dumps(
            {
                "captures": [
                    {
                        "capture": "control-layout",
                        "image": "captured.png",
                        "description": _CAPTION,
                    }
                ]
            }
        )
    )
    return tmp_path / "repository" / "docs"


def _index_figure_rows(docs_dir: Path) -> dict[str, dict]:
    rows = metadata_index.index_rows(docs_dir, _PROJECT)
    return {row["slug"]: row for row in rows if row.get("type") in {"figure", "gif"}}


def _inventory_rows(docs_dir: Path) -> dict[str, dict]:
    return {row["slug"]: row for row in figures.figure_rows(docs_dir, _PROJECT, set())}


def test_both_readers_agree_on_every_field_they_both_carry(docs: Path) -> None:
    """The index row and the inventory row describe one figure identically."""

    index_rows = _index_figure_rows(docs)
    inventory_rows = _inventory_rows(docs)

    assert set(index_rows) == set(inventory_rows), index_rows.keys()
    assert {
        "work/still.png",
        "work/wide.svg",
        "work/anim.gif",
        "work/captured.png",
    } == set(index_rows)

    # The fixture's SVG carries fractional dimensions, so a rounded value shows.
    dimensions = {
        "work/still.png": (320, 200),
        "work/wide.svg": (1400.5, 900.25),
        "work/anim.gif": (640, 480),
        "work/captured.png": (100, 50),
    }
    rendered = {
        "work/still.png": "320 \u00d7 200",
        "work/wide.svg": "1400.5 \u00d7 900.25",
        "work/anim.gif": "640 \u00d7 480",
        "work/captured.png": "100 \u00d7 50",
    }

    for slug, index_row in index_rows.items():
        inventory_row = inventory_rows[slug]
        assert set(index_row) == _FIGURE_ROW_KEYS, (slug, set(index_row))
        for field in ("slug", "type", "title", "href"):
            assert index_row[field] == inventory_row[field], (slug, field)
        width, height = dimensions[slug]
        assert index_row["width"] == width, (slug, index_row["width"])
        assert index_row["height"] == height, (slug, index_row["height"])
        assert inventory_row["dims"] == rendered[slug], (slug, inventory_row["dims"])


def test_the_captured_figure_is_titled_from_its_capture(docs: Path) -> None:
    """The captured figure's title comes from the capture name, not its filename."""

    index_row = _index_figure_rows(docs)["work/captured.png"]
    inventory_row = _inventory_rows(docs)["work/captured.png"]

    # The capture name differs from the file stem, so a title derived from the
    # filename instead of the capture would show here.
    assert index_row["title"] == "control layout"
    assert inventory_row["title"] == "control layout"
    assert inventory_row["title"] != "captured"
    assert inventory_row["caption"] == _CAPTION


def test_the_index_row_carries_the_revision_its_first_paint_uses(docs: Path) -> None:
    """Every index figure row carries the token that addresses the file's bytes."""

    index_rows = _index_figure_rows(docs)

    assert index_rows
    for slug, row in index_rows.items():
        path = docs / "figures" / slug
        assert row["revision"] == _revision_of(path), slug
        assert row["revision"] != "", slug


def test_one_builder_feeds_both_readers(docs: Path, monkeypatch) -> None:
    """Patching the public builder changes the title in the index and inventory."""

    original = figures.figure_row

    def marked(path, docs_dir, project, plan_slugs=None):
        row = original(path, docs_dir, project, plan_slugs)
        if row is not None:
            row["title"] = _MARKER
        return row

    monkeypatch.setattr(figures, "figure_row", marked)
    metadata_index.clear()

    index_rows = _index_figure_rows(docs)
    inventory_rows = _inventory_rows(docs)

    assert index_rows, "the index served no figure rows"
    assert set(index_rows) == set(inventory_rows)
    for slug in index_rows:
        assert index_rows[slug]["title"] == _MARKER, slug
        assert inventory_rows[slug]["title"] == _MARKER, slug
