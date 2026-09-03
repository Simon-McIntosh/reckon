"""Figure inventory rows: PNG/SVG discovery with dimensions read from the file.

No imaging dependency: a PNG's width and height are the two big-endian 32-bit
integers following its 8-byte signature and IHDR chunk header; an SVG's come
from its ``viewBox`` or ``width``/``height`` attributes.
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


def _named_capture(path: Path) -> str | None:
    """Return the capture name a sidecar declares for this file, if any."""

    geometry = path.parent / f"{path.stem}.geometry.json"
    if geometry.is_file():
        try:
            data = json.loads(geometry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("capture"):
            return str(data["capture"])

    index = path.parent / "capture-index.json"
    if index.is_file():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for capture in data.get("captures", []):
                if (
                    isinstance(capture, dict)
                    and capture.get("image") == path.name
                    and capture.get("capture")
                ):
                    return str(capture["capture"])
    return None


def figure_rows(docs_dir: Path, project: str, plan_slugs: set[str]) -> list[dict]:
    """Return one inventory row per PNG/SVG under ``docs_dir/figures``.

    ``plan_slugs`` names the plans already discovered in this project; a
    figure's ``for_plan`` is its top-level directory when that directory is
    itself a plan slug, and empty otherwise.
    """

    figures_dir = docs_dir / "figures"
    if not figures_dir.is_dir():
        return []

    paths = sorted(
        [*figures_dir.rglob("*.png"), *figures_dir.rglob("*.svg")],
        key=lambda p: p.relative_to(figures_dir).as_posix(),
    )

    rows = []
    for path in paths:
        slug = path.relative_to(figures_dir).as_posix()
        dims = _png_dims(path) if path.suffix == ".png" else _svg_dims(path)
        capture = _named_capture(path)
        title = _titleize(capture) if capture else _titleize(path.stem)
        first_segment = slug.split("/", 1)[0]
        rows.append(
            {
                "slug": slug,
                "type": "figure",
                "title": title,
                "dims": _format_dims(*dims) if dims else "",
                "for_plan": first_segment if first_segment in plan_slugs else "",
                "href": f"/{project}/figures/{slug}",
                "path": path,
            }
        )
    return rows
