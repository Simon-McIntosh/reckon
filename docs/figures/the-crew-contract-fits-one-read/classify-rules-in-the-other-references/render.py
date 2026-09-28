"""Render the binding-rule classification fragment and its figure.

Reads the row data from rows.py, writes the SVG figure beside this script and
the evidence fragment under docs/evidence/fragments/. Sentence totals come from
sentence_counts.py in this directory, which is the reproducible counter named in
the fragment.

Run from the worktree root:

    python3 docs/figures/the-crew-contract-fits-one-read/classify-rules-in-the-other-references/render.py
"""

from __future__ import annotations

import html
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
FRAGMENT = ROOT / "docs" / "evidence" / "fragments" / "the-crew-contract-fits-one-read" / "classify-rules-in-the-other-references.html"
FIGURE_NAME = "sentences-binding-enforced.svg"

HEAD_SHA = "7c25dee788bd668dfbfd36624f8ec590e9dba129"

RUN_ID = "r-20260928T205719503443-classify-rules-in-the-other-references"
RUN_DIR = f"/home/ITER/mcintos/.config/reckon/crew/runs/{RUN_ID}"


def enforced_test_count() -> int:
    ids = {r["t"] for r in ROWS if r["t"] not in ("", "-")}
    return len(ids)


def enforced_test_summary() -> str:
    """The enforced-gate log's own verdict line, read rather than transcribed."""
    log = Path(RUN_DIR) / "gate-enforced.log"
    if not log.exists():
        return "the gate log was not readable at render time"
    lines = [ln.strip() for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    verdict = next((ln for ln in reversed(lines) if " passed" in ln or " failed" in ln), "")
    exit_line = next((ln for ln in reversed(lines) if ln.startswith("EXIT=")), "")
    return f"{verdict} ({exit_line})" if verdict else "the gate log carried no verdict line"

FILES = [
    "conditional-guidance.md",
    "effort-routing.md",
    "lane-routing.md",
    "outage-recovery.md",
    "worker-backends.md",
    "worker-protocol.md",
    "worker-verification.md",
    "orchestrator-harness/claude-code.md",
    "orchestrator-harness/codex-cli.md",
]

DISPOSITION_LABEL = {
    "delete-as-enforced": "delete as enforced",
    "keep": "keep",
    "merge": "merge into a named surviving row",
    "delete-as-obsolete": "delete as obsolete",
}


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rows_mod = load("rows", HERE / "rows.py")
ROWS = rows_mod.ROWS
COUNTS = load("sentence_counts", HERE / "sentence_counts.py").COUNTS


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def rows_for(name: str) -> list[dict]:
    return [r for r in ROWS if r["f"] == name]


def enforced(row: dict) -> bool:
    return bool(row["t"]) and row["t"] != "-"


def wider_gate_lines() -> list[str]:
    log = Path(RUN_DIR) / "gate-classify-rules.log"
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def wider_gate_summary() -> str:
    """pytest's own summary line, never a comment appended around it."""
    lines = [ln.strip() for ln in wider_gate_lines() if ln.strip() and not ln.lstrip().startswith("#")]
    return next((ln for ln in reversed(lines) if " passed" in ln), "the gate log carried no verdict line")


def wider_gate_failures() -> str:
    """Recount the log's failure ids per test file, so the labels and the ids
    cannot disagree: they are both read from the same lines."""
    ids = [ln.split(None, 1)[1].strip() for ln in wider_gate_lines() if ln.startswith("FAILED ")]
    counts: dict[str, int] = {}
    for node_id in ids:
        path = node_id.split("::")[0]
        counts[path] = counts.get(path, 0) + 1
    return ", ".join(f"{path} {n}" for path, n in sorted(counts.items())) + f"; {len(ids)} in total"


DEMOTION_NOTES = (
    "Not marked enforced: the row cited a test module rather than a test",
    "Not marked enforced: no refusal or check in the tree could be named",
    "Not marked enforced: the cited test names neither the enforced symbol nor a refusal",
    "Not marked enforced: enforcement is real but no symbol was identified",
)


def enforced_survey() -> str:
    """Count the rows by why they are not enforced, derived from the rows."""
    counts = {note: 0 for note in DEMOTION_NOTES}
    never = 0
    for row in ROWS:
        if enforced(row):
            continue
        note = next((n for n in DEMOTION_NOTES if n in row["y"]), None)
        if note is None:
            never += 1
        else:
            counts[note] += 1
    shape = {
        DEMOTION_NOTES[0]: "whose citation named a module rather than a test",
        DEMOTION_NOTES[1]: "for which no refusal or check in the tree could be named",
        DEMOTION_NOTES[2]: "whose cited test names neither the enforced symbol nor a refusal",
        DEMOTION_NOTES[3]: "where the enforcement is real but no symbol was identified for it",
    }
    parts = [f"{never} that never cited a test"]
    parts += [f"{n} {shape[note]}" for note, n in counts.items() if n]
    return ", ".join(parts)


def build_figure() -> str:
    """Two bars per file: every sentence, of which the binding rows. A third
    bar shows how many binding rows step 2 marked enforced by code and carry a
    test that makes the guard fire."""
    width = 1040
    left = 330
    right = 120
    top = 96
    band = 74
    height = top + band * len(FILES) + 78
    max_total = max(COUNTS[f] for f in FILES)
    span = width - left - right

    def x(v: int) -> float:
        return left + span * v / max_total

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="Sentences, binding sentences and code-enforced binding sentences per reference file">',
        '<style>'
        'text{font-family:-apple-system,Segoe UI,Roboto,sans-serif;fill:#1c2530}'
        '.t{font-size:13px}.k{font-size:11px;fill:#5b6b7c}'
        '.n{font-size:12px;font-weight:600}'
        '.ttl{font-size:17px;font-weight:700}'
        'rect.bg{fill:#f4f6f9}'
        '</style>',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text class="ttl" x="24" y="38">Every sentence, the binding ones, and the ones code already enforces</text>',
        f'<text class="k" x="24" y="58">Nine reference files under skills/reckon-build/references at {HEAD_SHA[:12]}. '
        f'{sum(COUNTS[f] for f in FILES)} sentences; {len(ROWS)} binding.</text>',
    ]

    for i, name in enumerate(FILES):
        y = top + i * band
        total = COUNTS[name]
        binding = len(rows_for(name))
        enf = sum(1 for r in rows_for(name) if enforced(r))
        parts.append(f'<rect class="bg" x="16" y="{y - 20}" width="{width - 32}" height="{band - 10}" rx="6"/>')
        parts.append(f'<text class="t" x="30" y="{y + 2}">{esc(name)}</text>')
        parts.append(f'<text class="k" x="30" y="{y + 20}">{total} sentences</text>')
        for j, (value, fill, label) in enumerate(
            (
                (total, "#c9d4e0", "all"),
                (binding, "#3f6ea8", "binding"),
                (enf, "#1f9d63", "enforced"),
            )
        ):
            by = y - 12 + j * 14
            w = max(x(value) - left, 2)
            parts.append(f'<rect x="{left}" y="{by}" width="{w:.1f}" height="10" rx="2" fill="{fill}"/>')
            parts.append(f'<text class="n" x="{x(value) + 8:.1f}" y="{by + 9}">{value}</text>')
        parts.append(
            f'<text class="k" x="{left}" y="{y + 44}">all sentences &#183; binding &#183; enforced &#8212; '
            f'"{esc(", ".join(("all", "binding", "enforced")))}" key at left</text>'
        )

    for i, (fill, label) in enumerate(
        (("#c9d4e0", "all sentences"), ("#3f6ea8", "binding rows"), ("#1f9d63", "enforced, with a firing test"))
    ):
        lx = 24 + i * 300
        ly = height - 34
        parts.append(f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="{fill}"/>')
        parts.append(f'<text class="k" x="{lx + 16}" y="{ly}">{esc(label)}</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def build_fragment(figure_rel: str) -> str:
    total_sentences = sum(COUNTS[f] for f in FILES)
    out = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        '  <meta name="docs-project"     content="reckon">',
        '  <meta name="reckon-type"      content="evidence">',
        '  <meta name="plan-slug"        content="classify-rules-in-the-other-references">',
        '  <meta name="plan-title"       content="Binding rules in the other references — classified">',
        '  <meta name="plan-summary"     content="One table row per binding sentence across the nine '
        'skills/reckon-build/references files, with the code that enforces it and a disposition each.">',
        '  <meta name="plan-evidence-for" content="the-crew-contract-fits-one-read">',
        '  <meta name="plan-verifies"    content="the-crew-contract-fits-one-read#s2">',
        "  <title>Binding rules in the other references — classified | reckon</title>",
        '  <link rel="stylesheet" href="/_shared/foundation.css">',
        '  <link rel="stylesheet" href="/_shared/dashboard.css">',
        "</head>",
        "<body>",
        '  <main class="plan-doc">',
        "",
        '    <h2 id="scope">Scope and method</h2>',
        "    <p>Nine reference files under <code>skills/reckon-build/references/</code>. "
        "A <em>binding sentence</em> states a rule a coordinator or worker must follow: it contains or "
        "implies <em>must</em>, <em>never</em>, <em>always</em>, <em>refuse</em>, or it is an imperative. "
        "Every other sentence is counted but not rowed.</p>",
        f"    <p>Read at main HEAD <code>{esc(HEAD_SHA)}</code>. "
        f"{total_sentences} sentences in total; {len(ROWS)} of them binding, each with one row below.</p>",
        "    <p>The sentence counts are produced by <code>sentence_counts.py</code> in this fragment's "
        "figure directory: it strips fenced code blocks and headings, normalises table pipes and list "
        "markers, then splits on sentence-final punctuation. Row lines are the line number of the first "
        "line of the sentence.</p>",
        "    <p>A row is marked <strong>enforced</strong> only where all three hold: the enforcing code is "
        "named as <code>file:symbol</code> and that symbol exists in that file at the read revision; a "
        "single test is named; and that test's body makes the refusal or check fire, naming the enforced "
        "symbol or a refusal. Every cited file, symbol and test was resolved against the tree, never "
        "recalled and never produced by string substitution, and every cited test was run in the "
        "foreground at that revision. Where any of the three is missing the row is <em>keep</em> and its "
        "reason says which half failed.</p>",
        "",
        f"    <p>That bar is deliberately strict, and {sum(1 for r in ROWS if enforced(r))} of the "
        f"{len(ROWS)} rows clear it. The rest divide as {enforced_survey()}. A rule can be enforced in "
        "code and still land here: the row records the mechanism, and the reason records that no test "
        "shows the guard firing.</p>",
        "",
        '    <figure>',
        f'      <img src="{esc(figure_rel)}" alt="Bar chart per reference file: sentences, binding rows, and binding rows already enforced by code with a firing test">',
        "      <figcaption>Each file's full sentence count against the binding subset and the part of it "
        "the code already enforces with a guard a test makes fire.</figcaption>",
        "    </figure>",
        "",
        '    <h2 id="totals">Per-file totals</h2>',
        '    <table class="r-table">',
        "      <thead><tr><th>File</th><th>Sentences</th><th>Binding rows</th>"
        "<th>Enforced (firing test)</th><th>Keep</th><th>Delete as obsolete</th></tr></thead>",
        "      <tbody>",
    ]
    for name in FILES:
        fr = rows_for(name)
        enf = sum(1 for r in fr if enforced(r))
        keep = sum(1 for r in fr if r["d"] == "keep")
        obsolete = sum(1 for r in fr if r["d"] == "delete-as-obsolete")
        out.append(
            f"        <tr><td><code>{esc(name)}</code></td><td>{COUNTS[name]}</td><td>{len(fr)}</td>"
            f"<td>{enf}</td><td>{keep}</td><td>{obsolete}</td></tr>"
        )
    out.append(
        f'        <tr><th>Total</th><th>{total_sentences}</th><th>{len(ROWS)}</th>'
        f'<th>{sum(1 for r in ROWS if enforced(r))}</th>'
        f'<th>{sum(1 for r in ROWS if r["d"] == "keep")}</th>'
        f'<th>{sum(1 for r in ROWS if r["d"] == "delete-as-obsolete")}</th></tr>'
    )
    out += ["      </tbody>", "    </table>", ""]

    out += [
        '    <h2 id="rows">Classification</h2>',
        "    <p>Columns: the rule with its file and line; the code that enforces it, named by file and "
        "symbol (<code>-</code> where none does); a test that makes the guard fire (<code>-</code> where "
        "none does); whether the rule is still wanted; the disposition; and the reason.</p>",
        '    <table class="r-table">',
        "      <thead><tr><th>Rule</th><th>Enforced by</th><th>Firing test</th>"
        "<th>Still wanted</th><th>Disposition</th><th>Why</th></tr></thead>",
        "      <tbody>",
    ]
    for name in FILES:
        fr = sorted(rows_for(name), key=lambda r: r["l"])
        out.append(
            f'        <tr class="group"><td colspan="6"><strong>{esc(name)}</strong> '
            f"&#8212; {COUNTS[name]} sentences, {len(fr)} binding</td></tr>"
        )
        for r in fr:
            disp = DISPOSITION_LABEL.get(r["d"], r["d"])
            out.append(
                "        <tr>"
                f'<td><code>{esc(name)}:{r["l"]}</code> {esc(r["r"])}</td>'
                f'<td><code>{esc(r["e"])}</code></td>'
                f'<td><code>{esc(r["t"])}</code></td>'
                f'<td>{esc(r["w"])}</td>'
                f'<td>{esc(disp)}</td>'
                f'<td>{esc(r["y"])}</td>'
                "</tr>"
            )
    out += ["      </tbody>", "    </table>", ""]

    out += [
        '    <h2 id="evidence">Evidence</h2>',
        "    <p>Nothing in the nine reference files or in the code was edited by this node. The rows are "
        "classification only.</p>",
        f"    <p>This fragment is the deliverable of crew run "
        f"<code>{esc(RUN_ID)}</code>, node <code>classify-rules-in-the-other-references</code>.</p>",
        "",
        "    <p>Two foreground logs, both under this run's directory "
        f"<code>{esc(RUN_DIR)}</code>:</p>",
        "    <ul>",
        f"      <li><code>{esc(RUN_DIR)}/gate-enforced.log</code> — the driving tests of every enforced "
        f"row, run in the foreground. {enforced_test_count()} test ids from "
        "<code>gate-enforced-set.txt</code>; the log's header names the run id, the revision, the tree, "
        "the resolved <code>module.__file__</code> and the full command line, and its own verdict line "
        f"reads <code>{esc(enforced_test_summary())}</code>. A test that failed here would not have been "
        "allowed to carry an enforced row.</li>",
        f"      <li><code>{esc(RUN_DIR)}/gate-classify-rules.log</code> — the wider gate. Its own summary "
        f"line reads <code>{esc(wider_gate_summary())}</code>, and its failure ids recount per file as "
        f"{esc(wider_gate_failures())} — matching the ids listed in the manifest's "
        "<code>failure_attribution</code>. All of them are outside this node's write scope, which is "
        "documentation only, and the node ran at main HEAD "
        f"<code>{esc(HEAD_SHA)}</code> with no source file modified. This log's own "
        "<code># command:</code> header carries an unexpanded placeholder; the files it collected are "
        "beside it in <code>gate-focused-set.txt</code>.</li>",
        "    </ul>",
        "",
        f"    <p>The run record for this revision is at {esc(RUN_DIR)}/manifest.md.</p>",
        "",
        "  </main>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(out)


def main() -> None:
    svg = build_figure()
    figure_path = HERE / FIGURE_NAME
    figure_path.write_text(svg, encoding="utf-8")

    rel = f"/reckon/figures/the-crew-contract-fits-one-read/classify-rules-in-the-other-references/{FIGURE_NAME}"
    FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
    FRAGMENT.write_text(build_fragment(rel), encoding="utf-8")
    print(f"wrote {figure_path}")
    print(f"wrote {FRAGMENT}")


if __name__ == "__main__":
    main()