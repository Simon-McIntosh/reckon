"""Draw the fence-boundary figure with every label emitted as a glyph path.

The figure is a schematic, so the image must carry its own words. Text written
as <text> needs a font resolvable at render time, and a rasteriser with no
usable font draws the boxes and arrows and nothing else. Every label here is
therefore converted to an outline path when the SVG is written.
"""

import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib.text import Text

matplotlib.rcParams["svg.fonttype"] = "path"
matplotlib.rcParams["font.family"] = "DejaVu Sans"

INK = "#2b2b2b"
BODY = "#3f3f3f"
GREY = "#7a7a7a"
GREEN = "#2e8b57"
GREEN_DARK = "#1f5c3a"
PAPER = "#ffffff"

W_PX, H_PX = 1400, 720

fig = plt.figure(figsize=(W_PX / 100, H_PX / 100), dpi=100)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W_PX)
ax.set_ylim(H_PX, 0)
ax.axis("off")

texts: list[Text] = []


def put(x, y, s, *, size=14, colour=BODY, ha="left"):
    t = ax.text(x, y, s, fontsize=size, color=colour, ha=ha, va="center")
    texts.append(t)
    return t


def panel(x, y, w, h, colour):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0,rounding_size=10",
            linewidth=1.3, edgecolor=colour, facecolor="none")
    )


def arrow(start, end, colour, dashed):
    ax.annotate(
        "", xy=end, xytext=start,
        arrowprops=dict(
            arrowstyle="-|>",
            color=colour,
            linewidth=1.8 if dashed else 2.4,
            linestyle=(0, (4, 3)) if dashed else "solid",
            mutation_scale=16 if dashed else 20,
            shrinkA=0,
        ),
    )


put(60, 48, "everything else — mounted read-only", size=16, colour=INK)
put(1340, 48, "granted — bound writable after the root", size=16, colour=INK,
    ha="right")

panel(60, 110, 460, 450, GREY)
panel(880, 110, 460, 450, GREEN)

put(88, 170, "a sibling checkout")
put(88, 240, "the operator's home")
put(88, 310, "a shared project root")
put(88, 380, "the node's /tmp, outside the scratch")
put(88, 462, "a write or a recursive removal here", colour=GREY)
put(88, 486, "fails in the kernel — Read-only file", colour=GREY)
put(88, 510, "system — whether or not anybody", colour=GREY)
put(88, 534, "listed the path", colour=GREY)

put(908, 170, "the run's worktree, its git directory", colour=GREEN_DARK)
put(908, 192, "and the shared object store", colour=GREEN_DARK)
put(908, 280, "the run directory its manifest lands in", colour=GREEN_DARK)
put(908, 368, "the run's own scratch under the node tmp", colour=GREEN_DARK)
put(908, 456, "each declared grant, and never its parent", colour=GREEN_DARK)

ax.add_patch(
    FancyBboxPatch(
        (620, 320), 160, 110,
        boxstyle="round,pad=0,rounding_size=10",
        linewidth=0, facecolor=INK)
)
put(700, 362, "the fence", size=16, colour=PAPER, ha="center")
put(700, 390, "and its worker", colour=PAPER, ha="center")

put(570, 140, "refused", colour=GREY, ha="center")
put(826, 140, "granted", colour=GREEN, ha="center")

for start_y, end_y in ((340, 170), (360, 240), (380, 310), (400, 380)):
    arrow((618, start_y), (528, end_y), GREY, dashed=True)
for start_y, end_y in ((340, 192), (360, 280), (380, 368), (400, 456)):
    arrow((782, start_y), (872, end_y), GREEN, dashed=False)

put(60, 620, "a declared grant naming a filesystem root or a project root is "
             "refused by name")
put(60, 646, "before any mount is composed, so no worker ever runs against "
             "that boundary")

fig.canvas.draw()
renderer = fig.canvas.get_renderer()

# A label that leaves the canvas is not delivered, and neither is one that
# lands on another label; refuse to write a figure a reader cannot read.
canvas = fig.bbox
problems = []
for t in texts:
    bb = t.get_window_extent(renderer)
    if (bb.x0 < canvas.x0 or bb.x1 > canvas.x1
            or bb.y0 < canvas.y0 or bb.y1 > canvas.y1):
        problems.append(f"off canvas: {t.get_text()!r}")
for i, a in enumerate(texts):
    ba = a.get_window_extent(renderer)
    for b in texts[i + 1:]:
        if ba.overlaps(b.get_window_extent(renderer)):
            problems.append(f"overlap: {a.get_text()!r} with {b.get_text()!r}")

if problems:
    print("\n".join(problems), file=sys.stderr)
    raise SystemExit(1)

out = sys.argv[1]
fig.savefig(out, format="svg", transparent=True)
print("wrote", out, "|", len(texts), "labels, none clipped or overlapping")