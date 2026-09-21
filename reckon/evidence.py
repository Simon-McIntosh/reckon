"""Compose a landed evidence document from durable execution records."""

from __future__ import annotations

import html
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bs4 import BeautifulSoup

from reckon import _plan_html, ledger
from reckon.resources import ResourceCollision, resolve_resource


class EvidenceSynthesisError(RuntimeError):
    """A landed record cannot be composed from the available durable inputs."""


@dataclass(frozen=True, slots=True)
class EvidenceSynthesisResult:
    """The canonical output and input counts for one synthesis."""

    path: Path
    runs: int
    comments: int
    commits: int


# Manifest fields, in the order they are consulted, that name the durable
# paths a run delivered. A run whose deliverable is a report names it in the
# first of these that carries it, and a run that changed the repository names
# its changed paths in the same fields — so the reader takes the first
# candidate that resolves to a readable file rather than trusting one key.
_REPORT_SOURCE_KEYS = ("artifacts", "orientation_write_paths", "changed_paths")


@dataclass(frozen=True, slots=True)
class _RunReport:
    """The report a commit-less run delivered, or why none could be read."""

    path: Path | None
    content: str
    detail: str


def _section_key(value: object) -> str:
    section = str(value or "").strip()
    match = re.fullmatch(r"(?:§\s*|#?s(?:ection)?\s*)?(\d+(?:\.\d+)*)", section, re.I)
    if match:
        return "s" + match.group(1).replace(".", "-")
    if not section:
        return "_top"
    key = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
    return key or "_top"


def _section_labels(source: str) -> tuple[dict[str, str], list[str]]:
    soup = BeautifulSoup(source, "html.parser")
    labels: dict[str, str] = {}
    order: list[str] = []
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        section_id = str(heading.get("id") or "").strip()
        if not section_id:
            continue
        key = _section_key(section_id)
        if key not in labels:
            labels[key] = heading.get_text(" ", strip=True) or section_id
            order.append(key)
    return labels, order


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _format_seconds(value: object) -> str:
    if value is None:
        return "—"
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return _escape(value)
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder}s" if minutes else f"{remainder}s"


def _format_changed_lines(value: object) -> str:
    if not isinstance(value, Mapping) or not value:
        return "—"
    return _escape(
        json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )


def _run_rows(records: Sequence[Mapping[str, Any]]) -> str:
    rows: list[str] = []
    for record in records:
        commits = [str(item) for item in record.get("commits") or [] if str(item)]
        commit_html = "<br>".join(f"<code>{_escape(item)}</code>" for item in commits)
        if not commit_html:
            commit_html = "—"
        tests_added = record.get("tests_added")
        tests_text = "—" if tests_added is None else _escape(tests_added)
        scope_text = "changed" if record.get("scope_changed") else "unchanged"
        rows.append(
            "      <tr>\n"
            f"        <td><code>{_escape(record.get('run_id'))}</code></td>\n"
            f"        <td>{_escape(record.get('node')) or '—'}</td>\n"
            f"        <td>{commit_html}</td>\n"
            f"        <td><strong>{_escape(record.get('gate'))}</strong></td>\n"
            f"        <td>{tests_text}</td>\n"
            f"        <td>{_format_seconds(record.get('worker_seconds'))}</td>\n"
            f"        <td><code>{_format_changed_lines(record.get('changed_lines'))}</code></td>\n"
            f"        <td>{scope_text}</td>\n"
            "      </tr>"
        )
    return "\n".join(rows)


def _field_paths(value: object) -> list[object]:
    """Return one manifest field's path items, decoding a raw flow sequence.

    A manifest field normally arrives as a list, but a flow sequence a worker
    wrote can survive the parse as one literal string. It is still a structured
    value rather than prose, so it is decoded rather than treated as a single
    path that cannot exist.
    """

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return [value]
            if isinstance(decoded, list):
                return list(decoded)
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _report_candidates(record: Mapping[str, Any], root: Path) -> list[Path]:
    """Return the durable paths a commit-less run's manifest names, in order."""

    manifest_value = str(record.get("manifest_path") or "").strip()
    if not manifest_value:
        return []
    manifest = Path(manifest_value).expanduser()
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []

    from reckon.crew.reports import ManifestParseError, parse_manifest

    try:
        fields = parse_manifest(text, path=str(manifest))
    except ManifestParseError:
        return []

    candidates: list[Path] = []
    for key in _REPORT_SOURCE_KEYS:
        for item in _field_paths(fields.get(key)):
            candidate = Path(str(item)).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            candidates.append(candidate)
    return candidates


def _run_report(record: Mapping[str, Any], root: Path) -> _RunReport:
    """Read the report a commit-less run delivered, or say why it could not be read.

    A run whose deliverable is a report rather than a diff has no commits for
    the ledger row to compose from, so the composer reaches the run's manifest
    for the path and reads the report itself. The detail string is carried
    rather than raised because one unreadable report makes its own run's record
    incomplete, not the whole document's.
    """

    manifest_value = str(record.get("manifest_path") or "").strip()
    if not manifest_value:
        return _RunReport(
            None, "", "it names no manifest, so no report path could be resolved"
        )
    candidates = _report_candidates(record, root)
    if not candidates:
        return _RunReport(
            None,
            "",
            f"its manifest at {manifest_value} names no readable report path",
        )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return _RunReport(
                candidate, "", f"the report at {candidate} could not be read"
            )
        return _RunReport(candidate, content, "")
    named = ", ".join(str(candidate) for candidate in candidates)
    return _RunReport(
        None, "", f"no path in its manifest resolves to a readable file ({named})"
    )


def _report_entries(record: Mapping[str, Any], root: Path) -> str:
    """Render the report-backed block for a run whose deliverable is a report.

    A readable report is cited by path and reproduced, so the section is
    anchored to the artifact rather than to a paraphrase of it. A run with
    neither commits nor a readable report is stated as such in the section,
    because an anchored section with no content reads as work that landed
    nothing rather than as a record that could not be composed.
    """

    run_id = _escape(record.get("run_id"))
    node = _escape(record.get("node")) or run_id
    report = _run_report(record, root)
    if report.path is not None:
        return (
            f'    <article class="landed-report" data-run-id="{run_id}" '
            f'data-report-path="{_escape(report.path)}">\n'
            f"      <h3>{node} &mdash; delivered report</h3>\n"
            f"      <p>Cited from <code>{_escape(report.path)}</code>.</p>\n"
            '      <div class="landed-report-body">'
            f"<pre>{_escape(report.content)}</pre></div>\n"
            "    </article>"
        )
    return (
        f'    <article class="landed-report landed-report-unreadable" '
        f'data-run-id="{run_id}">\n'
        f"      <h3>{node} &mdash; no record composed</h3>\n"
        f"      <p>This run landed no commits and {_escape(report.detail)}, so "
        "no section could be composed from a report. The absence is recorded "
        "here rather than left as an anchored section with no content.</p>\n"
        "    </article>"
    )


def _comment_entries(comments: Sequence[Mapping[str, Any]]) -> str:
    entries: list[str] = []
    for comment in comments:
        identity = _escape(comment.get("id"))
        who = _escape(comment.get("who")) or "unknown"
        when = _escape(comment.get("when")) or "undated"
        body = str(comment.get("body") or "")
        entries.append(
            f'    <article class="landed-comment" data-comment-id="{identity}">\n'
            f"      <p><small>{who} · {when}</small></p>\n"
            f'      <div class="r-comment-body">{body}</div>\n'
            "    </article>"
        )
    return "\n".join(entries)


def _overall_verdict(records: Sequence[Mapping[str, Any]]) -> str:
    verdicts = {str(record.get("gate") or "").strip().lower() for record in records}
    if verdicts == {"passed"}:
        return "pass"
    if "failed" in verdicts:
        return "fail"
    return "qualified"


def _render_document(
    project: str,
    plan: Mapping[str, Any],
    source: str,
    records: Sequence[Mapping[str, Any]],
    root: Path,
) -> str:
    plan_slug = str(plan.get("slug") or "").strip()
    plan_title = str(plan.get("title") or plan_slug).strip()
    comments_by_section = {
        _section_key(section): list(items or [])
        for section, items in (plan.get("comments") or {}).items()
    }
    runs_by_section: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        runs_by_section.setdefault(_section_key(record.get("section")), []).append(
            record
        )

    labels, authored_order = _section_labels(source)
    available = set(comments_by_section) | set(runs_by_section)
    section_order = [key for key in authored_order if key in available]
    section_order.extend(sorted(available - set(section_order)))

    commits = list(
        dict.fromkeys(
            str(commit)
            for record in records
            for commit in (record.get("commits") or [])
            if str(commit).strip()
        )
    )
    completion_times = [
        str(record.get("completed_at"))
        for record in records
        if str(record.get("completed_at") or "").strip()
    ]
    recorded_at = max(completion_times, default="")
    comment_count = sum(len(items) for items in comments_by_section.values())

    sections: list[str] = []
    for key in section_order:
        label = labels.get(key) or ("Plan-level outcome" if key == "_top" else key)
        section_id = "outcome" if key == "_top" else key
        comments = comments_by_section.get(key, [])
        section_runs = runs_by_section.get(key, [])
        parts = [
            f'  <section id="{_escape(section_id)}">',
            f"    <h2>{_escape(label)}</h2>",
        ]
        if comments:
            parts.append(_comment_entries(comments))
        if section_runs:
            parts.extend(
                [
                    "    <table>",
                    "      <thead>",
                    "        <tr><th>Run</th><th>Node</th><th>Commits</th><th>Gate</th><th>Tests added</th><th>Worker time</th><th>Changed lines</th><th>Scope</th></tr>",
                    "      </thead>",
                    "      <tbody>",
                    _run_rows(section_runs),
                    "      </tbody>",
                    "    </table>",
                ]
            )
        # A run with no commits composed nothing from the ledger row, so its
        # record comes from the report it delivered. A run with commits keeps
        # composing from the row alone, which is why the report reader is
        # reached only for the commit-less records.
        parts.extend(
            _report_entries(record, root)
            for record in section_runs
            if not record.get("commits")
        )
        parts.append("  </section>")
        sections.append("\n".join(parts))

    summary = (
        f"Closure synthesis for {plan_title}: {len(records)} committed run(s), "
        f"{comment_count} section-anchored comment(s), and {len(commits)} landed commit(s)."
    )
    meta_lines = [
        f'  <meta name="docs-project" content="{_escape(project)}">',
        '  <meta name="reckon-type" content="evidence">',
        f'  <meta name="plan-slug" content="{_escape(plan_slug)}-landed">',
        f'  <meta name="plan-title" content="{_escape(plan_title)} — landed record">',
        f'  <meta name="plan-summary" content="{_escape(summary)}">',
        f'  <meta name="plan-evidence-for" content="{_escape(plan_slug)}">',
        f'  <meta name="plan-verdict" content="{_overall_verdict(records)}">',
    ]
    if recorded_at:
        meta_lines.append(
            f'  <meta name="plan-recorded-at" content="{_escape(recorded_at)}">'
        )
    if commits:
        meta_lines.append(
            f'  <meta name="plan-commits" content="{_escape(",".join(commits))}">'
        )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + "\n".join(meta_lines)
        + f"\n  <title>{_escape(plan_title)} — landed record | {_escape(project)}</title>\n"
        '  <link rel="stylesheet" href="/_shared/foundation.css">\n'
        '  <link rel="stylesheet" href="/_shared/dashboard.css">\n'
        "</head>\n"
        "<body>\n"
        ' <main class="plan-doc">\n'
        f"  <h1>{_escape(plan_title)} — landed record</h1>\n"
        f"  <p>{_escape(summary)}</p>\n"
        "\n" + "\n\n".join(sections) + "\n </main>\n"
        "</body>\n"
        "</html>\n"
    )


def synthesize_landed_record(
    docs_dir: Path,
    project: str,
    plan_slug: str,
) -> EvidenceSynthesisResult:
    """Replace the canonical landed record from plan comments and ledger runs."""

    docs_dir = docs_dir.resolve()
    try:
        resource = resolve_resource(
            docs_dir, project, plan_slug, artifact_type="plan", include_archived=False
        )
    except ResourceCollision as exc:
        raise EvidenceSynthesisError(str(exc)) from exc
    if resource is None:
        raise EvidenceSynthesisError(
            f"plan {plan_slug!r} was not found in project {project!r}"
        )

    source = resource.path.read_text(encoding="utf-8")
    plan = _plan_html.parse_plan(resource.path)
    try:
        records = ledger.runs(project, root=docs_dir.parent, plan=plan_slug)
    except ledger.LedgerError as exc:
        raise EvidenceSynthesisError(str(exc)) from exc
    if not records:
        raise EvidenceSynthesisError(
            f"plan {plan_slug!r} has no committed landed runs; "
            "cannot synthesize a closure evidence record"
        )
    records = sorted(
        records,
        key=lambda record: (
            str(record.get("completed_at") or ""),
            str(record.get("run_id") or ""),
        ),
    )
    rendered = _render_document(project, plan, source, records, docs_dir.parent)
    destination = docs_dir / "evidence" / "archive" / f"{plan_slug}-landed.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(rendered)
            temporary = Path(handle.name)
        temporary.replace(destination)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise EvidenceSynthesisError(
            f"cannot write landed evidence record {destination}: {exc}"
        ) from exc

    comments = sum(len(items or []) for items in (plan.get("comments") or {}).values())
    commits = len(
        {
            str(commit)
            for record in records
            for commit in (record.get("commits") or [])
            if str(commit).strip()
        }
    )
    return EvidenceSynthesisResult(destination, len(records), comments, commits)
