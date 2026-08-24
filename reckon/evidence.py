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
    rendered = _render_document(project, plan, source, records)
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
