"""Compose a landed evidence document from durable execution records."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Doctype

from reckon import _plan_html, ledger
from reckon.figures import figure_row
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

#: The origin an overview block writes, so a re-synthesis replaces its own
#: reading instead of carrying the copy already on disk forward as an append.
OVERVIEW_ORIGIN_VALUE = "overview"

#: The class every source element carries. A factual sentence in the overview
#: states which durable record it rests on through one of these, so a reader
#: can follow a claim to the run, comment, figure or gate it came from.
SOURCE_CLASS = "source"

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


def _record_sections(html_text: str) -> list[tuple[str, str, str]]:
    """Return each anchored section a record holds, in document order.

    The triple is the section's element id, its markup, and the origin that
    wrote it — ``append`` for an appended anchor, ``overview`` for a block this
    module owns and re-renders, and the empty string for a section written by
    neither. Only top-level sections of the document are read: a section
    nested inside another is part of its own parent's content and is carried with
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
        origin = str(child.get(APPEND_ORIGIN_ATTRIBUTE) or "").strip().lower()
        found.append((element_id, str(child), origin))
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


def _source(kind: str, reference: object, label: object) -> str:
    """One source element: the durable record a sentence rests on.

    ``kind`` names what sort of record ``reference`` addresses — a run, a
    comment, the plan, a figure path, a gate — because a bare id reads as an
    assertion with nothing behind it.
    """

    return (
        f'<span class="{SOURCE_CLASS}" '
        f'data-source="{_escape(reference)}" data-source-kind="{_escape(kind)}">'
        f"{_escape(label)}</span>"
    )


@dataclass(frozen=True, slots=True)
class LandedFigure:
    """One figure a landed run named, addressed for the served page."""

    path: str
    src: str
    caption: str
    run_id: str


@dataclass(frozen=True, slots=True)
class SectionLanding:
    """What one plan section landed: its landing line, runs, figures.

    This is the one reading of a landing the closure overview renders. It is
    derived from the plan and the ledger alone, so a caller that needs to know
    whether a section landed — the close guard, the sprint page — asks this
    rather than re-deriving it from a second population.
    """

    section: str
    label: str
    declaration: str
    done_when: str
    landing_comment_id: str
    landing: str
    runs: tuple[Mapping[str, Any], ...]
    figures: tuple[LandedFigure, ...] = ()

    @property
    def attempts(self) -> int:
        """How many implementing attempts this section holds."""
        return len(self.runs)

    @property
    def landed(self) -> bool:
        """Whether the section holds a landing line or a landed run."""
        return bool(self.landing.strip() or self.runs)


def _section_texts(source: str) -> dict[str, str]:
    """The authored body text of every section, keyed as the renderer keys it.

    Paragraphs are kept on their own lines rather than run together, because a
    reader that flattens the section cannot tell where one authored paragraph
    ends: a done-when quoted from flat text stops at the first full stop, so an
    abbreviation or a dotted path inside it truncates the declaration. The
    newline is the paragraph boundary the plan's own markup carries.
    """
    bodies: dict[str, str] = {}
    for heading in _plan_html.plan_headings(source):
        section_id = str(heading.raw_id or "").strip()
        if not section_id or heading.span is None:
            continue
        begin, end = heading.span
        soup = BeautifulSoup(source[begin:end], "html.parser")
        # The block elements a body is authored in are the paragraph boundaries
        # a quote needs. Flattening a section that carries none of them would
        # hand a done-when reader the whole body on one line, so such a body is
        # reported as unparagraphable and no declaration is quoted from it.
        block_tags = ("p", "li", "td")
        blocks = [
            block
            for block in soup.find_all(list(block_tags))
            if block.find_parent(list(block_tags)) is None
        ]
        bodies[_section_key(section_id)] = "\n".join(
            " ".join(block.get_text(" ").split())
            for block in blocks
            if block.get_text(" ").strip()
        )
    return bodies


def _done_when(body: str) -> str:
    """The section's stated done-when paragraph, or the empty string for none.

    The whole paragraph is quoted, not the text up to its first full stop: a
    done-when naming an abbreviation or a dotted path holds full stops that
    belong to its own wording, and cutting there quotes a declaration in part.
    """
    for line in body.split("\n"):
        if re.search(r"\bDone when\b", line):
            return line.strip()
    return ""


def _landed_run(runs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The run a section's landing is recorded from: its latest implementation.

    The population and the ordering are the ones the record's own writes use —
    an implementing attempt, latest by completion and then run id — so a review
    of a run, whose record sits on the same section, can never be read as the
    run that landed it.
    """
    attempts = [
        record
        for record in runs
        if str(record.get("role") or "").strip() in EXECUTABLE_SECTION_ROLES
    ]
    if not attempts:
        return {}
    return max(
        attempts,
        key=lambda record: (
            str(record.get("completed_at") or ""),
            str(record.get("run_id") or ""),
        ),
    )


def _run_comment_id(run_id: str) -> str:
    """The comment id promotion writes for a run's landing comment.

    Promotion derives the id from the run id, so the landing comment of a run
    is found by deriving the same id here from the run the section landed. The
    prefix is taken from the writer that owns it, and the run id is normalised
    by the writer's own rule: a bare ``c-run-`` prefix names every review of a
    run as well as its landing, so matching the prefix alone would quote a
    review as the landing.
    """
    from reckon.crew.plan_review import RUN_COMMENT_PREFIX

    normalised = re.sub(r"[^A-Za-z0-9._-]+", "-", str(run_id or "").strip())
    return f"{RUN_COMMENT_PREFIX}{normalised}"


def _landing_comment(
    comments: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]] = (),
) -> Mapping[str, Any]:
    """The section's landing comment: the one written by the run that landed it.

    A section ticked with a close comment quotes that comment. One landed by a
    promotion holds a comment under the id promotion derives from the landed
    run, and a review of that run writes a comment under the same prefix on the
    same section — so the landing is read from a comment, never from whichever
    comment happens to come last.
    """
    for comment in comments:
        if str(comment.get("id") or "").startswith(
            _plan_html.SECTION_CLOSE_COMMENT_PREFIX
        ):
            return comment
    landed = _landed_run(runs)
    run_id = str(landed.get("run_id") or "").strip()
    if not run_id:
        return {}
    wanted = _run_comment_id(run_id)
    for comment in comments:
        if str(comment.get("id") or "") == wanted:
            return comment
    return {}


def _figure_src(path: str, project: str, root: Path | None) -> str:
    """The project-absolute ``src`` a ``docs/figures`` path is served at.

    The SPA resolves an image against the project mount, so a relative path
    renders as a 404 and the record's own figure is invisible. The address
    itself belongs to the figure inventory's row builder, which is the one
    place that derives where a figure is served, so it is taken from there
    rather than rebuilt.
    """
    if root is None:
        return ""
    relative = path.strip().strip("/")
    if not relative.startswith("docs/"):
        return ""
    try:
        row = figure_row(root / relative, root / "docs", project)
    except KeyError:
        # A file whose suffix no figure reader owns is not a figure the
        # inventory addresses, so it has no address to cite.
        return ""
    return str((row or {}).get("href") or "")


def _run_figures(
    record: Mapping[str, Any],
    plan_slug: str,
    project: str,
    root: Path | None,
) -> list[LandedFigure]:
    """The figures one run named on its manifest's ``figures:`` line.

    The manifest is where the node named its figure, so the path is read there
    rather than guessed from the plan's figure directory. A path outside the
    plan's own figure directory is not this plan's figure and is skipped, so a
    record copied from another plan cannot pull a stranger's figure in. A run
    whose manifest is gone names no figure, so the overview cites only figures
    it can source.
    """
    manifest_value = str(record.get("manifest_path") or "").strip()
    if not manifest_value or not plan_slug:
        return []
    try:
        text = Path(manifest_value).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    from reckon.crew.reports import ManifestParseError, parse_manifest

    try:
        fields = parse_manifest(text, path=manifest_value)
    except ManifestParseError:
        return []
    node = str(record.get("node") or record.get("run_id") or "").strip()
    directory = f"docs/figures/{plan_slug}/"
    figures: list[LandedFigure] = []
    for item in _field_paths(fields.get("figures")):
        path = str(item).strip()
        if not path.startswith(directory):
            continue
        src = _figure_src(path, project, root)
        if not src:
            continue
        figures.append(
            LandedFigure(
                run_id=str(record.get("run_id") or ""),
                path=path,
                src=src,
                caption=f"{node} — {Path(path).name}" if node else Path(path).name,
            )
        )
    return figures


def read_section_landings(
    plan: Mapping[str, Any],
    source: str,
    records: Sequence[Mapping[str, Any]],
    project: str = "",
    *,
    root: Path | None = None,
) -> list[SectionLanding]:
    """Read what each plan section landed: its landing line, runs and figures.

    Sections are returned in the plan's authored order followed by any key only
    a comment or a run names, which is the order the record renders. A figure
    is read from the run's own manifest, so the overview cites the figure a run
    actually named rather than one inferred from its changed paths.
    """

    comments_by_section = {
        _section_key(section): list(items or [])
        for section, items in (plan.get("comments") or {}).items()
    }
    runs_by_section: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        runs_by_section.setdefault(_section_key(record.get("section")), []).append(
            record
        )
    declarations = {
        _section_key(section): str(classification or "").strip()
        for section, classification in (plan.get("section_declarations") or {}).items()
    }
    # A plan read from its file carries the declaration map; one built from
    # its section records alone carries each section's own status. Both name
    # the same fact, so a caller reading either shape gets the declaration.
    for record in plan.get("sections") or []:
        if not isinstance(record, Mapping):
            continue
        key = _section_key(record.get("id"))
        if key not in declarations:
            declarations[key] = str(record.get("status") or "").strip()
    labels, authored_order = _section_labels(source)
    bodies = _section_texts(source)
    available = set(comments_by_section) | set(runs_by_section) | set(declarations)
    order = [key for key in authored_order if key in available]
    order.extend(sorted(available - set(order)))

    landings: list[SectionLanding] = []
    for key in order:
        comments = comments_by_section.get(key, [])
        runs = tuple(runs_by_section.get(key, ()))
        landing = _landing_comment(comments, runs)
        body = bodies.get(key)
        if body is None:
            body = " ".join(
                BeautifulSoup(str(landing.get("body") or ""), "html.parser")
                .get_text(" ")
                .split()
            )
        landings.append(
            SectionLanding(
                section=_section_element_id(key),
                label=labels.get(key)
                or ("Plan-level outcome" if key == "_top" else key),
                declaration=declarations.get(key, ""),
                done_when=_done_when(body) if bodies.get(key) else "",
                landing_comment_id=str(landing.get("id") or ""),
                landing=str(landing.get("body") or ""),
                runs=runs,
                figures=tuple(
                    figure
                    for record in runs
                    for figure in _run_figures(
                        record, plan.get("slug") or "", project, root
                    )
                ),
            )
        )
    return landings


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


def _gate_facts(record: Mapping[str, Any]) -> dict[str, object]:
    """The four facts a run's gate produced, as its own record holds them."""
    check = record.get("gate_check")
    check = check if isinstance(check, Mapping) else {}
    suite = record.get("suite_delta")
    suite = suite if isinstance(suite, Mapping) else {}
    after = suite.get("after_suite")
    after = after if isinstance(after, Mapping) else {}
    return {
        "command": str(
            check.get("command") or suite.get("suite_command") or ""
        ).strip(),
        "exit_status": check.get("exit_status"),
        "tests_added": record.get("tests_added"),
        "suite_failures": after.get("failure_count"),
    }


def _recorded(value: object) -> str:
    """A number a run recorded, or the word for one it did not."""
    return _escape(value) if value is not None else "unrecorded"


def _measure_text(record: Mapping[str, Any]) -> str:
    """One attempt's measured result, every figure cited to its own run."""
    facts = _gate_facts(record)
    run_id = str(record.get("run_id") or "")
    command = str(facts["command"] or "")
    exit_status = facts["exit_status"]
    gate = (
        f"gate <code>{_escape(command)}</code> exited {_recorded(exit_status)}"
        if command
        else "no gate command recorded"
    )
    return (
        f"{gate}, {_recorded(facts['tests_added'])} tests added, "
        f"{_recorded(facts['suite_failures'])} suite failures "
        f"{_source('run', run_id, f'run {run_id}')}"
    )


def _section_paragraph(landing: SectionLanding, plan_slug: str) -> str:
    """One paragraph: what the section set out to do, its landing, its result."""
    reference = f"{plan_slug}#{landing.section}"
    setting = (
        f'<q class="done-when">{_escape(landing.done_when)}</q> '
        f"{_source('plan', reference, 'plan')}"
        if landing.done_when
        else _source("plan", reference, "plan")
    )
    if landing.landed:
        landing_text = " ".join(
            BeautifulSoup(landing.landing, "html.parser").get_text(" ").split()
        )
        earlier = landing.runs[:-1]
        measured = (
            _measure_text(landing.runs[-1]) if landing.runs else "no run recorded"
        )
        if earlier:
            measured += (
                f", after {len(earlier)} earlier attempt"
                f"{'' if len(earlier) == 1 else 's'} "
                f"{_source('section', reference, landing.section)}"
            )
        # A section landed by a run whose landing comment cannot be found is
        # stated as such: quoting an empty landing line reads as a claim with
        # nothing behind it, which is what the source element exists to stop.
        landed_clause = (
            f"Landed: <q>{_escape(landing_text)}</q> "
            f"{_source('comment', landing.landing_comment_id or reference, 'landed')}."
            if landing_text
            else "No landing comment is recorded on it "
            f"{_source('section', reference, landing.section)}."
        )
        body = (
            f"<strong>{_escape(landing.label)}</strong> set out to {setting}. "
            f"{landed_clause} Measured: {measured}."
        )
    else:
        body = (
            f"<strong>{_escape(landing.label)}</strong> set out to {setting} and is "
            f"declared {_escape(landing.declaration or 'undeclared')}; nothing landed "
            f"{_source('plan', reference, 'declaration')}."
        )
    return (
        f'    <p class="section-summary" data-section="{_escape(landing.section)}">'
        f"{body}</p>"
    )


def _fragment_href(node: str, plan_slug: str, project: str, root: Path | None) -> str:
    """The served address of a landed node's own fragment, or ``""`` for none.

    A node's fragment is optional, so the overview links one only where it was
    written: the file's presence under the plan's fragment directory is the
    whole test. Nothing of the fragment's text is read — the overview states
    what its own sources hold and the link lets a reader open the record the
    node authored. The href is project-absolute, the same shape a fragment link
    in a plan section's comment takes: ``/<project>/evidence/fragments/...``.
    """
    if root is None or not node or not plan_slug:
        return ""
    relative = f"docs/evidence/fragments/{plan_slug}/{node}.html"
    if not (root / relative).is_file():
        return ""
    return f"/{project}/evidence/fragments/{plan_slug}/{node}.html"


def _node_rows(
    landings: Sequence[SectionLanding],
    *,
    plan_slug: str = "",
    project: str = "",
    root: Path | None = None,
) -> list[str]:
    """One row per landed node, stating how many attempts that node took."""
    rows: list[str] = []
    for landing in landings_that_landed(landings):
        by_node: dict[str, list[Mapping[str, Any]]] = {}
        for record in landing.runs:
            node = str(record.get("node") or record.get("run_id") or "").strip()
            by_node.setdefault(node, []).append(record)
        for node, attempts in by_node.items():
            latest = attempts[-1]
            commits = ", ".join(
                _escape(commit)
                for commit in (latest.get("commits") or [])
                if str(commit).strip()
            )
            run_id = str(latest.get("run_id") or "")
            fragment = _fragment_href(node, plan_slug, project, root)
            rows.append(
                "      <tr>"
                f"<td>{_escape(node)}</td>"
                f"<td>{_escape(landing.section)}</td>"
                f"<td>{len(attempts)} attempt{'' if len(attempts) == 1 else 's'}</td>"
                f"<td>{_escape(latest.get('gate'))}</td>"
                f"<td>{commits or '—'}</td>"
                f"<td>{_format_seconds(latest.get('worker_seconds'))}</td>"
                f"<td>{_source('run', run_id, run_id)}</td>"
                f"<td>{_fragment_link(fragment)}</td>"
                "</tr>"
            )
    return rows


def _fragment_link(href: str) -> str:
    """The row cell linking a node's own fragment, or an em dash for none."""
    if not href:
        return "—"
    return f'<a class="node-fragment" href="{_escape(href)}">fragment</a>'


def landings_that_landed(
    landings: Sequence[SectionLanding],
) -> list[SectionLanding]:
    """The sections of one reading that landed."""
    return [landing for landing in landings if landing.landed]


def _figure_blocks(landings: Sequence[SectionLanding]) -> list[str]:
    """One figure per named path, with its caption and its own source."""
    blocks: list[str] = []
    seen: set[str] = set()
    for landing in landings:
        for figure in landing.figures:
            if figure.path in seen:
                continue
            seen.add(figure.path)
            blocks.append(
                '    <figure class="landed-figure">'
                f'<img src="{_escape(figure.src)}" alt="{_escape(figure.caption)}">'
                f"<figcaption>{_escape(figure.caption)} "
                f"{_source('figure', figure.path, figure.path)}</figcaption>"
                "</figure>"
            )
    return blocks


@dataclass(frozen=True, slots=True)
class ReviewTotals:
    """One stored review's findings, counted by how they were disposed of.

    ``raised`` is what the review recorded; ``folded`` and ``exempted`` are the
    two dispositions that answer a finding, and everything else — a finding
    with no disposition at all, and one a landing deferred without judging it —
    is still open work.
    """

    review_run_id: str
    raised: int
    folded: int
    exempted: int
    still_open: int


def _review_totals(record: Mapping[str, Any]) -> ReviewTotals:
    """One stored review's findings, counted by the disposition it carries."""
    from reckon.crew.findings import finding_disposition, parse_finding_disposition

    findings = [
        item for item in record.get("findings") or () if isinstance(item, Mapping)
    ]
    folded = exempted = still_open = 0
    for index in range(len(findings)):
        kind = parse_finding_disposition(finding_disposition(record, index))["kind"]
        if kind == "folded":
            folded += 1
        elif kind == "exempted":
            exempted += 1
        else:
            still_open += 1
    return ReviewTotals(
        review_run_id=str(record.get("review_run_id") or "").strip(),
        raised=len(findings),
        folded=folded,
        exempted=exempted,
        still_open=still_open,
    )


def _committed_review_totals(
    plan_slug: str, project: str, root: Path | None
) -> list[ReviewTotals]:
    """The plan's committed review records, counted in the order they ran.

    The records live in the project's committed tree, so every clone reads the
    same reviews. A checkout that resolves no review tree, or a plan with no
    stored review, yields nothing rather than an invented figure.
    """
    if root is None or not plan_slug:
        return []
    from reckon.crew import review as review_store

    reviews_root = review_store.committed_review_root(project, root=root)
    if reviews_root is None:
        return []
    directory = reviews_root / review_store.COMMITTED_PLAN_DIRNAME / plan_slug
    if not directory.is_dir():
        return []
    totals: list[ReviewTotals] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, Mapping):
            continue
        if not str(record.get("review_run_id") or "").strip():
            # The committed store keys a record by the review run that wrote
            # it, so a record carrying no id is read from its own file name.
            record = dict(record, review_run_id=path.stem)
        totals.append(_review_totals(record))
    return totals


def _review_totals_text(totals: Sequence[ReviewTotals]) -> str:
    """Review findings and their dispositions, cited to the review that raised them.

    Each per-review figure is attributed to the review run that recorded it,
    and the totals carry the source of every review that contributed to them,
    so an aggregate can be followed back to the records behind it.
    """
    if not totals:
        return ""
    sources = "".join(
        _source("review", item.review_run_id, item.review_run_id) for item in totals
    )
    folded = sum(item.folded for item in totals)
    exempted = sum(item.exempted for item in totals)
    still_open = sum(item.still_open for item in totals)
    raised = sum(item.raised for item in totals)
    by_review = "; ".join(
        f"{_escape(item.review_run_id)} raised {item.raised} "
        f"({item.folded} folded, {item.exempted} exempted, {item.still_open} open)"
        for item in totals
    )
    return (
        f" Reviews: {raised} finding(s) raised across {len(totals)} stored "
        f"review(s), {folded} folded, {exempted} exempted and {still_open} open "
        f"{sources}; by review: {by_review}."
    )


def _totals_paragraph(
    plan: Mapping[str, Any],
    landings: Sequence[SectionLanding],
    records: Sequence[Mapping[str, Any]],
    project: str,
    root: Path | None = None,
) -> str:
    """Totals for runs, attempts and worker hours, and for the review findings."""
    landed = landings_that_landed(landings)
    attempts = sum(landing.attempts for landing in landed)
    worker_seconds = sum(int(record.get("worker_seconds") or 0) for record in records)
    declared = plan.get("effort_hours")
    reviews = _review_totals_text(
        _committed_review_totals(str(plan.get("slug") or "").strip(), project, root)
    )
    return (
        f'    <p class="overview-totals">{len(records)} run(s) across '
        f"{len(landed)} landed section(s) and {attempts} attempt(s), "
        f"{_format_seconds(worker_seconds)} of worker time against "
        f"{_recorded(declared)} declared hour(s) "
        f"{_source('ledger', project or str(plan.get('slug') or ''), 'ledger')}."
        f"{reviews}</p>"
    )


def _overview_block(
    plan: Mapping[str, Any],
    landings: Sequence[SectionLanding],
    records: Sequence[Mapping[str, Any]],
    project: str,
    root: Path | None = None,
) -> str:
    """The sourced overview: a paragraph per section, one row per landed node."""
    plan_slug = str(plan.get("slug") or "").strip()
    paragraphs = [_section_paragraph(landing, plan_slug) for landing in landings]
    rows = _node_rows(landings, plan_slug=plan_slug, project=project, root=root)
    figures = _figure_blocks(landings)
    origin = f'{APPEND_ORIGIN_ATTRIBUTE}="{OVERVIEW_ORIGIN_VALUE}"'
    header = (
        "      <thead><tr><th>Node</th><th>Section</th><th>Attempts</th>"
        "<th>Gate</th><th>Commits</th><th>Worker time</th><th>Source</th>"
        "<th>Fragment</th>"
        "</tr></thead>"
    )
    table = (
        [
            "    <table>",
            header,
            "      <tbody>",
            *rows,
            "      </tbody>",
            "    </table>",
        ]
        if rows
        else ["    <p>No node landed.</p>"]
    )
    figures_block = figures or ["    <p>No run named a figure.</p>"]
    return "\n".join(
        [
            f'  <section id="overview" {origin}>',
            "    <h2>Sections</h2>",
            *paragraphs,
            "  </section>",
            f'  <section id="landed-nodes" {origin}>',
            "    <h2>Landed nodes</h2>",
            *table,
            "  </section>",
            f'  <section id="figures" {origin}>',
            "    <h2>Figures</h2>",
            *figures_block,
            "  </section>",
            f'  <section id="totals" {origin}>',
            "    <h2>Totals</h2>",
            _totals_paragraph(plan, landings, records, project, root),
            "  </section>",
        ]
    )


@dataclass(frozen=True, slots=True)
class SprintPlanRow:
    """One member plan of a closing sprint, read from its own record.

    The counts are the ones the plan's closure overview states: sections it
    landed, the runs that landed them and their worker time. ``href`` is the
    plan document the row links, empty when the sprint names a plan with no
    document — a row must never link a page that is not there.
    """

    slug: str
    title: str
    status: str
    href: str
    sections_landed: int
    runs: int
    worker_seconds: int
    headline: str
    figures: tuple[LandedFigure, ...] = ()


_SPRINT_PAGE_SUFFIX = "-closing"


def sprint_page_path(docs_dir: Path, sprint_id: str) -> Path:
    """The closing page one sprint's close writes, under the evidence archive.

    The page is an evidence record, not a sprint resource: a second file in the
    sprint root would be discovered as a sprint of its own, and the closing page
    documents what the sprint's plans landed rather than the sprint's schedule.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(sprint_id or "").strip()).strip("-")
    if not stem:
        raise EvidenceSynthesisError(
            f"sprint id {sprint_id!r} names no page: a closing page needs an id"
        )
    return docs_dir / "evidence" / "archive" / f"{stem}{_SPRINT_PAGE_SUFFIX}.html"


def _plan_page_href(project: str, resource: Any) -> str:
    """The served address of a plan document, or the empty string without one.

    The address names the file rather than the SPA's extensionless route, so
    every link the page writes resolves to a document in the repository.
    """
    relative = getattr(resource, "relative_path", None)
    if relative is None:
        return ""
    return f"/{project}/{relative}"


def _headline_result(landings: Sequence[SectionLanding], summary: str) -> str:
    """The plan's headline result: what its latest landing recorded.

    A plan with no landing line falls back to the summary it was authored with,
    and one with neither says so rather than rendering an empty cell that reads
    as a result.
    """
    for landing in reversed(landings_that_landed(landings)):
        text = " ".join(
            BeautifulSoup(landing.landing, "html.parser").get_text(" ").split()
        )
        if text:
            return text
    return summary.strip() or "no result recorded"


def read_sprint_plan_row(docs_dir: Path, project: str, slug: str) -> SprintPlanRow:
    """Read one member plan through the reading the closure overview built.

    Sections landed, runs and worker time come from
    :func:`read_section_landings` — the one reading a plan's overview renders —
    rather than from a second walk of the ledger, so the sprint page and the
    plan's overview cannot state different counts for the same plan.
    """
    resource = resolve_resource(
        docs_dir, project, slug, artifact_type="plan", include_archived=False
    )
    if resource is None:
        return SprintPlanRow(
            slug=slug,
            title=slug,
            status="no plan document",
            href="",
            sections_landed=0,
            runs=0,
            worker_seconds=0,
            headline="the sprint names a plan with no document",
        )
    source = resource.path.read_text(encoding="utf-8")
    plan = _plan_html.parse_plan(resource.path, slug)
    try:
        records = ledger.runs(project, root=docs_dir.parent, plan=slug)
    except ledger.LedgerError as exc:
        raise EvidenceSynthesisError(
            f"cannot read the ledger for project {project!r}, plan {slug!r}: {exc}"
        ) from exc
    records = sorted(
        records,
        key=lambda record: (
            str(record.get("completed_at") or ""),
            str(record.get("run_id") or ""),
        ),
    )
    landings = read_section_landings(
        plan, source, records, project, root=docs_dir.parent
    )
    landed = landings_that_landed(landings)
    return SprintPlanRow(
        slug=slug,
        title=str(plan.get("title") or slug),
        status=str(plan.get("status") or "unrecorded"),
        href=_plan_page_href(project, resource),
        sections_landed=len(landed),
        runs=len(records),
        worker_seconds=sum(
            int(record.get("worker_seconds") or 0) for record in records
        ),
        headline=_headline_result(landings, str(plan.get("summary") or "")),
        figures=tuple(figure for landing in landings for figure in landing.figures),
    )


def _sprint_row(row: SprintPlanRow) -> str:
    """One table row: the plan, its status, what it landed and its result."""
    name = (
        f'<a href="{_escape(row.href)}">{_escape(row.title)}</a>'
        if row.href
        else _escape(row.title)
    )
    return (
        "      <tr>"
        f"<td>{name}</td>"
        f"<td>{_escape(row.status)}</td>"
        f"<td>{row.sections_landed}</td>"
        f"<td>{row.runs}</td>"
        f"<td>{_format_seconds(row.worker_seconds)}</td>"
        f"<td>{_escape(row.headline)}</td>"
        "</tr>"
    )


def render_sprint_page(
    project: str, sprint_id: str, rows: Sequence[SprintPlanRow]
) -> str:
    """Render one closing page: a row per member plan, its figures, its totals."""
    stem = sprint_page_path(Path("."), sprint_id).stem
    title = f"{sprint_id} — closing sprint"
    sections_landed = sum(row.sections_landed for row in rows)
    runs = sum(row.runs for row in rows)
    worker_seconds = sum(row.worker_seconds for row in rows)
    figures = _figure_blocks(
        [
            SectionLanding(
                section=row.slug,
                label=row.title,
                declaration="",
                done_when="",
                landing_comment_id="",
                landing="",
                runs=(),
                figures=row.figures,
            )
            for row in rows
        ]
    )
    table = [
        '    <section id="sprint-plans">',
        "      <h2>Member plans</h2>",
        "      <table>",
        (
            "        <thead><tr><th>Plan</th><th>Status</th>"
            "<th>Sections landed</th><th>Runs</th>"
            "<th>Worker time</th><th>Headline result</th></tr></thead>"
        ),
        "        <tbody>",
        *[_sprint_row(row) for row in rows],
        "        </tbody>",
        "      </table>",
        "    </section>",
    ]
    totals = [
        '    <section id="sprint-totals">',
        "      <h2>Totals</h2>",
        (
            f"      <p>{len(rows)} plan(s), {sections_landed} section(s) landed, "
            f"{runs} run(s), {_format_seconds(worker_seconds)} of worker time.</p>"
        ),
        "    </section>",
    ]
    figure_section = [
        '    <section id="sprint-figures">',
        "      <h2>Figures</h2>",
        *(figures or ["      <p>No member plan's overview carries a figure.</p>"]),
        "    </section>",
    ]
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f'  <meta name="docs-project" content="{_escape(project)}">',
            '  <meta name="reckon-type" content="evidence">',
            f'  <meta name="plan-slug" content="{_escape(stem)}">',
            f'  <meta name="plan-title" content="{_escape(title)}">',
            f"  <title>{_escape(title)}</title>",
            "</head>",
            "<body>",
            ' <main class="plan-doc">',
            f"  <h1>{_escape(title)}</h1>",
            f"  <p>What the {len(rows)} plan(s) of this sprint landed, read from "
            "their own closure readings and the runs their ledger holds. Every "
            "link resolves to a document, and every figure is one a landed run "
            "named.</p>",
            *table,
            *figure_section,
            *totals,
            " </main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def write_sprint_page(
    docs_dir: Path, project: str, sprint_id: str, slugs: Sequence[str]
) -> Path:
    """Write the closing page for one sprint from its member plans' readings.

    A sprint close owes one page, so its failure is the close's failure: a
    sprint recorded as closed with no page would leave the work unreadable.
    """
    rows = [read_sprint_plan_row(docs_dir, project, slug) for slug in slugs]
    destination = sprint_page_path(docs_dir, sprint_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from reckon._store import write_atomically

    rendered = render_sprint_page(project, sprint_id, rows)
    try:
        write_atomically(
            destination,
            lambda handle: handle.write(rendered),
            fsync=False,
            mode=0o600,
        )
    except OSError as exc:
        raise EvidenceSynthesisError(
            f"cannot write the closing page {destination}: {exc}"
        ) from exc
    return destination


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
    # The count sentence and the chip naming the ledger it was read from are one
    # statement, so a synthesis states both. A frame an append creates read no
    # ledger at all: it publishes neither the run count nor a source for it.
    if created_by_append:
        summary_paragraph = f"  <p>{_escape(summary)}</p>\n"
    else:
        summary_paragraph = (
            f"  <p>{_escape(summary)} "
            f"{_source('ledger', project or plan_slug, f'{len(records)} run(s)')}</p>\n"
        )
    overview = (
        ""
        if created_by_append
        else _overview_block(
            plan,
            read_section_landings(plan, source, records, project, root=root),
            records,
            project,
            root,
        )
        + "\n\n"
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
        f"{summary_paragraph}"
        "\n" + overview + "\n\n".join(sections) + "\n </main>\n"
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
    for element_id, markup, origin in _record_sections(html_text):
        if origin == OVERVIEW_ORIGIN_VALUE:
            # The overview is this module's own block, re-rendered from its
            # inputs on every synthesis, so keeping the copy already on disk
            # would write it twice and let a stale copy outlive its inputs.
            continue
        marked = origin == APPEND_ORIGIN_VALUE
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
