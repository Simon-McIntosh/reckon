"""Fail when the figure's text depends on a font the renderer must resolve.

A reviewer rendered this figure on a host with no usable font and got four rows
of grey seats with no labels at all: the shapes survived and every glyph was
lost, because the words lived in SVG text elements. A label drawn as a vector
path is a shape like any other, so the figure says the same thing under any
font configuration.

The check renders the SVG twice — once with the host's fonts and once with an
empty font directory — and requires the two images to be identical, and the SVG
to carry no text element. Exit 0 when both hold, 1 otherwise.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

HERE = Path(__file__).resolve().parent
SVG = HERE / "placement-bound-queueing.svg"
EMPTY_FONTS = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>{directory}</dir>
  <cachedir>{directory}/cache</cachedir>
</fontconfig>
"""


def render(svg: Path, png: Path, fonts: Path | None) -> None:
    environment = dict(os.environ)
    if fonts is None:
        environment.pop("FONTCONFIG_FILE", None)
    else:
        config = fonts / "fonts.conf"
        config.write_text(EMPTY_FONTS.format(directory=fonts), encoding="utf-8")
        environment["FONTCONFIG_FILE"] = str(config)
    subprocess.run(
        ["convert", "-density", "100", str(svg), str(png)],
        check=True,
        env=environment,
        capture_output=True,
    )


def main() -> int:
    text_elements = SVG.read_text(encoding="utf-8").count("<text")
    with tempfile.TemporaryDirectory(prefix="figure-text-") as workspace:
        room = Path(workspace)
        fonts = room / "fonts"
        fonts.mkdir()
        (fonts / "cache").mkdir()
        host_png = room / "host.png"
        bare_png = room / "bare.png"
        render(SVG, host_png, fonts=None)
        render(SVG, bare_png, fonts=fonts)
        with Image.open(host_png) as host, Image.open(bare_png) as bare:
            host = host.convert("RGB")
            bare = bare.convert("RGB")
            difference = ImageChops.difference(host, bare)
            differing = difference.getbbox()
            ink = host.convert("L").getbbox()

    changed = differing is not None
    print(f"text elements: {text_elements}")
    print(f"differing area between the host render and the fontless render: {differing}")
    print(f"ink present in the host render: {ink}")
    if text_elements:
        print("FAIL: the figure carries SVG text, so a renderer without a font loses it")
        return 1
    if changed:
        print("FAIL: the two renders differ, so the figure's content depends on a font")
        return 1
    print("PASS: every label is a path and both renders are identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())