"""GIF inventory rows are their own kind with real dimensions.

A GIF under ``docs/figures`` must emit a row whose kind is ``gif`` — not
``figure`` — so a surface can build a GIF tab beside Figures. The row carries
the dimensions read from the GIF header, a PNG and an SVG keep their figure
rows unchanged, and adding a GIF invalidates the discovery signature.
"""

from __future__ import annotations

import struct
from pathlib import Path

from reckon import serve
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


def _gif_bytes(width: int, height: int, signature: bytes = b"GIF89a") -> bytes:
    return signature + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _figures_tree(tmp_path: Path) -> Path:
    """Create a figures tree with one GIF and return the docs dir."""
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "anim.gif").write_bytes(_gif_bytes(640, 480))
    return tmp_path / "docs"


def test_gif_yields_a_row_of_its_own_kind(tmp_path: Path) -> None:
    docs = _figures_tree(tmp_path)

    rows = figure_rows(docs, "sample", set())

    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "work/anim.gif"
    assert row["type"] == "gif"
    assert [r for r in rows if r["type"] == "figure"] == []


def test_png_and_svg_still_yield_figure_rows(tmp_path: Path) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "still.png").write_bytes(_png_bytes(320, 200))
    (figures / "still.svg").write_text(_svg_text(300, 150))

    rows = figure_rows(tmp_path / "docs", "sample", set())

    kinds = {row["slug"]: row["type"] for row in rows}
    assert kinds == {
        "work/still.png": "figure",
        "work/still.svg": "figure",
    }


def test_gif_row_and_figure_row_differ_only_in_kind(tmp_path: Path) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "shape.gif").write_bytes(_gif_bytes(640, 480))
    (figures / "shape.png").write_bytes(_png_bytes(640, 480))

    rows = figure_rows(tmp_path / "docs", "sample", set())
    gif = next(r for r in rows if r["type"] == "gif")
    fig = next(r for r in rows if r["type"] == "figure")

    assert set(gif) == set(fig)
    # File-identity fields (slug, href, path) differ by filename by design; every
    # shared content field must match, so the two rows differ only in kind.
    excluded = {"type", "slug", "href", "path"}
    shared = {k: v for k, v in gif.items() if k not in excluded}
    shared_fig = {k: v for k, v in fig.items() if k not in excluded}
    assert shared == shared_fig


def test_gif_row_carries_non_square_header_dimensions(tmp_path: Path) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    # 640 x 480 is not square, so a transposed unpack would read 480 x 640.
    (figures / "wide.gif").write_bytes(_gif_bytes(640, 480))

    rows = figure_rows(tmp_path / "docs", "sample", set())

    assert rows[0]["dims"] == "640 \u00d7 480"


def test_both_gif_signature_forms_are_read(tmp_path: Path) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "old.gif").write_bytes(_gif_bytes(320, 240, b"GIF87a"))
    (figures / "new.gif").write_bytes(_gif_bytes(64, 64, b"GIF89a"))

    rows = figure_rows(tmp_path / "docs", "sample", set())

    dims = {row["slug"]: row["dims"] for row in rows}
    assert dims["work/old.gif"] == "320 \u00d7 240"
    assert dims["work/new.gif"] == "64 \u00d7 64"


def test_gif_with_wrong_signature_reports_no_dimensions_without_raising(
    tmp_path: Path,
) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "fake.gif").write_bytes(b"NOTGIF" + struct.pack("<HH", 640, 480))

    rows = figure_rows(tmp_path / "docs", "sample", set())

    assert rows[0]["slug"] == "work/fake.gif"
    assert rows[0]["dims"] == ""


def test_gif_truncated_inside_its_header_reports_no_dimensions_without_raising(
    tmp_path: Path,
) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    # Only the signature and the width survive; the height short is missing.
    (figures / "cut.gif").write_bytes(b"GIF89a" + struct.pack("<H", 640))

    rows = figure_rows(tmp_path / "docs", "sample", set())

    assert rows[0]["slug"] == "work/cut.gif"
    assert rows[0]["dims"] == ""


def test_discovery_signature_changes_when_a_gif_is_added(tmp_path: Path) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "still.png").write_bytes(_png_bytes(320, 200))
    docs = tmp_path / "docs"

    before = serve._discovery_signature(docs, "sample", None)
    steady = serve._discovery_signature(docs, "sample", None)
    assert before == steady, "signature moved with nothing changed"

    (figures / "anim.gif").write_bytes(_gif_bytes(640, 480))
    after = serve._discovery_signature(docs, "sample", None)

    assert after != before, "adding a GIF left the discovery signature alone"


def test_mixed_tree_yields_exactly_one_gif_kind(tmp_path: Path) -> None:
    figures = tmp_path / "docs" / "figures" / "work"
    figures.mkdir(parents=True)
    (figures / "still.png").write_bytes(_png_bytes(320, 200))
    (figures / "still.svg").write_text(_svg_text(300, 150))
    (figures / "anim.gif").write_bytes(_gif_bytes(640, 480))

    rows = figure_rows(tmp_path / "docs", "sample", set())

    assert len(rows) == 3
    assert sum(1 for r in rows if r["type"] == "gif") == 1
    assert sorted(r["type"] for r in rows) == ["figure", "figure", "gif"]
