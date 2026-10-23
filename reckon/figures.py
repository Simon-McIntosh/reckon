"""Figure inventory rows: PNG/SVG/GIF discovery with dimensions read from the file.

No imaging dependency: a PNG's width and height are the two big-endian 32-bit
integers following its 8-byte signature and IHDR chunk header; an SVG's come
from its ``viewBox`` or ``width``/``height`` attributes; a GIF's are the two
little-endian unsigned shorts following its 6-byte ``GIF87a``/``GIF89a``
signature.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

_SEPARATORS = re.compile(r"[-_]+")

_SVG_VIEWBOX_RE = re.compile(
    r'viewBox\s*=\s*"[^"]*?\s([0-9.]+)\s+([0-9.]+)\s*"', re.IGNORECASE
)
_SVG_WIDTH_RE = re.compile(r'\bwidth\s*=\s*"([0-9.]+)[a-zA-Z%]*"')
_SVG_HEIGHT_RE = re.compile(r'\bheight\s*=\s*"([0-9.]+)[a-zA-Z%]*"')


def rasterise_svg(source: Path, width: int) -> bytes:
    """Draw an SVG as a PNG at the requested width, preserving its aspect ratio."""

    import resvg_py

    size = _svg_dims(source)
    height = (
        max(1, round(width * size[1] / size[0])) if size and min(size) > 0 else width
    )
    # Matplotlib writes canvas dimensions in points. resvg needs a DPI to
    # resolve those units; 96 is the CSS reference resolution.
    return resvg_py.svg_to_bytes(
        svg_string=source.read_text(encoding="utf-8", errors="replace"),
        width=width,
        height=height,
        dpi=96,
    )


def _titleize(name: str) -> str:
    words = _SEPARATORS.sub(" ", name).split()
    return " ".join(words)


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _format_dims(width: float, height: float) -> str:
    return f"{_format_number(width)} \u00d7 {_format_number(height)}"


def _png_dims(path: Path) -> tuple[int, int] | None:
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None
    if (
        len(header) < 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        return None
    width, height = struct.unpack(">II", header[16:24])
    return width, height


_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")


def _gif_dims(path: Path) -> tuple[int, int] | None:
    try:
        header = path.read_bytes()[:10]
    except OSError:
        return None
    if len(header) < 10 or header[:6] not in _GIF_SIGNATURES:
        return None
    width, height = struct.unpack("<HH", header[6:10])
    return width, height


def _svg_dims(path: Path) -> tuple[float, float] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _SVG_VIEWBOX_RE.search(text)
    if match:
        return float(match.group(1)), float(match.group(2))
    width_match = _SVG_WIDTH_RE.search(text)
    height_match = _SVG_HEIGHT_RE.search(text)
    if width_match and height_match:
        return float(width_match.group(1)), float(height_match.group(1))
    return None


def _capture_metadata(path: Path) -> tuple[str | None, str]:
    """Return the sidecar capture name and index description for this file."""

    capture_name = None
    geometry = path.parent / f"{path.stem}.geometry.json"
    if geometry.is_file():
        try:
            data = json.loads(geometry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("capture"):
            capture_name = str(data["capture"])

    index = path.parent / "capture-index.json"
    if index.is_file():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for capture in data.get("captures", []):
                if isinstance(capture, dict) and capture.get("image") == path.name:
                    if capture.get("capture"):
                        capture_name = str(capture["capture"])
                    description = capture.get("description")
                    caption = "" if description is None else str(description).strip()
                    return capture_name, caption
    return capture_name, ""


_DIMS_BY_SUFFIX = {".png": _png_dims, ".svg": _svg_dims, ".gif": _gif_dims}


def figure_row(
    path: Path,
    docs_dir: Path,
    project: str,
    plan_slugs: set[str] | None = None,
) -> dict | None:
    """Return one inventory row for a figure file under ``docs_dir/figures``.

    This is the single builder of a figure's row: the Figures inventory and the
    metadata index both take their fields from here, so a title, a kind or a
    dimension is derived once. ``width`` and ``height`` are the numbers the
    format's reader returns, unrounded, and ``width`` is ``None`` when the file
    cannot be read. ``plan_slugs`` names the plans already discovered in this
    project; a row's ``for_plan`` is its top-level directory when that directory
    is itself a plan slug, and empty otherwise. A path outside the figures
    directory yields no row.
    """

    try:
        slug = path.relative_to(docs_dir / "figures").as_posix()
    except ValueError:
        return None
    # Each format has its own reader; a GIF must not fall through to the SVG
    # reader, which would read a binary file as text and return nothing.
    dims = _DIMS_BY_SUFFIX[path.suffix](path)
    capture, caption = _capture_metadata(path)
    title = _titleize(capture) if capture else _titleize(path.stem)
    first_segment = slug.split("/", 1)[0]
    # download names the fetchable file a reader can offer to save, and it
    # equals href here for every figure and gif row because the inventory is
    # built by walking this directory, so each row is a file on disk by
    # construction. It is not therefore redundant: href answers where the
    # artifact is addressed, download answers whether that address is a file
    # a reader can save, and the two coincide for these kinds and diverge
    # for kinds produced elsewhere.
    href = f"/{project}/figures/{slug}"
    return {
        "slug": slug,
        "type": "gif" if path.suffix == ".gif" else "figure",
        "title": title,
        "caption": caption,
        "for_plan": first_segment if first_segment in (plan_slugs or ()) else "",
        "href": href,
        "download": href,
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
        "path": path,
    }


def figure_rows(
    docs_dir: Path,
    project: str,
    plan_slugs: set[str],
    *,
    covered_files: list[tuple[str, Path]] | None = None,
) -> list[dict]:
    """Return one inventory row per PNG/SVG/GIF under ``docs_dir/figures``.

    A PNG or an SVG yields a row whose kind is ``figure``; a GIF's row is
    identical in every other field but carries kind ``gif``, so a surface can
    register a separate tab beside Figures rather than interleaving
    animations into the figures index. ``plan_slugs`` names the plans already
    discovered in this project; a row's ``for_plan`` is its top-level
    directory when that directory is itself a plan slug, and empty otherwise.
    """

    figures_dir = docs_dir / "figures"
    if covered_files is None:
        if not figures_dir.is_dir():
            return []
        paths = [
            *figures_dir.rglob("*.png"),
            *figures_dir.rglob("*.svg"),
            *figures_dir.rglob("*.gif"),
        ]
    else:
        paths = [
            path
            for relative, path in covered_files
            if relative.startswith("figures/") and path.suffix in _DIMS_BY_SUFFIX
        ]
    paths.sort(key=lambda p: p.relative_to(figures_dir).as_posix())

    rows = []
    for path in paths:
        row = figure_row(path, docs_dir, project, plan_slugs)
        if row is None:
            continue
        # The inventory renders one dims string from the builder's numeric
        # width and height, and carries neither number itself, so its row keeps
        # the shape it served before the builder existed.
        width, height = row["width"], row["height"]
        rows.append(
            {
                "slug": row["slug"],
                "type": row["type"],
                "title": row["title"],
                "caption": row["caption"],
                "dims": _format_dims(width, height) if width is not None else "",
                "for_plan": row["for_plan"],
                "href": row["href"],
                "download": row["download"],
                "path": row["path"],
            }
        )
    return rows
