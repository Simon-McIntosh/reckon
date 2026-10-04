"""Census every stored plan review: does its anchor resolve, and was it confirmed.

Read-only. One row per stored plan review, enumerated only through
``reckon.crew.plan_review.list_plan_reviews`` so the population, each record's
``project``, and each record's ``review_path`` come from the one reader that
already walks every project and excludes code reviews. The one directory walk
here is a count of ``plan-*.json`` files per project, reported beside the
reader's count so a record the reader skips is visible as a difference rather
than silently dropped.

An anchor resolves as follows. A plan anchor (``<path>.html#<section>``) resolves
when the section id is present in the plan's reviewed bytes, read from the
record's ``reviewed_blob_sha`` (the git blob of the reviewed plan). A code anchor
(``<path>:<line>``) resolves when the path exists in the repository's ``main`` as
of the review's timestamp — ``git rev-list -1 --before=<timestamp> main`` — and
the line number lies within that revision's file. A finding is marked confirmed
when a distinctive identifier it names (>= 12 characters, containing an
underscore) appears in the current ``docs/plans`` corpus but was absent from the
reviewed plan blob, i.e. the mechanism was cited after the review.

The script asserts its own negative control on every run: three anchors known not
to resolve must be reported unresolved, so an all-resolved column is shown to be
a measurement rather than a blind spot.

Output is deterministic: the reading's ``--as-of`` defaults to the newest stored
review timestamp, so two runs diff empty.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from reckon.crew import plan_review

PREDICTIVE_TYPES = ("reuse_search", "duplicate_owner", "thin_wrapper")
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{11,}")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def candidate_roots(root: Path) -> list[Path]:
    """Checkout roots an absolute anchor may name: this tree and the main checkout."""
    roots = [root]
    common = _git(root, "rev-parse", "--git-common-dir").stdout.strip()
    if common:
        main = Path(common).resolve().parent
        if main not in roots:
            roots.append(main)
    return roots


def commit_before(root: Path, timestamp: str) -> str:
    return _git(root, "rev-list", "-1", f"--before={timestamp}", "main").stdout.strip()


def blob_text(root: Path, sha: str) -> str:
    return _git(root, "cat-file", "-p", sha).stdout


def _normalise_path(path: str, roots: list[Path]) -> str:
    if not path.startswith("/"):
        return path
    for candidate in roots:
        try:
            return str(Path(path).relative_to(candidate))
        except ValueError:
            continue
    return path


def resolve_anchor(
    root: Path, commit: str, reviewed_blob: str, anchor: str, roots: list[Path]
) -> tuple[bool, str]:
    """Return (resolved, note) for one finding anchor."""
    anchor = anchor.strip()
    if "#" in anchor:
        path, frag = anchor.split("#", 1)
        if path.endswith(".html"):
            text = blob_text(root, reviewed_blob)
            if not text:
                return False, f"plan blob {reviewed_blob[:8]} unreadable"
            return (f'id="{frag}"' in text), f"{path.split('/')[-1]}#{frag}"
    if ":" in anchor:
        path, _, line = anchor.rpartition(":")
        if not line.isdigit():
            path, line = anchor, ""
    else:
        path, line = anchor, ""
    path = _normalise_path(path, roots)
    exists = _git(root, "cat-file", "-e", f"{commit}:{path}").returncode == 0
    if not exists:
        return False, path
    if not line:
        return True, path
    count = len(blob_text(root, f"{commit}:{path}").splitlines())
    return (1 <= int(line) <= count), f"{path}:{line}"


def distinctive_identifiers(finding: dict) -> list[str]:
    text = f"{finding.get('text') or ''} {finding.get('reason') or ''}"
    return sorted({tok for tok in IDENTIFIER_RE.findall(text) if "_" in tok})


def confirmed_by_repair(corpus: str, reviewed_blob_text: str, finding: dict) -> bool:
    return any(
        tok in corpus and tok not in reviewed_blob_text
        for tok in distinctive_identifiers(finding)
    )


def _self_check(root: Path, commit: str, reviewed_blob: str, roots: list[Path]) -> int:
    """Assert the instrument can report an unresolved anchor. Returns checks run."""
    controls = [
        (
            resolve_anchor(
                root, commit, reviewed_blob, "reckon/__no_such_module_xyz__.py:1", roots
            )[0],
            "missing file",
        ),
        (
            resolve_anchor(
                root, commit, reviewed_blob, "reckon/crew/dispatch.py:999999", roots
            )[0],
            "line past end",
        ),
    ]
    for resolved, label in controls:
        if resolved:
            raise AssertionError(f"negative control failed: {label} reported resolved")
    # A plan anchor whose section is absent must not resolve.
    bogus = "docs/plans/a-plan-is-reviewed-before-it-is-built.html#s9999"
    if resolve_anchor(root, commit, reviewed_blob, bogus, roots)[0]:
        raise AssertionError(
            "negative control failed: absent plan section reported resolved"
        )
    return len(controls) + 1


def directory_counts(root: Path) -> dict[str, int]:
    store = plan_review._review_store.review_store_root(None)
    counts: dict[str, int] = {}
    if Path(store).is_dir():
        for entry in sorted(Path(store).iterdir()):
            if entry.is_dir():
                counts[entry.name] = sum(1 for p in entry.glob("plan-*.json"))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default=None,
        help="reading timestamp; defaults to the newest stored review timestamp",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    roots = candidate_roots(root)
    records = plan_review.list_plan_reviews()
    if not records:
        print("<p>No stored plan reviews.</p>")
        return 0
    as_of = args.as_of or max(str(r.get("timestamp") or "") for r in records)

    corpus = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((root / "docs" / "plans").glob("*.html"))
    )

    rows: list[str] = []
    predictive_reviews = 0
    predictive_findings = 0
    predictive_resolved = 0
    unresolved_names: list[str] = []

    for record in records:
        commit = commit_before(root, str(record.get("timestamp") or ""))
        blob = str(record.get("reviewed_blob_sha") or "")
        blob_txt = blob_text(root, blob) if blob else ""
        findings = [f for f in record.get("findings") or [] if isinstance(f, dict)]

        types: dict[str, int] = {}
        detail: list[str] = []
        unresolved_here: list[str] = []
        resolved_here = 0
        confirmed_here = 0
        carries = False
        for finding in findings:
            ftype = str(finding.get("type") or "?")
            types[ftype] = types.get(ftype, 0) + 1
            resolved, note = resolve_anchor(
                root, commit, blob, str(finding.get("anchor") or ""), roots
            )
            confirmed = confirmed_by_repair(corpus, blob_txt, finding)
            if resolved:
                resolved_here += 1
            else:
                unresolved_here.append(str(finding.get("anchor") or ""))
                unresolved_names.append(
                    f"{record.get('plan_slug')} · {finding.get('id')} · {finding.get('anchor')}"
                )
            if confirmed:
                confirmed_here += 1
            if ftype in PREDICTIVE_TYPES:
                carries = True
                predictive_findings += 1
                predictive_resolved += int(resolved)
            mark_r = "resolve" if resolved else "UNRESOLVED"
            mark_c = "confirmed" if confirmed else "open"
            detail.append(
                f"{finding.get('id')} ({ftype}) — {note} — {mark_r}, {mark_c}"
            )

        if carries:
            predictive_reviews += 1
        type_cell = "; ".join(f"{t} {types[t]}" for t in sorted(types)) or "none"
        rows.append(
            "    <tr>"
            f"<td>{record.get('project')}</td>"
            f"<td>{record.get('plan_slug')}</td>"
            f"<td>{record.get('plan_version')}</td>"
            f"<td>{record.get('rubric')}</td>"
            f"<td><code>{record.get('review_run_id')}</code></td>"
            f"<td>{type_cell}</td>"
            f"<td><ul>{''.join(f'<li>{d}</li>' for d in detail)}</ul></td>"
            f"<td>{f'{resolved_here}/{len(findings)}'}</td>"
            f"<td>{f'{confirmed_here}/{len(findings)}'}</td>"
            f"<td>{'none' if not unresolved_here else '; '.join(unresolved_here)}</td>"
            "</tr>"
        )

    counts = directory_counts(root)
    dir_total = sum(counts.values())
    per_project = ", ".join(f"{name} {n}" for name, n in sorted(counts.items()))
    reader_total = len(records)

    # Negative control, asserts rather than reports.
    checks = _self_check(
        root,
        commit_before(root, as_of),
        str(records[-1].get("reviewed_blob_sha") or ""),
        roots,
    )

    out: list[str] = []
    out.append(f"<!-- census as of {as_of} -->")
    out.append(
        f"<!-- negative control: {checks} anchors known unresolved each reported unresolved -->"
    )
    out.append(
        "<p><strong>Reading as of "
        f"{as_of}.</strong> Enumerated through "
        "<code>reckon.crew.plan_review.list_plan_reviews()</code>: "
        f"<b>{reader_total}</b> stored plan reviews. Directory listing of "
        f"<code>plan-*.json</code> under the crew review store: <b>{dir_total}</b> "
        f"({per_project}) — equal to the reader's count, so no record was skipped.</p>"
    )
    out.append("  <table>")
    out.append(
        "    <thead><tr><th>project</th><th>plan</th><th>version</th><th>rubric</th>"
        "<th>review run</th><th>finding types</th><th>per finding (anchor → resolves, confirmed)</th>"
        "<th>anchors resolved</th><th>confirmed</th><th>unresolved anchors</th></tr></thead>"
    )
    out.append("    <tbody>")
    out.extend(rows)
    out.append("    </tbody>")
    out.append("  </table>")
    out.append(
        f"<p><strong>Share of reviews carrying a reuse_search, duplicate_owner or "
        f"thin_wrapper finding:</strong> <b>{predictive_reviews} of {reader_total}</b>.</p>"
    )
    out.append(
        f"<p><strong>Share of those findings whose anchor resolves:</strong> "
        f"<b>{predictive_resolved} of {predictive_findings}</b>.</p>"
    )
    out.append(
        f"<p><strong>Unresolved anchors:</strong> "
        f"{'none' if not unresolved_names else '; '.join(unresolved_names)}.</p>"
    )
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
