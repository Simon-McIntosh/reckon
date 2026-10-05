"""Census every stored plan review: does its anchor resolve, and was it confirmed.

Read-only. One row per stored plan review, enumerated only through
``reckon.crew.plan_review.list_plan_reviews`` so the population, each record's
``project``, and each record's ``review_path`` come from the one reader that
already walks every project and excludes code reviews. Beside the reader's own
count the module walks each project directory for a broad ``plan-*.json`` count,
so a stored file the reader does not enumerate is visible as a difference rather
than silently dropped; the reader's pattern is imported from
``reckon.crew.plan_review`` and is not restated here.

An anchor resolves as follows. A plan anchor (``<path>.html#<section>``) resolves
when the section id is present in the bytes of the document the anchor's own path
names, at the review's commit; an anchor naming no path falls back to the
reviewed plan's ``reviewed_blob_sha`` blob. A code anchor (``<path>:<line>``)
resolves when the path exists in the repository's ``main`` as of the review's
timestamp — ``git rev-list -1 --before=<timestamp> main`` — and the line number
lies within that revision's file. A finding is marked confirmed when a
distinctive identifier it names (>= 12 characters, containing an underscore)
appears in the current reviewed plan but was absent from the reviewed plan blob,
i.e. the mechanism was cited after the review; the id of the section or comment
that carries it is recorded so the cell can be checked.

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
        if not path or path.endswith(".html"):
            if path:
                target = _normalise_path(path, roots)
                text = blob_text(root, f"{commit}:{target}")
                note = f"{path.split('/')[-1]}#{frag}"
            else:
                text = blob_text(root, reviewed_blob)
                note = f"#{frag}"
            if not text:
                return False, f"plan {note} unreadable"
            return (f'id="{frag}"' in text), note
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


_ID_ATTR_RE = re.compile(r'(?:data-)?id="([^"]+)"')


def confirming_location(plan_html: str, identifier: str) -> str | None:
    """Return the id of the section or comment that first carries ``identifier``."""
    index = plan_html.find(identifier)
    if index < 0:
        return None
    location: str | None = None
    for match in _ID_ATTR_RE.finditer(plan_html, 0, index):
        location = match.group(1)
    return location


def confirmed_by_repair(
    plan_html: str, reviewed_blob_text: str, finding: dict
) -> tuple[bool, str]:
    """Confirm a finding within the reviewed plan, naming the confirming id.

    Scoped to the reviewed plan rather than the whole ``docs/plans`` corpus, so
    an identifier that also appears in an unrelated plan cannot mark the cut. A
    distinctive identifier the finding names, present in the current plan but
    absent from the reviewed bytes, shows the repair was recorded after the
    review; the id of the section or comment carrying it is returned beside the
    flag, empty when no enclosing id is found.
    """
    for token in distinctive_identifiers(finding):
        if token in plan_html and token not in reviewed_blob_text:
            return True, (confirming_location(plan_html, token) or "")
    return False, ""


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


def directory_counts(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Count store files per project under the broad and the reader's patterns.

    The broad pattern is every ``plan-*.json`` a project directory holds; the
    reader's pattern is the store's own :data:`PLAN_REVIEW_FILE_GLOB`, imported
    from the module that owns the filename grammar. Reporting both makes a
    stored file the reader does not enumerate — a skipped or non-versioned one —
    visible as a difference between the two counts rather than silently dropped.
    """
    store = plan_review._review_store.review_store_root(None)
    broad: dict[str, int] = {}
    reader: dict[str, int] = {}
    if Path(store).is_dir():
        for entry in sorted(Path(store).iterdir()):
            if entry.is_dir():
                broad[entry.name] = sum(1 for _ in entry.glob("plan-*.json"))
                reader[entry.name] = sum(
                    1 for _ in entry.glob(plan_review.PLAN_REVIEW_FILE_GLOB)
                )
    return broad, reader


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

    rows: list[str] = []
    predictive_reviews = 0
    predictive_findings = 0
    predictive_resolved = 0
    unresolved_names: list[str] = []

    for record in records:
        commit = commit_before(root, str(record.get("timestamp") or ""))
        blob = str(record.get("reviewed_blob_sha") or "")
        blob_txt = blob_text(root, blob) if blob else ""
        slug = str(record.get("plan_slug") or "")
        plan_file = root / "docs" / "plans" / f"{slug}.html"
        plan_html = plan_file.read_text(encoding="utf-8") if plan_file.is_file() else ""
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
            confirmed, confirm_loc = confirmed_by_repair(plan_html, blob_txt, finding)
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
            if confirmed:
                where = (
                    f"{plan_file.name}#{confirm_loc}" if confirm_loc else plan_file.name
                )
                mark_c = f"confirmed at {where}"
            else:
                mark_c = "open"
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

    counts_broad, counts_reader = directory_counts(root)
    broad_total = sum(counts_broad.values())
    reader_pattern_total = sum(counts_reader.values())
    per_project_broad = ", ".join(
        f"{name} {n}" for name, n in sorted(counts_broad.items())
    )
    per_project_reader = ", ".join(
        f"{name} {n}" for name, n in sorted(counts_reader.items())
    )
    reader_total = len(records)

    # Negative control, asserts rather than reports.
    checks = _self_check(
        root,
        commit_before(root, as_of),
        str(records[-1].get("reviewed_blob_sha") or ""),
        roots,
    )

    if broad_total == reader_total:
        crosscheck = (
            f"Broad <b>{broad_total}</b> equals the reader's <b>{reader_total}</b> "
            "rows, so no stored record was skipped."
        )
    else:
        crosscheck = (
            f"Broad <b>{broad_total}</b> differs from the reader's "
            f"<b>{reader_total}</b> rows by <b>{broad_total - reader_total}</b>: a "
            "stored plan file the reader does not enumerate."
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
        f"<b>{reader_total}</b> stored plan reviews. Directory cross-check under "
        f"the crew review store: broad <code>plan-*.json</code> listing "
        f"<b>{broad_total}</b> ({per_project_broad}); the reader's own pattern "
        f"<code>{plan_review.PLAN_REVIEW_FILE_GLOB}</code> listing "
        f"<b>{reader_pattern_total}</b> ({per_project_reader}). {crosscheck}</p>"
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
