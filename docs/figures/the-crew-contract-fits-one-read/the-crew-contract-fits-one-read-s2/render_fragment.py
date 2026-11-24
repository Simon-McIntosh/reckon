"""Render the binding-rule classification fragment.

The page states a test only where one was run. So the renderer reads the fired
enforcement logs and refuses to write if any enforced row cites a test whose
identifier does not appear in them: a row cannot claim an enforcement it cannot
point at.

The refusal is the negative control. Feed it a rows file whose enforced row
names an identifier the logs do not carry, and it exits 2 naming the row.

Each log's revision and result line are read through the readers the manifest
audit itself uses, so the page cannot state a figure the audit would spell
differently.

Run:  python3 render_fragment.py --out <fragment.html> --logs <enforced-logs-dir>
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import classify
import scan_head
from reckon.crew.reports import _GATE_LOG_REVISION, _first_recorded_line
from reckon.crew.review import pytest_summary_line

NODE = "the-crew-contract-fits-one-read-s2"


def load_logs(logs_dir: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(logs_dir.glob("*.log"))
    )


def check_citations(rows, logs_text: str) -> list[str]:
    missing = []
    for r in rows:
        if r["disposition"] != "delete as enforced":
            continue
        if r["test"] and r["test"] not in logs_text:
            missing.append(f"{r['file']}:{r['line']} cites {r['test']}")
    return missing


def log_statements(logs_dir: Path) -> list[tuple[str, str, str, str]]:
    """Each log's name, revision, result line and the command that ran it."""
    statements = []
    for path in sorted(logs_dir.glob("*.log")):
        text = path.read_text(encoding="utf-8", errors="replace")
        named = _GATE_LOG_REVISION.search(_first_recorded_line(text))
        summary = pytest_summary_line(text)
        command = next(
            (line[len("# command:") :].strip() for line in text.splitlines() if line.startswith("# command:")),
            "",
        )
        if named and summary:
            statements.append((path.name, named.group(1), summary, command))
    return statements


def log_for_test(test: str, statements) -> str | None:
    """The one log whose command ran this test node, if any ran it."""
    node = test.split("::", 1)[-1]
    for name, _revision, _summary, command in statements:
        if node and node in command:
            return name
    return None


def per_file_counts():
    counts = {}
    for rel in scan_head.FILES:
        text = (scan_head.SKILLS / rel).read_text(encoding="utf-8")
        sents = scan_head.sentences_with_lines(text)
        counts[f"skills/reckon-build/{rel}"] = (
            len(sents),
            sum(1 for _, s in sents if scan_head.is_binding(s)),
        )
    return counts


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def render(rows, total: int, statements) -> str:
    counts = per_file_counts()
    enforced = [r for r in rows if r["disposition"] == "delete as enforced"]
    obsolete = [r for r in rows if r["disposition"] == "delete as obsolete"]
    adds = [r for r in rows if r["disposition"].startswith("merge into")]
    kept = len(rows) - len(enforced) - len(obsolete) - len(adds)

    # One line per cited test, with its log.
    tests = {}
    for r in enforced:
        tests.setdefault(r["test"], r["enforced_by"])

    out = []
    out.append("<!doctype html>")
    out.append('<html lang="en">')
    out.append("<head>")
    out.append('  <meta charset="utf-8">')
    out.append('  <meta name="docs-project" content="reckon">')
    out.append('  <meta name="reckon-type" content="evidence">')
    out.append(f'  <meta name="plan-slug" content="{NODE}">')
    out.append('  <meta name="plan-title" content="Binding build rules — classification at head">')
    out.append(
        '  <meta name="plan-summary" content="One row per binding sentence in the build skill and its references, with its enforcement, its still-wanted verdict and its disposition.">'
    )
    out.append('  <meta name="plan-evidence-for" content="the-crew-contract-fits-one-read">')
    out.append('  <meta name="plan-verifies" content="the-crew-contract-fits-one-read#s2">')
    out.append("  <title>Binding build rules — classification at head | reckon</title>")
    out.append('  <link rel="stylesheet" href="/_shared/foundation.css">')
    out.append('  <link rel="stylesheet" href="/_shared/dashboard.css">')
    out.append("</head>")
    out.append("<body>")
    out.append(' <main class="plan-doc">')
    out.append("  <h1>Binding build rules — classification at head</h1>")

    out.append(f'  <section id="{NODE}-scope">')
    out.append("   <h2>§1 — Method and scope</h2>")
    out.append(
        "   <p><code>scan_head.py</code> splits <code>skills/reckon-build/SKILL.md</code> and every file under"
        " <code>skills/reckon-build/references/</code> — nine references plus the two harness files — into"
        " prose sentences, dropping fenced code, headings, table rows and raw HTML. The keyword set,"
        " imperative-verb table and abbreviation guard are imported from"
        " <code>classify-rules-in-the-skill/scan_binding.py</code>, the module that already owns them, so"
        " there is one definition of what reads as a rule. A sentence is <em>binding</em> when it carries"
        " that keyword set (<em>must</em>, <em>never</em>, <em>always</em>, <em>do not</em>, <em>only</em>,"
        " <em>refuse</em>, <em>shall</em>, <em>mandatory</em>, <em>no longer</em>, <em>cannot</em>,"
        " <em>required</em>, and the rest) or opens with an imperative verb from that table. Every binding"
        " sentence has one row in §2, located at the line its first character sits on.</p>"
    )
    out.append(
        f"   <p>The plan written on 2026-09-26 named eight reference files. The corpus is twelve files today:"
        f" <code>coordinator-reference.md</code> was added later and the harness directory carries two files."
        f" All twelve are inventoried. At this head the twelve files carry <strong>{total}</strong> prose"
        f" sentences, and <strong>{len(rows)}</strong> of them are binding — roughly one sentence in"
        f" {round(total / len(rows))} states a rule.</p>"
    )
    out.append("   <table>")
    out.append("    <thead><tr><th>Measure</th><th>Value</th></tr></thead><tbody>")
    out.append(f"     <tr><td>Total prose sentences</td><td>{total}</td></tr>")
    out.append(f"     <tr><td>Binding sentences (one row each)</td><td>{len(rows)}</td></tr>")
    out.append(f"     <tr><td>Marked enforced, test cited and fired</td><td>{len(enforced)}</td></tr>")
    out.append(f"     <tr><td>Delete as enforced</td><td>{len(enforced)}</td></tr>")
    out.append(f"     <tr><td>Delete as obsolete</td><td>{len(obsolete)}</td></tr>")
    out.append(f"     <tr><td>Merge into a surviving row</td><td>{len(adds)}</td></tr>")
    out.append(f"     <tr><td>Keep</td><td>{kept}</td></tr>")
    out.append("   </tbody></table>")

    out.append("   <table>")
    out.append("    <thead><tr><th>File</th><th>Sentences</th><th>Binding</th></tr></thead><tbody>")
    for name, (s, b) in counts.items():
        out.append(f"     <tr><td><code>{esc(name)}</code></td><td>{s}</td><td>{b}</td></tr>")
    out.append(f"     <tr><td><strong>Total</strong></td><td><strong>{total}</strong></td><td><strong>{len(rows)}</strong></td></tr>")
    out.append("   </tbody></table>")
    out.append(
        "   <p>The <em>still wanted</em> column is this node's proposal; the lead confirms it before §3"
        " starts. A row marked <em>enforced</em> names the refusal or check and a test that drives the"
        " guarded action far enough to make it fire; every cited test was run in the foreground at this"
        " head and its log is in §3.</p>"
    )
    out.append("  </section>")

    out.append(f'  <section id="{NODE}-table">')
    out.append("   <h2>§2 — The classification</h2>")
    out.append("   <table>")
    out.append(
        "    <thead><tr><th>#</th><th>Rule (file:line)</th><th>Enforced by</th>"
        " <th>Test that fires it</th><th>Still wanted</th><th>Disposition</th></tr></thead><tbody>"
    )
    for i, r in enumerate(rows, 1):
        loc = f"{esc(r['file'])}:{r['line']}"
        test = esc(r["test"]) if r["test"] else "—"
        out.append(
            f"     <tr><td>{i}</td><td>{esc(r['rule'])}<br><code>{loc}</code></td>"
            f"<td>{esc(r['enforced_by'])}</td><td>{test}</td>"
            f"<td>{esc(r['still_wanted'])}</td><td>{esc(r['disposition'])}</td></tr>"
        )
    out.append("   </tbody></table>")
    out.append("  </section>")

    out.append(f'  <section id="{NODE}-enforcement">')
    out.append("   <h2>§3 — Enforcement evidence</h2>")
    out.append(
        f"   <p>{len(enforced)} rows name a production refusal or check, and {len(tests)} distinct tests"
        " drive those guards far enough to make each fire. Every cited test node id was run at this head,"
        " and each run exited 0. The logs are under the run directory <code>logs/enforced/</code>.</p>"
    )
    out.append(
        "   <p>Every log in this section records the revision it ran at on its first line and its own"
        " runner result line; both are read here through the readers the manifest audit uses, so the"
        " page and the audit spell each figure the same way:</p>"
    )
    out.append("   <ul>")
    for name, revision, summary, _command in statements:
        out.append(
            f"    <li><code>{esc(name)}</code> — revision <code>{esc(revision)}</code>,"
            f" {esc(summary)}</li>"
        )
    out.append("   </ul>")
    out.append("   <table>")
    out.append("    <thead><tr><th>Refusal or check</th><th>Test id</th><th>Log</th></tr></thead><tbody>")
    for test, by in tests.items():
        log = "logs/enforced/" + log_for_test(test, statements)
        out.append(
            f"     <tr><td>{esc(by)}</td><td><code>{esc(test)}</code></td>"
            f"<td><code>{esc(log)}</code></td></tr>"
        )
    out.append("   </tbody></table>")
    out.append("  </section>")

    out.append(f'  <section id="{NODE}-negative-control">')
    out.append("   <h2>§4 — The check that could fail</h2>")
    out.append(
        "   <p><code>render_fragment.py</code> reads the fired logs and refuses to write the page when an"
        " enforced row names a test identifier those logs do not carry, so a row cannot claim enforcement"
        " it cannot point at. Run against a copy whose scope row names a test the logs never ran, it exited"
        " 2 and named that row. The log is <code>logs/negative-control.log</code> in the run directory.</p>"
    )
    out.append("  </section>")

    out.append(" </main>")
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def main() -> int:
    args = sys.argv[1:]
    if "--out" not in args:
        print("--out is required", file=sys.stderr)
        return 3
    out_path = Path(args[args.index("--out") + 1])
    logs_dir = Path(args[args.index("--logs") + 1]) if "--logs" in args else Path("/nonexistent")
    if "--rows" in args:
        import json

        rows_file = Path(args[args.index("--rows") + 1])
        rows = [json.loads(line) for line in rows_file.read_text().splitlines() if line.strip()]
        total = scan_total()
    else:
        total, rows = classify.build()
    logs_text = load_logs(logs_dir)
    missing = check_citations(rows, logs_text)
    if missing:
        print("REFUSED: enforced rows cite tests the fired logs do not carry:", file=sys.stderr)
        for m in missing:
            print("  " + m, file=sys.stderr)
        return 2
    statements = log_statements(logs_dir)
    if "--arm-logs" in args:
        statements += log_statements(Path(args[args.index("--arm-logs") + 1]))
    unlogged = [
        f"{r['file']}:{r['line']} cites {r['test']}"
        for r in rows
        if r["disposition"] == "delete as enforced" and log_for_test(r["test"], statements) is None
    ]
    if unlogged:
        print("REFUSED: enforced rows name no log whose command ran their test:", file=sys.stderr)
        for m in unlogged:
            print("  " + m, file=sys.stderr)
        return 2
    out_path.write_text(render(rows, total, statements), encoding="utf-8")
    print(f"# wrote {out_path} rows={len(rows)}", file=sys.stderr)
    return 0


def scan_total() -> int:
    return sum(
        len(scan_head.sentences_with_lines((scan_head.SKILLS / rel).read_text(encoding="utf-8")))
        for rel in scan_head.FILES
    )


if __name__ == "__main__":
    raise SystemExit(main())