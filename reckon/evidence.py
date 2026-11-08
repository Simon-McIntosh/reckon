"""Compose a landed evidence document from durable execution records."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from bs4 import BeautifulSoup
from bs4.element import Doctype

from reckon import _plan_html, ledger
from reckon.resources import ResourceCollision, resolve_resource


class EvidenceSynthesisError(RuntimeError):
    """A landed record cannot be composed from the available durable inputs."""


@dataclass(frozen=True, slots=True)
class EvidenceSynthesisResult:
    """The canonical output and input counts for one synthesis.

    ``lost_anchors`` names every anchored section of the record the old and new
    documents disagree about, as ``(tag, anchor)`` pairs. A record a reader
    links to loses an anchor silently unless the rewrite says so, so each entry
    carries why it was not carried: ``dropped`` an appended anchor the
    synthesis now renders itself, ``replaced`` a section a rendered one took the
    place of, ``retired`` a section on a reserved id this synthesis does not
    render, and ``carried`` a section kept because it cannot be told apart from
    an append.
    """

    path: Path
    runs: int
    comments: int
    commits: int
    lost_anchors: list[tuple[str, str]] = field(default_factory=list)


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
    match = re.fullmatch(
        rf"(?:§\s*|#?s(?:ection)?\s*)?({_plan_html.SECTION_NUMBER_PATTERN})",
        section,
        re.IGNORECASE,
    )
    if match:
        return _plan_html.section_record_id(section)
    if not section:
        return _plan_html.section_anchor(section)
    key = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
    return key or _plan_html.section_anchor(None)


#: The attribute an evidence append writes on the section it creates. Without
#: it a later synthesis cannot tell an appended anchor from one of its own
#: sections, and rewrites the first as if it had generated it.
APPEND_ORIGIN_ATTRIBUTE = "data-evidence-origin"
APPEND_ORIGIN_VALUE = "append"

#: The section id a plan-level key renders as, and the id the closing section
#: takes. Both are reserved for the synthesis, so no append may claim them.
_PLAN_LEVEL_KEY = "_top"
_OUTCOME_SECTION_ID = "outcome"


def _section_element_id(key: str) -> str:
    """The element id the record writes for one rendered section key."""
    return _OUTCOME_SECTION_ID if key == _PLAN_LEVEL_KEY else key


def _element_section_key(element_id: str) -> str:
    """The rendered section key an existing record's element id addresses."""
    return (
        _PLAN_LEVEL_KEY
        if element_id == _OUTCOME_SECTION_ID
        else _section_key(element_id)
    )


def _evidence_record_path(docs_dir: Path, plan_slug: str) -> Path:
    """The cumulative landing record one plan's evidence appends land in."""
    return docs_dir / "evidence" / "archive" / f"{plan_slug}-landed.html"


def _evidence_anchor_sets(
    plan: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    source: str,
) -> tuple[set[str], set[str]]:
    """The section keys a synthesis writes now, and every key it may write later.

    The rendered set is what this synthesis renders: the plan's comment keys
    together with the current ledger runs' section keys. A declared plan
    section carrying no comment and no run is not in it, so an id only the plan
    declares is free *today* and is still reserved, because the next comment or
    run on it makes the synthesis claim the id and a section already sitting
    there would be overwritten.

    Every key is normalised through :func:`_section_key`, so a caller may spell
    a section as an authored id, a section sign or prose numbering and address
    the same section. The synthesis, the carry-forward and the append refusal
    all read this function, so the three cannot disagree about which ids are
    the record's own.
    """
    rendered_keys = {_section_key(key) for key in (plan.get("comments") or {})}
    rendered_keys.update(_section_key(record.get("section")) for record in records)
    rendered = {_section_element_id(key) for key in rendered_keys}

    labels, _order = _section_labels(source)
    declared = {
        _section_key(section.get("id"))
        for section in (plan.get("sections") or [])
        if isinstance(section, Mapping)
    }
    reserved = (
        set(rendered) | set(labels) | declared | {_OUTCOME_SECTION_ID, _PLAN_LEVEL_KEY}
    )
    return rendered, reserved


def _record_sections(html_text: str) -> list[tuple[str, str, bool]]:
    """Return each anchored section a record holds, in document order.

    The triple is the section's element id, its markup, and whether an append
    wrote it. Only top-level sections of the document are read: a section
    nested inside another is part of its parent's content and is carried with
    it rather than on its own.
    """
    if not html_text.strip():
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    container = soup.find("main") or soup.find("body") or soup
    found: list[tuple[str, str, bool]] = []
    for child in container.children:
        if getattr(child, "name", None) != "section":
            continue
        element_id = str(child.get("id") or "").strip()
        if not element_id:
            continue
        marked = (
            str(child.get(APPEND_ORIGIN_ATTRIBUTE) or "").strip().lower()
            == APPEND_ORIGIN_VALUE
        )
        found.append((element_id, str(child), marked))
    return found


def _section_labels(source: str) -> tuple[dict[str, str], list[str]]:
    labels: dict[str, str] = {}
    order: list[str] = []
    for heading in _plan_html.plan_headings(source):
        section_id = str(heading.raw_id or "").strip()
        if not section_id:
            continue
        key = _section_key(section_id)
        if key not in labels:
            labels[key] = heading.text or section_id
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


#: How the per-section verdicts rank: the record takes the worst reading.
_VERDICT_SEVERITY = {"pass": 0, "qualified": 1, "fail": 2}

#: The roles whose runs decide a section's verdict. A review or an
#: investigation records its gate as not-run by design, so counting one would
#: deny every reviewed plan a passing record; a section attempt count reads
#: this same population.
EXECUTABLE_SECTION_ROLES = frozenset({"implement", "test"})


def _run_verdict(record: Mapping[str, Any]) -> str:
    """The verdict one implementing run earned from its gate and classification."""
    gate = str(record.get("gate") or "").strip().lower()
    if gate == "passed":
        return "pass"
    if (
        gate == "failed"
        and str(record.get("failure_classification") or "").strip().lower()
        != "pre-existing-failure"
    ):
        return "fail"
    return "qualified"


def _overall_verdict(records: Sequence[Mapping[str, Any]]) -> str:
    """The worst section verdict over each section's latest implementing run.

    The population and the section identity are the ones a section attempt
    count already derives: implement- and test-role runs, addressed by
    :func:`reckon._plan_html.section_record_id`. Within a section the latest
    run decides — ordered by completion, then run id, as the record's own run
    tables are — so a failure a later run repaired no longer holds the
    section. A not-run gate qualifies the section rather than failing it,
    because the gate's evidence was not produced rather than produced and red.
    """
    from reckon._plan_html import section_record_id

    latest: dict[str, tuple[tuple[str, str], str]] = {}
    for record in records:
        if str(record.get("role") or "") not in EXECUTABLE_SECTION_ROLES:
            continue
        section = section_record_id(record.get("section"))
        if not section:
            continue
        order = (
            str(record.get("completed_at") or ""),
            str(record.get("run_id") or ""),
        )
        current = latest.get(section)
        if current is None or order >= current[0]:
            latest[section] = (order, _run_verdict(record))
    if not latest:
        return "qualified"
    return max(
        (verdict for _order, verdict in latest.values()),
        key=lambda verdict: _VERDICT_SEVERITY[verdict],
    )


def _render_document(
    project: str,
    plan: Mapping[str, Any],
    source: str,
    records: Sequence[Mapping[str, Any]],
    root: Path,
    created_by_append: bool = False,
    carried_sections: Sequence[str] = (),
) -> str:
    """Render a landed record.

    ``created_by_append`` says this document is the empty frame an
    :func:`append_evidence` creates before any section lands in it. Such a
    frame has read nothing, so it states no run, comment or commit count and
    no verdict — the counts a synthesis computes are the only ones the record
    may publish.

    ``carried_sections`` are anchored sections of the record being replaced
    that this synthesis does not render itself, kept verbatim after the
    rendered ones. Their anchors are what a link into the record resolves
    against, so dropping one turns a live link into a dead one.
    """
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
        section_id = _section_element_id(key)
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
    sections.extend(carried_sections)

    if created_by_append:
        summary = f"Landing record for {plan_title}: evidence appended by section."
    else:
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
    ]
    if not created_by_append:
        meta_lines.append(
            f'  <meta name="plan-verdict" content="{_overall_verdict(records)}">'
        )
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


#: The ``reckon-type`` a cumulative evidence record declares.
_RECORD_TYPE = "evidence"


def evidence_record_plan(path: Path) -> str | None:
    """Return the plan a cumulative evidence record documents, else ``None``.

    The document's own metas decide, not its filename or its directory: a
    record declares a ``reckon-type`` of ``evidence`` and a ``plan-evidence-for``
    naming the plan whose execution it records. A file merely named like a
    record but carrying no such metas is an ordinary document, and a record is
    one wherever it happens to be written.

    ``plan-evidence-for`` is a typed plan reference — a bare slug, a
    ``<project>:<slug>`` reference, or either with a ``#<section>`` suffix — so
    the plan it names is the slug the last path segment resolves to. The SPA
    resolves a record's ``evidence_for`` the same way, so the plan composed
    here is the plan the surface links the record to. When the meta names more
    than one plan, the first is the record's own.
    """

    if path.suffix != ".html":
        # Metas live in an HTML document; a non-document cannot be a record.
        # This bounds the read for the file route, which sees every asset.
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return record_plan_in_text(text)


def record_plan_in_text(text: str) -> str | None:
    """Return the plan a document's own evidence-record metas name, else None."""

    soup = BeautifulSoup(text or "", "html.parser")

    def meta(name: str) -> str:
        tag = soup.find("meta", attrs={"name": name})
        return ((tag.get("content") if tag else "") or "").strip()

    if meta("reckon-type").lower() != _RECORD_TYPE:
        return None
    reference = meta("plan-evidence-for").split(",")[0].strip()
    if not reference:
        return None
    plan = reference.split("#", 1)[0].rsplit(":", 1)[-1].strip()
    return plan or None


def _record_evidence_dir(record_path: Path) -> Path:
    """Return the ``docs/evidence`` directory a record path lives under.

    A record is spelled either at the archived path
    ``docs/evidence/archive/<plan>-landed.html`` or at the live path
    ``docs/evidence/<plan>-landed.html``. Fragments live beside both spellings
    rather than beneath either, so the directory is resolved from the record's
    position instead of assumed.
    """

    parent = record_path.parent
    return parent.parent if parent.name == "archive" else parent


def _fragment_body_bytes(fragment: Path) -> bytes:
    """Return a fragment's readable content, without its document wrapper.

    A fragment is written as a full HTML document, so appending its bytes whole
    would carry its doctype, ``head`` and ``html`` element into the record — and
    with its ``head``, the ``plan-*`` metas it holds, which would then read as
    duplicates of the record's own scalars. The composed record takes the
    content a reader is meant to see: the children of the fragment's ``main``
    element when it has one, else the children of its ``body``.

    A fragment can carry a document wrapper without ever naming a ``body``: the
    content sits directly under ``html``. For that shape the wrapper is dropped
    a level higher — the doctype, the whole ``head`` element and the ``html``
    open and close tags are removed and what remains is composed, so the metas
    the ``head`` held still cannot re-enter the record.

    A fragment with none of those — a bare fragment anchor — is returned
    unchanged, so a fragment written before the document wrapper existed
    composes exactly as it did.
    """

    raw = fragment.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    body = soup.find("body")
    main = soup.find("main")
    if body is not None or main is not None:
        container = main if main is not None else body
        return "".join(str(child) for child in container.children).encode("utf-8")

    html_element = soup.find("html")
    head = soup.find("head")
    if html_element is None and head is None and not _carries_doctype(soup):
        return raw
    scope = html_element if html_element is not None else soup
    return "".join(
        str(child)
        for child in scope.children
        if getattr(child, "name", None) != "head" and not isinstance(child, Doctype)
    ).encode("utf-8")


def _carries_doctype(soup: BeautifulSoup) -> bool:
    """Return whether a parsed document holds a doctype declaration node."""

    return soup.find(string=lambda node: isinstance(node, Doctype)) is not None


def compose_landed_record(
    record_path: Path,
    plan_slug: str,
    *,
    project: str,
    root: Path | None = None,
) -> bytes:
    """Compose a plan's cumulative evidence record from its file and fragments.

    The composed form is the record file's own bytes followed by each fragment
    under ``docs/evidence/fragments/<plan>/`` in the order the ledger records
    the fragments' promotions. Only a fragment's body content is appended, not
    its document wrapper, so a fragment's own ``head`` metas cannot duplicate
    the record's scalars. A fragment is named for the ledger row's
    ``node``, so a redispatch of the same node replaces its predecessor's
    fragment and the composed record carries that node's latest fragment once,
    at its earliest promotion.

    A fragment whose node has no row for this plan is composed after the
    ledgered ones, in filename order. A coordinator merges a fragment before it
    promotes the run, so between the merge and completion the fragment has no
    recorded promotion; dropping it would hide merged evidence, and a run never
    promoted would hide it for good. Only this plan's fragment directory is
    read, so another plan's row cannot pull this plan's fragment in.

    The reader writes no file and never re-renders the document. A record with
    no fragment directory, or a fragment directory holding no fragment that
    exists, composes to the record's own bytes — so a record written before
    fragments existed is returned byte-identical rather than rewritten.

    ``root`` names the checkout whose ledger holds the promotions; without it
    the checkout containing the record is used, which is where the fragments
    are read from as well.
    """

    record_path = Path(record_path)
    evidence_dir = _record_evidence_dir(record_path)
    fragment_dir = evidence_dir / "fragments" / plan_slug
    composed = bytearray(record_path.read_bytes())
    if not fragment_dir.is_dir():
        return bytes(composed)

    ledger_root = root if root is not None else evidence_dir.parent.parent
    try:
        rows = ledger.runs(project, root=ledger_root, plan=plan_slug)
    except ledger.LedgerError as exc:
        raise EvidenceSynthesisError(
            f"cannot read the ledger for project {project!r}: {exc}"
        ) from exc

    ordered: list[Path] = []
    seen: set[str] = set()
    for row in rows:
        node = str(row.get("node") or "").strip()
        if not node or node in seen:
            continue
        seen.add(node)
        fragment = fragment_dir / f"{node}.html"
        if fragment.is_file():
            ordered.append(fragment)

    # Fragments merged before their run was promoted carry no recorded
    # promotion, so they are composed after the ledgered ones rather than
    # hidden. Filename order makes the result deterministic and independent of
    # directory iteration order.
    ordered.extend(
        fragment
        for fragment in sorted(fragment_dir.glob("*.html"))
        if fragment.name[: -len(".html")] not in seen
    )

    for fragment in ordered:
        composed.extend(_fragment_body_bytes(fragment))
    return bytes(composed)


def _carried_sections(
    html_text: str,
    rendered: set[str],
    reserved: set[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Split a record's existing sections into what a rewrite keeps and loses.

    A rewrite keeps an appended section whose anchor this synthesis does not
    render, because the anchor is what a link into the record resolves against
    and the synthesis never composed that content. What it does not keep is
    named rather than dropped quietly:

    ``dropped``
        a marked append whose id a ledger run or a comment now renders — the
        synthesis writes that section itself, so the appended copy goes.
    ``replaced``
        an unrendered section on a rendered id — the synthesis's own section
        takes the place of a section written before the mark existed.
    ``retired``
        an unrendered section on a reserved id — a plan section that carries no
        comment and no run now, so nothing renders it and nothing may silently
        keep it either.
    ``carried``
        a section kept because it cannot be told from an append: a record
        written before the mark gives no way to distinguish a generated
        section whose plan section was deleted from content a person wrote.
        It is carried so nothing is lost, and listed so an operator can remove
        it.
    """
    carried: list[str] = []
    lost: list[tuple[str, str]] = []
    for element_id, markup, marked in _record_sections(html_text):
        key = _element_section_key(element_id)
        if key in rendered:
            lost.append(("dropped" if marked else "replaced", element_id))
            continue
        if marked:
            carried.append(markup)
            continue
        if key in reserved:
            lost.append(("retired", element_id))
            continue
        carried.append(markup)
        lost.append(("carried", element_id))
    return carried, lost


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
    destination = _evidence_record_path(docs_dir, plan_slug)
    existing = ""
    if destination.is_file():
        try:
            existing = destination.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    rendered_anchors, reserved_anchors = _evidence_anchor_sets(plan, records, source)
    carried, lost = _carried_sections(existing, rendered_anchors, reserved_anchors)

    rendered = _render_document(
        project, plan, source, records, docs_dir.parent, carried_sections=carried
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    from reckon._store import write_atomically

    try:
        write_atomically(
            destination, lambda handle: handle.write(rendered), fsync=False, mode=0o600
        )
    except OSError as exc:
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
    return EvidenceSynthesisResult(
        destination, len(records), comments, commits, lost_anchors=lost
    )
