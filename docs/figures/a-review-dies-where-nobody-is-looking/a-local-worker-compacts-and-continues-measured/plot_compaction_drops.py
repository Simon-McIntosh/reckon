"""Draw context size either side of each compaction in one worker's stream.

Reads the boundary records out of the run's stream so the figure cannot drift
from the data it plots. Text is written as paths (svg.fonttype=path) so the
figure carries its labels in any renderer, including one without the fonts
matplotlib names.

usage: plot_compaction_drops.py <run-dir>
"""

import json
import os
import sys

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, 'compaction-drops.svg')
WINDOW = 230144
FEATURE = '#2f6fbf'
GREY = '#929292'
MUTED = '#5a5a5a'


def boundaries(run):
    out = []
    with open(os.path.join(run, 'stream.jsonl')) as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get('type') == 'system' and rec.get('subtype') == 'compact_boundary':
                meta = rec['compact_metadata']
                out.append((i, meta['pre_tokens'] / 1000.0,
                            meta['post_tokens'] / 1000.0))
    return out


def draw(points):
    plt.style.use('default')
    plt.rcParams.update({
        'font.size': 14,
        'axes.labelsize': 16,
        'xtick.labelsize': 14,
        'ytick.labelsize': 14,
        'font.family': 'sans-serif',
        'svg.fonttype': 'path',
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.grid': False,
        'figure.dpi': 200,
    })
    fig, ax = plt.subplots(figsize=(31.1 / 2.54, 16 / 2.54))

    for idx, pre, post in points:
        ax.plot([idx, idx], [post, pre], color=FEATURE, lw=2.6,
                solid_capstyle='butt', zorder=3)
        ax.plot([idx], [pre], 'o', ms=9, color=FEATURE, zorder=4)
        ax.plot([idx], [post], 'o', ms=8, mfc='white', mec=FEATURE, mew=2.2,
                zorder=4)

    ax.axhline(WINDOW / 1000.0, color=GREY, lw=1.2, ls=(0, (2, 5)), zorder=1)
    ax.text(7880, WINDOW / 1000.0 + 4, 'declared usable window 230 144',
            ha='right', va='bottom', color=MUTED, fontsize=14)
    ax.annotate('before', (points[0][0], points[0][1]),
                textcoords='offset points', xytext=(0, 14),
                ha='center', color=FEATURE, fontsize=14)
    ax.annotate('after', (points[-1][0], points[-1][2]),
                textcoords='offset points', xytext=(0, -22),
                ha='center', color=FEATURE, fontsize=14)

    ax.set_xlim(5950, 7900)
    ax.set_ylim(0, 250)
    ax.set_xticks([6000, 6500, 7000, 7500])
    ax.set_xticklabels(['6 000', '6 500', '7 000', '7 500'])
    ax.set_yticks([0, 50, 100, 150, 200])
    ax.set_xlabel('stream record index')
    ax.set_ylabel('context [k tokens]')
    ax.tick_params(direction='out', length=6, colors=MUTED)
    fig.tight_layout()
    return fig, ax


def main(argv):
    run = argv[1] if len(argv) > 1 else os.environ.get('RECKON_RUN_DIR', '')
    points = boundaries(run)
    fig, ax = draw(points)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    fbox = fig.bbox
    boxes = []
    for art in fig.findobj(matplotlib.text.Text):
        label = art.get_text().strip()
        if not art.get_visible() or not label:
            continue
        bb = art.get_window_extent(renderer)
        if (bb.x0 < fbox.x0 - 1 or bb.y0 < fbox.y0 - 1
                or bb.x1 > fbox.x1 + 1 or bb.y1 > fbox.y1 + 1):
            print('CLIPPED %r' % label)
            return 2
        boxes.append((label, bb))
    for i, (l1, b1) in enumerate(boxes):
        for l2, b2 in boxes[i + 1:]:
            if not b1.overlaps(b2):
                continue
            inter = b1.intersection(b2)
            if inter.width > 1 and inter.height > 1:
                print('OVERLAP %r %r' % (l1, l2))
                return 2
    fig.savefig(SVG, format='svg')
    emitted = open(SVG).read()
    if '<text' in emitted:
        print('TEXT ELEMENTS in the emitted svg: a renderer without fonts '
              'draws none of the fourteen labels')
        return 3
    paths = emitted.count('<path')
    if paths < 40:
        print('only %d paths: the label outlines are missing' % paths)
        return 3
    print('OK %d boundaries, %d text artists, %s' % (len(points), len(boxes), SVG))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))