"""Draw the figure pairs used to test a reviewer's discrimination.

Five pairs; each pair is two figures drawn on the same canvas. One figure of a
pair carries a single defect and its twin does not, so a verdict on the
pair can be attributed to the one rule the defect touches.

Every figure is written as the SVG it was drawn in and as a PNG rendered from
that SVG. Which letter (``a`` or ``b``) carries the defect is drawn from a
seeded random source, so the file name never gives the role away, and
``pairs.json`` records the rule and the defect per pair for the operator. No
figure carries a word that names its role.
"""

from __future__ import annotations

import contextlib
import json
import random
import shutil
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent

WIDTH_IN = 14.0
HEIGHT_IN = 6.0
DPI = 100
TICK_PT = 20
LABEL_PT = 22
BAD_BODY_PT = 8

# A moderate blue for the series a figure is about, and a saturated blue for a
# filled area a label sits on.
FEATURE = "#2f6fb0"
SATURATED = "#1f5fa8"
NEAR_WHITE = "#f7f7f7"
LOW_CONTRAST_GREY = "#8a8a8a"
NEAR_BLACK = "#0b0b0b"
REFERENCE = "#52514e"

GREY = (0.573, 0.573, 0.573)
RED = (0.706, 0.141, 0.122)

RNG_SEED = 20_261_016


def _apply_style() -> None:
    with contextlib.suppress(OSError):
        plt.style.use("data-ink")
    # A fixed salt makes the SVG element ids stable, so a re-run reproduces the
    # figure byte for byte rather than only in name.
    plt.rcParams["svg.hashsalt"] = "figure-review-pairs"


def _axes(figsize: tuple[float, float] = (WIDTH_IN, HEIGHT_IN)):
    _apply_style()
    return plt.subplots(figsize=figsize, dpi=DPI)


def _label_sizes(ax: plt.Axes, body_pt: float = TICK_PT) -> None:
    ax.xaxis.label.set_size(LABEL_PT)
    ax.yaxis.label.set_size(LABEL_PT)
    ax.tick_params(labelsize=body_pt)


def _rasterise(svg: Path, png: Path) -> None:
    if shutil.which("rsvg-convert"):
        subprocess.run(
            ["rsvg-convert", "-w", str(int(WIDTH_IN * DPI)), "-o", str(png), str(svg)],
            check=True,
        )
        return
    if shutil.which("inkscape"):
        subprocess.run(
            [
                "inkscape",
                str(svg),
                "--export-type=png",
                f"--export-filename={png}",
            ],
            check=True,
        )
        return
    if shutil.which("convert"):
        subprocess.run(
            ["convert", "-density", str(DPI), str(svg), str(png)], check=True
        )
        return
    msg = "no SVG rasteriser found: tried rsvg-convert, inkscape and convert"
    raise SystemExit(msg)


def _save(fig: plt.Figure, name: str) -> tuple[Path, Path]:
    svg = HERE / f"{name}.svg"
    png = HERE / f"{name}.png"
    fig.tight_layout()
    fig.savefig(svg, metadata={"Date": None})
    plt.close(fig)
    _rasterise(svg, png)
    return svg, png


# --- shared data -------------------------------------------------------------


def _profile(n: int = 200):
    rho = np.linspace(0.0, 1.0, n)
    temperature = 1.2 * (1.0 - rho**2) ** 1.6 + 0.12
    return rho, temperature


# --- pair 1: a table where a plot belongs ------------------------------------


def draw_table_versus_plot(bad: bool):
    rho, temperature = _profile()
    fig, ax = _axes()
    if bad:
        ax.axis("off")
        rows = np.linspace(0.0, 1.0, 11)
        values = 1.2 * (1.0 - rows**2) ** 1.6 + 0.12
        ax.text(0.08, 0.93, "ρ", fontsize=TICK_PT, color=NEAR_BLACK)  # noqa: RUF001
        ax.text(
            0.28,
            0.93,
            "electron temperature [keV]",
            fontsize=TICK_PT,
            color=NEAR_BLACK,
        )
        for i, (r, v) in enumerate(zip(rows, values, strict=True)):
            y = 0.83 - i * 0.075
            ax.text(0.08, y, f"{r:.2f}", fontsize=TICK_PT, color=NEAR_BLACK)
            ax.text(0.28, y, f"{v:.3f}", fontsize=TICK_PT, color=NEAR_BLACK)
    else:
        ax.plot(rho, temperature, color=FEATURE, lw=3.0)
        ax.set_xlabel("normalized radius ρ")  # noqa: RUF001
        ax.set_ylabel("electron temperature [keV]")
        _label_sizes(ax)
        ax.text(
            0.985,
            0.92,
            "on-axis",
            transform=ax.transAxes,
            ha="right",
            color=FEATURE,
            fontsize=TICK_PT,
        )
    return fig


# --- pair 2: text contrast against a saturated ground ------------------------


def draw_contrast(bad: bool):
    x = np.linspace(0.0, 10.0, 400)
    y = np.exp(-((x - 4.6) ** 2) / 2.2) * 0.9 + 0.05
    fig, ax = _axes()
    ax.fill_between(x, 0.0, y, color=SATURATED, lw=0.0)
    ax.set_xlabel("time [s]")
    ax.set_ylabel("radiated power [a.u.]")
    _label_sizes(ax)
    colour = LOW_CONTRAST_GREY if bad else NEAR_WHITE
    ax.text(
        4.6, 0.42, "marfe", ha="center", va="center", color=colour, fontsize=LABEL_PT
    )
    return fig


# --- pair 3: body text against the legibility floor --------------------------


def draw_legibility(bad: bool):
    t = np.linspace(0.0, 10.0, 500)
    signal = np.exp(-t / 4.0) * np.sin(2.0 * np.pi * 0.7 * t) + 0.35
    fig, ax = _axes()
    ax.plot(t, signal, color=FEATURE, lw=3.0)
    ax.set_xlabel("time [s]")
    ax.set_ylabel("magnetic perturbation [mT]")
    body_pt = BAD_BODY_PT if bad else TICK_PT
    ax.xaxis.label.set_size(LABEL_PT)
    ax.yaxis.label.set_size(LABEL_PT)
    ax.tick_params(labelsize=body_pt)
    ax.text(6.4, 0.95, "mode onset", color=FEATURE, fontsize=body_pt)
    return fig


# --- pair 4: data-ink --------------------------------------------------------


def _shade(rgb, f: float):
    return tuple(c + (1.0 - c) * f for c in rgb)


def draw_data_ink(bad: bool):
    labels = ["1", "2", "3", "4", "5", "6"]
    values = np.array([2.1, 3.4, 2.8, 5.6, 3.1, 2.4])
    limit = 4.0
    fig, ax = _axes()
    if bad:
        ax.set_facecolor("#eef2f7")
        ax.grid(True, color="#c9d2dd")
    for i, v in enumerate(values):
        colour = RED if v > limit else GREY
        if bad:
            ax.add_patch(
                plt.Rectangle(
                    (i - 0.36 + 0.07, -0.03),
                    0.72,
                    v,
                    facecolor="#b9c2cf",
                    edgecolor="none",
                    zorder=1,
                )
            )
            slices = 40
            for k in range(slices):
                f = k / (slices - 1)
                ax.add_patch(
                    plt.Rectangle(
                        (i - 0.36, f * v),
                        0.72,
                        v / slices,
                        facecolor=_shade(colour, 0.55 * f),
                        edgecolor="none",
                        zorder=2,
                    )
                )
        else:
            ax.bar(i, v, width=0.72, color=colour, zorder=2)
        ax.text(
            i,
            v + 0.08,
            f"{v:.1f}",
            ha="center",
            color=NEAR_BLACK,
            fontsize=TICK_PT,
            zorder=4,
        )
    ax.axhline(limit, ls=":", lw=1.2, color=REFERENCE, zorder=3)
    ax.text(5.45, limit + 0.08, "limit", color=REFERENCE, fontsize=TICK_PT, ha="right")
    ax.set_xticks(np.arange(len(labels)), labels)
    ax.set_xlabel("coil index")
    ax.set_ylabel("measured field error [%]")
    _label_sizes(ax)
    ax.set_xlim(-0.7, 5.7)
    ax.set_ylim(0.0, 6.2)
    return fig


# --- pair 5: the poloidal-flux case ------------------------------------------


def draw_poloidal_flux(bad: bool):
    r = np.linspace(0.6, 2.4, 140)
    z = np.linspace(-1.2, 1.2, 140)
    rg, zg = np.meshgrid(r, z)
    r0, z0, width = 1.6, 0.05, 0.55
    flux = np.exp(-((rg - r0) ** 2 + (zg - z0) ** 2) / (2.0 * width**2))
    fig, ax = _axes()
    contour = ax.contourf(rg, zg, flux, levels=12, cmap="viridis")
    cbar = fig.colorbar(contour, ax=ax)
    ax.set_xlabel("R [m]")
    ax.set_ylabel("Z [m]")
    _label_sizes(ax)
    cbar.ax.tick_params(labelsize=TICK_PT)
    if not bad:
        cbar.set_label("poloidal flux [Wb]", fontsize=LABEL_PT)
    return fig


# --- driver ------------------------------------------------------------------


def _pairs():
    return [
        (
            1,
            (
                "A figure presents its data as a continuous plotted curve; a "
                "rendered table of labels and numeric values is not a plot."
            ),
            "the data is set as a table of text rows, not plotted as a curve",
            draw_table_versus_plot,
        ),
        (
            2,
            (
                "Text set on a dark or saturated ground holds strong contrast "
                "against it, such as near-white on a saturated or dark fill."
            ),
            ("a mid-grey label is set on a saturated blue fill, giving weak contrast"),
            draw_contrast,
        ),
        (
            3,
            (
                "Body text, the axis tick labels and direct labels, is set at "
                "or above the legibility floor of 20 pt."
            ),
            (
                "the tick labels and direct label are set at 8 pt, well below "
                "the 20 pt floor"
            ),
            draw_legibility,
        ),
        (
            4,
            (
                "The figure carries only its data and the minimum needed to "
                "read it: flat marks, hairline rules and direct labels, with "
                "no gradient, shadow, decorative fill or gridline."
            ),
            (
                "the bars carry a vertical gradient and a drop shadow, the "
                "plot has a decorative background fill and gridlines are drawn"
            ),
            draw_data_ink,
        ),
        (
            5,
            (
                "A poloidal-flux plot labels its colour axis with the quantity "
                "and its unit, poloidal flux [Wb]."
            ),
            (
                "the colour axis carries no label, so the plotted quantity and "
                "unit are unstated"
            ),
            draw_poloidal_flux,
        ),
    ]


def main() -> None:
    rng = random.Random(RNG_SEED)  # noqa: S311 -- a seeded draw to hide the role
    records = []
    for number, rule, defect, draw in _pairs():
        bad_letter = rng.choice(("a", "b"))
        files = []
        for letter in ("a", "b"):
            name = f"pair-{number}-{letter}"
            svg, png = _save(draw(bad=letter == bad_letter), name)
            files.extend([svg.name, png.name])
        records.append(
            {
                "pair": number,
                "rule": rule,
                "defect": defect,
                "bad": bad_letter,
                "files": files,
            }
        )
    payload = {"pairs": records}
    (HERE / "pairs.json").write_text(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    main()
