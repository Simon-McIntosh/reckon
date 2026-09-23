"""Plot the population the state checks report over, and the token stream they read.

Two relationships are clearer drawn than described. The population panel shows
every finding the state-and-structure checks return against the whole corpus, so
a reader can see that one code is the entire yield and the island check is silent
over a non-empty population. The token panel shows the unclosed-row specimen as
the raw stream the scanner reads: three missing end tags that a parser repairs
away, which is why the check must read the stream and not the tree.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path(__file__).with_name("state-check-population.png")
DOCS = 262
CODES = [
    ("duplicate-plan-scalar", 14, 4),
    ("tr-unclosed", 1, 1),
    ("resource-island-missing", 0, 0),
    ("resource-island-malformed", 0, 0),
]


def _population(ax: plt.Axes) -> None:
    labels = [code for code, _, _ in CODES][::-1]
    findings = [n for _, n, _ in CODES][::-1]
    docs = [d for _, _, d in CODES][::-1]
    bars = ax.barh(labels, findings, color="#b4462f", height=0.55)
    for bar, n, d in zip(bars, findings, docs):
        ax.text(
            bar.get_width() + 0.25,
            bar.get_y() + bar.get_height() / 2,
            f"{n} in {d} doc" + ("" if d == 1 else "s"),
            va="center",
            fontsize=8,
            color="#333",
        )
    ax.set_xlim(0, 18)
    ax.set_xlabel(f"findings reported (denominator: {DOCS} documents)")
    ax.set_title("Every finding the two new checks return", fontsize=10)
    ax.tick_params(labelsize=8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _token_stream(ax: plt.Axes) -> None:
    tokens = ["<tr>", "<td>1</td>", "</tr>", "<tr>", "<td>2</td>", "<tr>", "<td>3</td>", "<tr>", "<td>4</td>", "</table>"]
    missing = {3, 5, 7}
    ax.set_xlim(-0.5, len(tokens) - 0.4)
    ax.set_ylim(-0.9, 1.5)
    ax.axis("off")
    for i, token in enumerate(tokens):
        opened = token.startswith("<tr>")
        face = "#eef3f6" if not opened else "#f7e6e2"
        ax.add_patch(
            plt.Rectangle((i - 0.42, -0.28), 0.84, 0.56, facecolor=face, edgecolor="#7a7a7a", linewidth=0.7)
        )
        ax.text(i, 0.0, token, ha="center", va="center", fontsize=7, family="monospace")
        if i in missing:
            ax.annotate(
                "no </tr>",
                xy=(i, 0.30), xytext=(i, 0.95),
                ha="center", fontsize=7, color="#b4462f",
                arrowprops=dict(arrowstyle="->", color="#b4462f", linewidth=0.8),
            )
    ax.text(
        -0.4, -0.72,
        "raw tag stream the scanner reads: 4 <tr> opens, 1 </tr> — the tree a parser returns shows neither gap",
        fontsize=7.5, color="#333",
    )
    ax.set_title("The unclosed-row specimen, as the scanner sees it", fontsize=10)


def main() -> None:
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(8.2, 5.4), gridspec_kw={"height_ratios": [1.0, 0.78]})
    _population(top)
    _token_stream(bottom)
    fig.tight_layout()
    fig.savefig(OUT, dpi=150)
    print("wrote", OUT)


if __name__ == "__main__":
    main()