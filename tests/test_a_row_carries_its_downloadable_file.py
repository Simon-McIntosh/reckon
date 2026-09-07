"""Figure and GIF inventory rows carry a download field naming the saveable file.

A PNG, an SVG or a GIF under ``docs/figures`` is a file on disk by
construction, because the inventory is built by walking that directory. So
every figure row and every gif row is fetchable: the row's ``download`` is the
row's own ``href``, the reader can offer a save control keyed on one field
without enumerating kinds, and there is no second field to disagree with it.
The field is a string on every row, never ``None``, and it is additive — every
other field is unchanged from what it was before ``download`` existed. A row
whose kind is neither ``figure`` nor ``gif`` is not produced by this function
at all, which is why nothing here asserts about other kinds.
"""

from __future__ import annotations

import struct
from pathlib import Path

from reckon.figures import figure_rows


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def _svg_text(width: int, height: int) -> str:
    return f'<svg viewBox="0 0 {width} {height}"><rect/></svg>'


def _gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _mixed_figures_tree(tmp_path: Path) -> Path:
    """Create a docs tree holding one figure, one SVG and one GIF."""
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "still.png").write_bytes(_png_bytes(320, 200))
    (figures / "still.svg").write_text(_svg_text(300, 150))
    (figures / "anim.gif").write_bytes(_gif_bytes(640, 480))
    return tmp_path / "docs"


def test_figure_row_download_equals_its_href(tmp_path: Path) -> None:
    rows = figure_rows(_mixed_figures_tree(tmp_path), "sample", set())

    figure_rows_by_slug = [r for r in rows if r["type"] == "figure"]
    assert figure_rows_by_slug, "expected at least one figure row"
    for row in figure_rows_by_slug:
        # Compared field-to-field on the same row, not against a constructed
        # href string, so a divergence in either field fails this test.
        assert row["download"] == row["href"]
        assert row["download"] != ""


def test_gif_row_download_equals_its_href(tmp_path: Path) -> None:
    rows = figure_rows(_mixed_figures_tree(tmp_path), "sample", set())

    gif_rows = [r for r in rows if r["type"] == "gif"]
    assert len(gif_rows) == 1
    for row in gif_rows:
        assert row["download"] == row["href"]
        assert row["download"] != ""


def test_download_is_a_non_empty_string_on_every_row(tmp_path: Path) -> None:
    rows = figure_rows(_mixed_figures_tree(tmp_path), "sample", set())

    assert rows
    for row in rows:
        assert isinstance(row["download"], str), (
            "download must be a string so a reader sees one falsy shape "
            f"for the absent case; got {type(row['download']).__name__}"
        )
        assert row["download"], "every figure and gif row names a fetchable file"


def test_both_kinds_meet_on_the_same_download_contract(tmp_path: Path) -> None:
    rows = figure_rows(_mixed_figures_tree(tmp_path), "sample", set())

    gif = next(r for r in rows if r["type"] == "gif")
    fig = next(r for r in rows if r["type"] == "figure")
    # The two kinds name their own files, so the downloads differ; what must
    # not differ is that each equals its own href.
    assert gif["download"] != fig["download"]
    assert gif["download"] == gif["href"]
    assert fig["download"] == fig["href"]


def test_download_is_additive_every_other_field_is_unchanged(
    tmp_path: Path,
) -> None:
    docs = _mixed_figures_tree(tmp_path)
    rows = figure_rows(docs, "sample", set())

    # The rows as the previous revision produced them, pinned field by field.
    # download is the only addition; each of these values must be identical to
    # what the row carried before the field existed.
    pre_change_rows = {
        "work/still.png": {
            "slug": "work/still.png",
            "type": "figure",
            "title": "still",
            "caption": "",
            "dims": "320 \u00d7 200",
            "for_plan": "",
            "href": "/sample/figures/work/still.png",
            "path": docs / "figures" / "work" / "still.png",
        },
        "work/anim.gif": {
            "slug": "work/anim.gif",
            "type": "gif",
            "title": "anim",
            "caption": "",
            "dims": "640 \u00d7 480",
            "for_plan": "",
            "href": "/sample/figures/work/anim.gif",
            "path": docs / "figures" / "work" / "anim.gif",
        },
    }

    by_slug = {row["slug"]: row for row in rows}
    for slug, expected in pre_change_rows.items():
        row = by_slug[slug]
        assert set(row) == set(expected) | {"download"}, (
            f"row {slug} must gain exactly the download key"
        )
        for key, value in expected.items():
            assert row[key] == value, (
                f"row {slug}.{key} changed from {value!r} to {row[key]!r}"
            )
        assert row["download"] == row["href"]
