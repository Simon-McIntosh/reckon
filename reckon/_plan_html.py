#!/usr/bin/env python3
"""HTML plan state engine — plan data IS semantic HTML.

A plan page carries its data as ordinary HTML the reader can see and the
reckon server reads and writes directly. There is NO embedded JSON blob.

  - Scalars and the versioned capability request live in
    ``<meta name="plan-*">`` elements.
  - Decisions are <div class="r-dec" data-key=…> elements inside
    <section data-reckon="decisions">, with visible <button class="r-opt"
    data-value=…> options, a data-choice attribute (the locked answer, an
    option value OR free text), and a free-form .r-dec-rat rationale.
  - Typed section records live on h2 elements or adjacent
    ``<section data-reckon="section" data-id=...>`` elements.
  - Gates / followups / questions / comments are matching
    <section data-reckon=…> blocks of semantic elements.

`read_state` parses this into the canonical dict; `write_state` regenerates
the reckon-owned sections + meta from the dict, leaving authored prose
untouched. Reads degrade gracefully: a bare page with no plan markup still
yields a valid record (slug from filename, title from <title>, status=draft).
"""

from __future__ import annotations

import html as _htmlmod
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path

from bs4 import BeautifulSoup

from reckon._schema import LEGACY_EFFORT_HOURS, PlanState
from reckon.capability import (
    CAPABILITY_SCHEMA_VERSION,
    from_legacy_tier,
)
from reckon.tags import normalise_tag

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

RECKON_ATTRIBUTE = "data-reckon"
LANDED_SECTION_CLASS = "section-landed"
_SECTION_OPEN_RE = re.compile(r"<section\b[^>]*>", re.IGNORECASE)


def machinery_kind(attributes) -> str | None:
    """The owned marker's value; absence alone denotes an unmarked element."""
    values = dict(attributes)
    return (values.get(RECKON_ATTRIBUTE) or "") if RECKON_ATTRIBUTE in values else None


def _section_identity_value(attributes, enclosing=None):
    """Read the identity attribute once, with authored wrapper fallback."""
    value = attributes.get("id") if "id" in attributes else attributes.get("data-id")
    if value is None and enclosing and machinery_kind(enclosing) in (None, "section"):
        value = enclosing.get("id")
    return value


SECTION_NUMBER_PATTERN = r"\d+(?:[.-]\d+)*"
SECTION_NUMBER_CONTINUATION = SECTION_NUMBER_PATTERN[len(r"\d+") :]
_SECTION_IDENTITY = re.compile(
    rf"^(?:§|#)?\s*(?:s(?:ection)?[\s.-]*)?({SECTION_NUMBER_PATTERN})$", re.IGNORECASE
)


def section_record_id(section: object, enclosing=None) -> str:
    """Return the one identity every spelling of a plan section addresses.

    A section may use a hyphenated anchor, a section-sign display, or prose
    numbering. Its typed record, element ids, and comment anchors preserve the
    same identity, so callers resolve every spelling through this derivation.
    """
    if isinstance(section, Mapping):
        section = _section_identity_value(section, enclosing)
    text = re.sub(r"\s+", " ", str(section or "").strip())
    numbered = _SECTION_IDENTITY.fullmatch(text)
    if numbered:
        return "s" + numbered.group(1).replace(".", "-")
    return text.removeprefix("#").casefold()


def section_anchor(section: object) -> str:
    """Return the anchor a section reference's comments and records hang from."""
    return section_record_id(section) or "_top"


def section_id_candidates(section: object) -> set[str]:
    """Return every authored id spelling one section reference may address.

    A section's identity is one, but a plan is authored under whichever
    spelling its author wrote: the hyphenated id a typed record carries
    (``s5-1``) or the dotted one an author may have used (``s5.1``). The raw
    reference stays a candidate of its own, because for a slug section it is
    the whole id. Both authored-HTML lookups build their candidate set here,
    so the two cannot drift apart.
    """
    text = re.sub(r"\s+", " ", str(section or "").strip())
    identity = section_record_id(text)
    candidates = {text.casefold().removeprefix("#"), identity}
    numbered = _SECTION_IDENTITY.fullmatch(identity)
    if numbered:
        candidates.add(f"s{numbered.group(1).replace('-', '.')}")
    return {candidate for candidate in candidates if candidate}


def _is_landed(attributes) -> bool:
    classes = dict(attributes).get("class") or ()
    return LANDED_SECTION_CLASS in (
        classes.split() if isinstance(classes, str) else classes
    )


@dataclass
class _PlanHeadingRecord:
    identity: str
    raw_id: str | None
    level: int
    text: str
    span: tuple[int, int]
    is_card: bool
    identity_span: tuple[int, int]
    heading_span: tuple[int, int]
    opening_span: tuple[int, int]
    inner_span: tuple[int, int]
    own_id: str | None
    machinery: bool = False
    error: str = ""
    body_span: tuple[int, int] = (0, 0)
    opening_html: str = ""


@dataclass
class _StructuralSpan:
    tag: str
    attributes: dict
    start: int
    open_end: int
    close_start: int
    end: int
    closed: bool = False
    protected: bool = False
    heading: _PlanHeadingRecord | None = None
    parents: tuple = ()

    @property
    def span(self):
        return self.start, self.end

    @property
    def kind(self):
        return machinery_kind(self.attributes)


class _StructuredSectionSpanParser(HTMLParser):
    """Locate structural spans, headings and their extents in one raw read."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0, *(match.end() for match in re.finditer(r"\n", source))]
        self.records: list[_StructuralSpan] = []
        self.stack: list[_StructuralSpan] = []
        self.elements: list[tuple[str, _StructuralSpan | None]] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        level = int(tag[1]) if re.fullmatch(r"h[1-6]", tag) else None
        values = {name: value if value is not None else "" for name, value in attrs}
        structural = level is not None or tag in {"section", "main", "body"}
        if not structural and machinery_kind(values) is None:
            if tag not in _VOID_TAGS:
                self.elements.append((tag, None))
            return
        start = self._offset()
        end = start + len(self.get_starttag_text())
        record = _StructuralSpan(
            tag,
            values,
            start,
            end,
            len(self.source),
            len(self.source),
            parents=tuple(
                record
                for record in self.stack
                if record.tag in {"section", "main", "body"}
                or record.heading is not None
            ),
        )
        kind = machinery_kind(values)
        record.protected = (tag == "section" and kind is not None) or (
            level == 2 and kind == "section"
        )
        if level is not None:
            wrapper = next(
                (parent for parent in reversed(self.stack) if parent.tag == "section"),
                None,
            )
            enclosing = wrapper.attributes if wrapper else None
            heading_values = {"id": None, **values}
            raw = _section_identity_value(heading_values, enclosing)
            own = _section_identity_value(heading_values)
            record.heading = _PlanHeadingRecord(
                section_record_id(heading_values, enclosing),
                raw,
                level,
                "",
                (start, len(self.source)),
                False,
                (start, end),
                (start, end),
                (start, end),
                (end, end),
                own,
                any(parent.kind not in (None, "section") for parent in self.stack),
            )
        self.records.append(record)
        if tag in _VOID_TAGS:
            record.close_start = record.end = record.open_end
            record.closed = True
        else:
            self.stack.append(record)
            self.elements.append((tag, record))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.elements and self.elements[-1][0] == tag:
            _, record = self.elements.pop()
            if record is not None:
                self.stack.remove(record)
                record.close_start = record.end = record.open_end
                record.closed = True

    def handle_endtag(self, tag: str) -> None:
        index = next(
            (
                i
                for i in range(len(self.elements) - 1, -1, -1)
                if self.elements[i][0] == tag
            ),
            None,
        )
        if index is None:
            return
        _, record = self.elements.pop(index)
        if record is None:
            return
        start = self._offset()
        end = self.source.find(">", start)
        record.close_start = start
        record.end = len(self.source) if end < 0 else end + 1
        record.closed = True
        self.stack.remove(record)

    def close(self) -> None:
        super().close()
        headings = [record for record in self.records if record.heading is not None]
        sections = [record for record in self.records if record.tag == "section"]
        for record in headings:
            heading = record.heading
            heading.heading_span = record.span
            heading.inner_span = record.open_end, record.close_start
            heading.text = BeautifulSoup(
                self.source[record.open_end : record.close_start], "html.parser"
            ).get_text(" ", strip=True)
            wrapper = next(
                (
                    parent
                    for parent in reversed(record.parents)
                    if parent.tag == "section"
                ),
                None,
            )
            identity_element = (
                wrapper
                if heading.own_id is None and heading.raw_id is not None
                else record
            )
            heading.identity_span = identity_element.span
            heading.opening_html = self.source[record.start : record.open_end]
            if heading.own_id is None and heading.raw_id is not None:
                heading.opening_html = (
                    heading.opening_html[:-1]
                    + f' id="{_htmlmod.escape(heading.raw_id, quote=True)}">'
                )
        for record in headings:
            heading = record.heading
            wrapper = next(
                (
                    parent
                    for parent in reversed(record.parents)
                    if parent.tag == "section"
                ),
                None,
            )
            identity_element = (
                wrapper if heading.identity_span != heading.heading_span else record
            )
            ends = [len(self.source)]
            ends.extend(
                (
                    other.heading.identity_span[0]
                    if other.heading.identity_span[0] > record.start
                    else other.start
                )
                for other in headings
                if other.start > record.start and other.heading.level <= heading.level
            )
            ends.extend(
                other.start
                for other in sections
                if other.start > record.start
                and (
                    other.kind not in (None, "section") or _is_landed(other.attributes)
                )
            )
            ends.extend(
                parent.close_start
                for parent in record.parents
                if parent.close_start >= record.end
            )
            heading.span = record.start, min(ends)
            if not record.closed:
                heading.error = f"heading id {heading.raw_id!r} has no closing tag"
            if identity_element is not record:
                heading.span = identity_element.span
                if not identity_element.closed:
                    heading.error = (
                        f"the section carrying id {heading.raw_id!r} has no closing tag"
                    )
            if (
                heading.level == 2
                and wrapper is not None
                and _is_landed(wrapper.attributes)
            ):
                if not wrapper.closed:
                    heading.error = (
                        f"the landed card for {heading.raw_id!r} has no closing tag"
                    )
                elif sum(
                    other.heading.level == 2
                    for other in headings
                    if wrapper.start < other.start < wrapper.end
                ) != 1 or any(
                    other.start > wrapper.start
                    and other.start < wrapper.end
                    and (
                        other.kind not in (None, "section")
                        or _is_landed(other.attributes)
                    )
                    for other in sections
                ):
                    heading.error = f"cannot determine the extent of {heading.raw_id!r} unambiguously"
                else:
                    heading.is_card = True
                    heading.span = wrapper.span
            body_end = (
                identity_element.close_start
                if identity_element is not record
                else wrapper.close_start
                if heading.is_card
                else heading.span[1]
            )
            heading.body_span = record.end, body_end


def _structural_spans(html_text: str):
    parser = _StructuredSectionSpanParser(html_text)
    parser.feed(html_text)
    parser.close()
    return parser.records


def plan_headings(html_text: str):
    """Headings with canonical and raw identity, text, level and source spans.

    ``span`` is the section extent; ``heading_span`` and ``identity_span``
    identify the heading and the element carrying its identity independently.
    An invalid replacement extent carries ``error`` and never earns a card flag.
    """
    return tuple(
        record.heading
        for record in _structural_spans(html_text)
        if record.heading is not None
    )


def _section_spans(
    records,
    *,
    every_marked_element: bool = False,
    skip_landed_cards: bool = False,
    keep_landed_notes: bool = False,
) -> tuple[tuple[int, int], ...]:
    """The protected ranges one parsed record set yields.

    The default protects structured sections and typed heading openings. The
    prose reader opts into whole marked elements and can subtract landed-card
    interiors with their heading kept, so historical digests retain their
    prose. ``keep_landed_notes`` leaves the text of a marked note that is not a
    structured section — a ``div`` or ``p`` carrying the marker — in the prose reader's
    output, where the default blanks every marked element alike. The store-facing
    projection reads only the default.
    """
    spans = [
        (
            record.start,
            record.end
            if every_marked_element or not record.heading
            else record.open_end,
        )
        for record in records
        if (record.kind is not None if every_marked_element else record.protected)
        # A section record element, a section carrying the owned marker, is
        # transparent: its interior is that section's authored prose, and only
        # a marked collection nested inside it is subtracted. Without this a
        # section authored inside its own marked wrapper reads as empty prose.
        and not (every_marked_element and record.kind == "section")
        and not (every_marked_element and keep_landed_notes and record.tag != "section")
    ]
    if skip_landed_cards:
        for card in records:
            if card.tag != "section" or not _is_landed(card.attributes):
                continue
            cursor = card.start
            for heading in records:
                if (
                    heading.heading is not None
                    and heading.heading.level == 2
                    and card.start < heading.start < card.end
                ):
                    spans.append((cursor, heading.start))
                    cursor = heading.end
            spans.append((cursor, card.end))
    return tuple(sorted(spans))


def structured_section_spans(html_text: str) -> tuple[tuple[int, int], ...]:
    """Protected ranges for editing authored plan sections."""
    return _section_spans(_structural_spans(html_text))


# The identity key every authored unit outside a section is digested under.
DOCUMENT_UNIT = "_document"


def section_prose(
    document: str,
    *,
    keep_landed_cards: bool = False,
    keep_landed_notes: bool = False,
):
    """Yield each authored unit's prose in document order from one raw read.

    Each item is an ``(identity, prose)`` pair: a level-two authored section's
    prose keyed by its identity, or a remainder stretch keyed by
    :data:`DOCUMENT_UNIT`. Remainder prose interleaves between sections, so the
    result is an ordered sequence rather than a bare mapping. The document is
    parsed once, and the span rule and the body extent both come from that one
    record set. A landed card's interior is subtracted with its heading kept
    unless ``keep_landed_cards`` is set. Every marked element's text is
    subtracted unless ``keep_landed_notes`` is set, which retains the text of a
    marked note that is not a structured section — a ``div`` or ``p`` carrying the
    marker — so a search surface can keep a plan's landed notes.
    """
    records = _structural_spans(document)
    headings = [
        record.heading
        for record in records
        if record.heading is not None
        and record.heading.level == 2
        and record.heading.identity
        and not record.heading.machinery
    ]
    protected = _section_spans(
        records,
        every_marked_element=True,
        skip_landed_cards=not keep_landed_cards,
        keep_landed_notes=keep_landed_notes,
    )
    body = next((record for record in records if record.tag == "body"), None)
    start = body.open_end if body is not None else 0
    end = body.close_start if body is not None else len(document)
    # A landed card is a section element whose heading leads its parent header,
    # so its kept slice starts at the heading: the generated badge before it is
    # not authored prose. That stretch is reported under the document unit, so
    # a search surface that joins every slice keeps it while a section read of
    # the identity does not.
    card_headers = []
    for card in records:
        if card.tag != "section" or not _is_landed(card.attributes):
            continue
        lead = next(
            (
                heading
                for heading in headings
                if card.start < heading.heading_span[0] < card.end
            ),
            None,
        )
        if lead is not None and card.start < lead.heading_span[0]:
            card_headers.append((card.start, lead.heading_span[0]))
    cuts = {start, end}
    for left, right in (
        *protected,
        *(heading.span for heading in headings),
        *card_headers,
    ):
        cuts.update((max(start, min(end, left)), max(start, min(end, right))))
    points = sorted(cuts)
    for left, right in pairwise(points):
        identity = next(
            (
                heading.identity
                for heading in headings
                if heading.span[0] <= left and right <= heading.span[1]
            ),
            DOCUMENT_UNIT,
        )
        if any(low <= left and right <= high for low, high in card_headers):
            identity = DOCUMENT_UNIT
        raw = (
            ""
            if any(low <= left and right <= high for low, high in protected)
            else document[left:right]
        )
        yield identity, _strip_tags(raw)


def landed_section_ids(html_text: str) -> frozenset[str]:
    """Identities whose rendered section extent is an unambiguous landed card."""
    return frozenset(
        heading.identity
        for heading in plan_headings(html_text)
        if heading.identity and heading.is_card and not heading.error
    )


# ── Scalar fields carried in <meta name="plan-*"> ──────────────────────────
_SCALARS = (
    "slug",
    "title",
    "summary",
    "status",
    "roi",
    "effort",
    "milestone",
    "sprint",
    "graph_handle",
    "north_star",
    "tier",
    "owner",
    "modified",
    # Lifecycle visibility — set via UI status menu.
    # archived: "1" hides the plan from default inventory views (separate from status).
    # read:     "1" marks a research/doc as reviewed (de-emphasises in lists).
    "archived",
    "read",
    "reviewed_at",
    "recorded_at",
    "verdict",
    "environment",
    "source",
    "source_quality",
    # The wiring declaration: a non-empty reason silences the unwired-plan
    # finding. Carried as markup (plan-standalone) so it round-trips through a
    # state write rather than being dropped as an unknown field.
    "standalone",
)
_LIST_SCALARS = (
    "depends_on",
    "blocks",
    "after",
    "informs",
    "evidence_for",
    "verifies",
    "supersedes",
    "commits",
    "artifacts",
    "tags",
)  # comma-separated in meta

_PLAN_ONLY_METAS = (
    "plan-status",
    "plan-roi",
    "plan-effort-hours",
    "plan-effort",
    "plan-milestone",
    "plan-sprint",
    "plan-graph-handle",
    "plan-north-star",
    "plan-tier",
    "plan-capability-version",
    "plan-capability-class",
    "plan-capability-reasoning",
    "plan-capability-context",
    "plan-capability-tool-autonomy",
    "plan-capability-verification",
    "plan-capability-risk",
    "plan-depends-on",
    "plan-blocks",
    "plan-after",
    "plan-standalone",
    "plan-impl",
    "plan-section-declarations",
)

_DEFAULTS = {
    "status": "draft",
    "roi": "mid",
    "effort": "M",
    "milestone": "—",
    "sprint": None,
    "summary": "",
    "modified": "",
    "owner": "",
    "impl": 0.0,
    "version": 0,
}

# reckon-owned section ids — regenerated on write, stripped by the SPA before
# rendering the authored prose (it renders interactive widgets instead).
SECTION_IDS = ("gates", "decisions", "followups", "questions", "research", "comments")

_CAPABILITY_REQUIREMENTS = (
    "reasoning",
    "context",
    "tool_autonomy",
    "verification",
    "risk",
)


def _esc(s) -> str:
    return _htmlmod.escape("" if s is None else str(s), quote=True)


def _body(s) -> str:
    """Emit a body field verbatim — body fields ARE authored HTML.

    The matching read path (:func:`_inner_html`) preserves the field's inner
    HTML, so write must re-emit it raw (NOT ``_esc``) to round-trip
    ``<strong>``, ``<code>``, ``<a>``, ``<p>`` etc. Applies only to body /
    outcome / resolution fields whose readers use ``_inner_html``; every other
    field (titles, attributes, the plain-text fleet prompt) still uses ``_esc``.
    """
    return "" if s is None else str(s)


def _txt(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _inner_html(el) -> str:
    """Return the inner HTML of an element, preserving authored markup.

    Body fields (comment / followup / question bodies and outcomes) are authored
    as HTML — ``<strong>``, ``<code>``, ``<a>``, ``<p>`` — and the SPA renders
    them as HTML. Flattening them with ``_txt`` would destroy that markup on the
    next ``write_state`` (every MCP edit regenerates ALL reckon-owned sections),
    so body fields are read with their inner HTML intact and re-emitted raw.
    ``str.strip`` only trims surrounding whitespace; entity normalisation by
    BeautifulSoup (``&#x27;`` → ``'``) is cosmetic and round-trip stable.
    """
    if el is None:
        return ""
    return el.decode_contents().strip()


def _canonical_type(value: object) -> str:
    """Return the canonical artifact type used by every read surface."""
    raw = str(value or "plan").strip().lower()
    return "research" if raw == "doc" else (raw or "plan")


def _capability_from_values(
    values: dict[str, str],
    *,
    prefix: str,
) -> dict | None:
    capability_class = values.get(f"{prefix}class", "")
    if not capability_class:
        return None
    requirements = {
        key: values[f"{prefix}{key.replace('_', '-')}"]
        for key in _CAPABILITY_REQUIREMENTS
        if values.get(f"{prefix}{key.replace('_', '-')}")
    }
    return {
        "version": values.get(f"{prefix}version") or CAPABILITY_SCHEMA_VERSION,
        "class": capability_class,
        "requirements": requirements,
    }


def _capability_attributes(capability: dict | None) -> str:
    if not capability:
        return ""
    requirements = capability.get("requirements") or {}
    attrs = [
        f' data-capability-version="{_esc(capability.get("version") or CAPABILITY_SCHEMA_VERSION)}"',
        f' data-capability-class="{_esc(capability.get("class"))}"',
    ]
    attrs.extend(
        f' data-capability-{key.replace("_", "-")}="{_esc(requirements[key])}"'
        for key in _CAPABILITY_REQUIREMENTS
        if requirements.get(key)
    )
    return "".join(attrs)


# ── Read ───────────────────────────────────────────────────────────────────


def _section_record_elements(soup: BeautifulSoup) -> list:
    """A structural section wrapper opts in by declaring record metadata."""
    return [
        element
        for element in soup.select('[data-reckon="section"]')
        if any(
            key
            in {
                "data-id",
                "data-effort-hours",
                "data-attempts",
                "data-status",
                "data-links",
            }
            or key.startswith("data-capability-")
            for key in element.attrs
        )
    ]


_SECTION_RECORD_ATTRIBUTES = (
    "data-id",
    "data-effort-hours",
    "data-attempts",
    "data-status",
    "data-links",
)
_RECORD_MARKER_RE = re.compile(
    rf"""{RECKON_ATTRIBUTE}\s*=\s*["']section["']""", re.IGNORECASE
)


def _record_field(record, name):
    if hasattr(record, "get"):
        return record.get(name)
    return getattr(record, name, None)


def derive_impl_from_sections(sections, declarations=None) -> float | None:
    """impl from section effort: done over done plus remaining implementable.

    Deferred sections sit outside the denominator, and a plan carrying no
    record in scope derives nothing — the caller keeps the authored figure
    rather than reporting a fabricated zero.

    The records must cover the declarations for the same reason. A section
    declared done or implementable that carries no record is effort the
    denominator cannot see, so a figure derived over the remaining records
    reports the absent records as a zero rather than as unknown, and the
    authored figure stands instead.
    """
    if not sections:
        return None
    recorded = {str(_record_field(record, "id") or "") for record in sections}
    for section, classification in (declarations or {}).items():
        if (
            str(classification or "").strip() in ("done", "implementable")
            and str(section) not in recorded
        ):
            return None
    done = 0.0
    predicted = 0.0
    for record in sections:
        effort = _record_field(record, "effort_hours")
        status = str(_record_field(record, "status") or "").lower()
        try:
            hours = float(effort)
        except (TypeError, ValueError):
            return None
        if hours <= 0:
            return None
        if status == "done":
            done += hours
        elif status == "implementable":
            predicted += hours
        elif status != "deferred":
            return None
    total = done + predicted
    if total <= 0:
        return None
    return done / total


def _document_carries_records(html_text: str) -> bool:
    """Whether the document declares record metadata, judged without parsing.

    A gate rather than an approximation of the contract: a document carrying no
    such marker cannot hold a record, and one that does is handed to the parsed
    read so the figure the fast path reports is the figure the parser derived.
    A record the parser would refuse yields no figure on either path.
    """
    if _RECORD_MARKER_RE.search(html_text or "") is None:
        return False
    return any(key in html_text for key in _SECTION_RECORD_ATTRIBUTES) or (
        "data-capability-" in html_text
    )


def _read_section_records(soup: BeautifulSoup, declarations: dict) -> list[dict]:
    """Validate stored metadata; data-attempts is legacy, not the live count.

    The read API derives attempts from distinct crew run ids. An existing
    attribute is parsed and preserved, but its value never drives the count.
    """
    records = []
    for element in _section_record_elements(soup):
        if element.name == "h2":
            identity = section_record_id({"id": None, **element.attrs})
        elif element.name == "section":
            identity = section_record_id(element.attrs)
            neighbors = [element.find_previous_sibling(), element.find_next_sibling()]
            if not any(
                neighbor is not None
                and neighbor.name == "h2"
                and section_record_id(neighbor.attrs) == identity
                for neighbor in neighbors
            ):
                raise ValueError(
                    f"sections[{identity!r}].id: record must be adjacent to its h2"
                )
            if element.find(True) is not None or element.get_text(strip=True):
                raise ValueError(
                    f"sections[{identity!r}]: record metadata must not contain authored prose"
                )
        else:
            raise ValueError(
                "sections: data-reckon=section requires an h2 or adjacent section element"
            )
        attrs = {str(key): str(value) for key, value in element.attrs.items()}
        record: dict = {
            "id": identity,
            "capability": _capability_from_values(attrs, prefix="data-capability-"),
            "status": attrs.get("data-status"),
            "links": [
                ref.strip()
                for ref in attrs.get("data-links", "").split(",")
                if ref.strip()
            ],
        }
        for field, convert in (("effort_hours", float), ("attempts", int)):
            value = attrs.get(f"data-{field.replace('_', '-')}")
            if field == "attempts" and value is None:
                record[field] = 0
                continue
            try:
                record[field] = convert(value)
            except (TypeError, ValueError):
                record[field] = value
        records.append(record)
    if not records:
        return []
    return PlanState.model_validate(
        {"sections": records, "section_declarations": declarations}
    ).canonical_dump()["sections"]


_COMMENT_IDENTITY_FIELDS = ("id", "who", "when", "quote", "body")


def _comment_record(element) -> dict:
    """One comment's parsed fields.

    The reader and the write-side residence match share this extraction, so the
    record the reader returns and the element the writer recognises cannot
    disagree about identity.
    """
    return {
        "id": element.get("data-id", ""),
        "who": element.get("data-who", ""),
        "when": element.get("data-when", ""),
        "quote": element.get("data-quote", "") or None,
        "body": _inner_html(element.select_one(".r-comment-body")) or _txt(element),
    }


def _comment_identity(record) -> tuple:
    return tuple(str(record.get(field) or "") for field in _COMMENT_IDENTITY_FIELDS)


def _comment_elements(soup) -> list:
    """Every comment record in the document, in document order.

    A landing record written into a section's own body is a comment element
    like any other: the section it belongs to is named by its own
    ``data-section``, not by the section element that happens to hold it.
    Markup quoted inside a ``pre`` or ``code`` sample is text, not a record.
    """
    return [
        element
        for element in soup.select(".r-comment")
        if element.find_parent("pre") is None and element.find_parent("code") is None
    ]


def _in_comments_section(element) -> bool:
    """Whether this comment element sits inside the comments section itself."""
    return (
        element.find_parent("section", attrs={RECKON_ATTRIBUTE: "comments"}) is not None
    )


def _body_resident_comment_counts(html_text: str) -> Counter:
    """Identity counts of the comment records a document holds outside its
    comments section — the records a write leaves exactly where they are."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    counts: Counter = Counter()
    for element in _comment_elements(soup):
        if not _in_comments_section(element):
            counts[_comment_identity(_comment_record(element))] += 1
    return counts


def read_state(html_text: str) -> dict:
    """Parse a plan's semantic HTML into the canonical state dict."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    st: dict = {}
    meta_values: dict[str, str] = {}

    # Scalars from <meta name="plan-*">. A name carried more than once
    # resolves to its FIRST occurrence in document order; every later copy is
    # ignored and reported. ``_set_meta`` replaces the first occurrence, so
    # this rule is the one the writer maintains — last-wins read a different
    # line than the writer updated, and a write then reported a value the
    # reader never saw.
    duplicated: list[str] = []
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").lower()
        if name in meta_values:
            if name.startswith("plan-"):
                duplicated.append(name)
            continue
        meta_values[name] = m.get("content", "")
        if not name.startswith("plan-"):
            continue
        field = name[len("plan-") :].replace("-", "_")
        content = m.get("content", "")
        if field in _SCALARS:
            st[field] = content
        elif field in _LIST_SCALARS:
            st[field] = [x.strip() for x in content.split(",") if x.strip()]
        elif field in ("impl", "effort_hours", "wall_clock_hours", "version"):
            try:
                st[field] = int(content) if field == "version" else float(content)
            except (TypeError, ValueError):
                pass

    warnings: list[str] = []
    for duplicate_name in dict.fromkeys(duplicated):
        warnings.append(
            f"{duplicate_name}: duplicated in the document; the first "
            "occurrence in document order wins and every later copy is ignored"
        )
    raw_declarations = meta_values.get("plan-section-declarations")
    if raw_declarations is not None:
        try:
            declarations = json.loads(raw_declarations)
        except (TypeError, ValueError):
            declarations = None
        if isinstance(declarations, dict) and all(
            isinstance(section, str) and isinstance(classification, str)
            for section, classification in declarations.items()
        ):
            st["section_declarations"] = declarations
        else:
            warnings.append(
                "section_declarations: plan-section-declarations must be a JSON "
                "object mapping section identities to classifications"
            )
    st["sections"] = _read_section_records(soup, st.get("section_declarations", {}))
    derived = derive_impl_from_sections(st["sections"], st.get("section_declarations"))
    if derived is not None:
        st["impl"] = derived
        st["impl_source"] = "computed"
    elif "impl" in st:
        st["impl_source"] = "authored"
    capability = _capability_from_values(
        meta_values,
        prefix="plan-capability-",
    )
    if capability:
        st["capability"] = capability
    elif st.get("tier"):
        mapped, diagnostic = from_legacy_tier(st["tier"])
        if mapped:
            st["capability"] = mapped
            warnings.append(f"plan: {diagnostic}")

    authored_hours = "effort_hours" in st
    legacy_effort = st.get("effort")
    if authored_hours:
        st["effort_calibrated"] = True
        if legacy_effort:
            warnings.append(
                f"effort: legacy letter {legacy_effort!r} is redundant; "
                "explicit worker-hours win"
            )
    elif legacy_effort in LEGACY_EFFORT_HOURS:
        st["effort_hours"] = LEGACY_EFFORT_HOURS[legacy_effort]
        st["effort_calibrated"] = False
        warnings.append(
            f"effort: legacy letter {legacy_effort!r} maps to "
            f"{st['effort_hours']:.1f} worker-hours; plan is uncalibrated"
        )

    wall = st.get("wall_clock_hours")
    worker = st.get("effort_hours")
    if wall is not None and worker is not None and wall > worker:
        warnings.append(
            f"effort: wall-clock {wall:.2f}h exceeds {worker:.2f} worker-hours; "
            "parallelism cannot take longer than the same work done serially"
        )

    # ``doc`` remains a compatibility alias on disk but reads are canonical.
    rt = soup.find("meta", attrs={"name": "reckon-type"})
    st["type"] = _canonical_type(rt.get("content") if rt else "plan")

    # Owning project (<meta name="docs-project">). Captured additively so the
    # typed PlanState can carry it; write_state never re-emits this meta (it is
    # authored head that survives writes untouched), so the read/write asymmetry
    # is intentional. Absent → omitted (PlanState defaults it to '').
    dp = soup.find("meta", attrs={"name": "docs-project"})
    if dp is not None:
        st["project"] = (dp.get("content") or "").strip()

    title_tag = soup.find("title")
    if title_tag and not st.get("title"):
        st["title"] = title_tag.get_text(strip=True).split("|")[0].strip()

    # Evidence gates. ``passed`` is derived from the recorded verdict so the
    # semantic HTML never carries two values that can disagree.
    gates_section = soup.select_one('section[data-reckon="gates"]')
    if gates_section is not None:
        gates = []
        for gate in gates_section.select(".r-gate[data-id]"):
            verdict = (gate.get("data-verdict") or "").strip()
            evidence_link = gate.select_one(".r-gate-evidence")
            gates.append(
                {
                    "id": (gate.get("data-id") or "").strip(),
                    "section": (gate.get("data-section") or "").strip(),
                    "gated_sections": [
                        section.strip()
                        for section in (gate.get("data-gated-sections") or "").split(
                            ","
                        )
                        if section.strip()
                    ],
                    "status": (gate.get("data-status") or "").strip(),
                    "measure": _txt(gate.select_one(".r-gate-measure")),
                    "required_evidence": _inner_html(
                        gate.select_one(".r-gate-required-evidence")
                    ),
                    "verdict": verdict,
                    "evidence": (
                        (evidence_link.get("href") or "") if evidence_link else ""
                    ),
                    "passed": verdict == "passed",
                    **{
                        field: (gate.get(attribute) or "").strip()
                        for field, attribute in (
                            ("transition", "data-transition"),
                            ("gating_plan", "data-gating-plan"),
                            ("decision", "data-decision"),
                        )
                        if gate.has_attr(attribute)
                    },
                }
            )
        st["gates"] = gates

    # Decisions
    decisions: dict[str, dict] = {}
    for dec in soup.select('section[data-reckon="decisions"] .r-dec[data-key]'):
        key = dec.get("data-key", "").strip()
        if not key:
            continue
        opts = [
            {"value": b.get("data-value", _txt(b)), "label": _txt(b)}
            for b in dec.select(".r-opt")
        ]
        decisions[key] = {
            "title": _txt(dec.select_one(".r-dec-q")),
            "context": _txt(dec.select_one(".r-dec-ctx")),
            "choices": [o["value"] for o in opts],
            "option_labels": {o["value"]: o["label"] for o in opts},
            "choice": dec.get("data-choice", "") or "",
            "recommended": dec.get("data-recommended", "") or "",
            "recommended_by": dec.get("data-recommended-by", "") or "",
            "rationale": _txt(dec.select_one(".r-dec-rat")),
            "when": dec.get("data-when", "") or "",
            "by": dec.get("data-by", "") or "",
            "sections": [
                section.strip()
                for section in (dec.get("data-sections") or "").split(",")
                if section.strip()
            ],
        }
    st["decisions"] = decisions

    # Followups — resolved fields are present only when the followup is resolved.
    followups = []
    for fu in soup.select('section[data-reckon="followups"] .r-fu'):
        # Derive status from resolved_at so a stale data-status="open" left by
        # an older resolve_followup (which set resolved_at but not status) still
        # reads as resolved. Mirrors the questions parser.
        _resolved_at = fu.get("data-resolved-at")
        attributes = {str(key): str(value) for key, value in fu.attrs.items()}
        followup_capability = _capability_from_values(
            attributes,
            prefix="data-capability-",
        )
        f = {
            "id": fu.get("data-id", ""),
            "status": "resolved" if _resolved_at else fu.get("data-status", "open"),
            "written_by": fu.get("data-written-by", ""),
            "written_at": fu.get("data-written-at", ""),
            "recommends_skill": fu.get("data-recommends-skill", ""),
            "title": _txt(fu.select_one(".r-fu-title")),
            "body": _inner_html(fu.select_one(".r-fu-body")),
            # prompt is a plain-text fleet-dispatch block (preserved verbatim,
            # rendered as preformatted text — never as HTML).
            "prompt": (
                fu.select_one(".r-fu-prompt").get_text()
                if fu.select_one(".r-fu-prompt")
                else ""
            ),
        }
        legacy_tier = fu.get("data-tier", "")
        if legacy_tier:
            f["tier"] = legacy_tier
        if followup_capability:
            f["capability"] = followup_capability
        elif legacy_tier:
            mapped, diagnostic = from_legacy_tier(legacy_tier)
            if mapped:
                f["capability"] = mapped
                warnings.append(f"followup {f['id'] or '<no-id>'}: {diagnostic}")
        if fu.get("data-resolved-at"):
            f["resolved_at"] = fu.get("data-resolved-at")
        if fu.get("data-resolved-by"):
            f["resolved_by"] = fu.get("data-resolved-by")
        outcome = _inner_html(fu.select_one(".r-fu-outcome"))
        if outcome:
            f["outcome"] = outcome
        followups.append(f)
    st["followups"] = followups

    # Questions
    questions = []
    for q in soup.select('section[data-reckon="questions"] .r-q'):
        questions.append(
            {
                "id": q.get("data-id", ""),
                "section": q.get("data-section", ""),
                "opened_by": q.get("data-opened-by", ""),
                "opened_at": q.get("data-opened-at", ""),
                "body": _inner_html(q.select_one(".r-q-body")) or _txt(q),
                "resolution": _inner_html(q.select_one(".r-q-resolution")) or None,
                "resolved_at": q.get("data-resolved-at", "") or None,
                "resolved_by": q.get("data-resolved-by", "") or None,
            }
        )
    st["questions"] = questions

    # Research
    research = []
    for r in soup.select('section[data-reckon="research"] .r-research'):
        research.append(
            {
                "id": r.get("data-id", ""),
                "type": r.get("data-type", ""),
                "title": _txt(r.select_one(".r-research-title")) or _txt(r),
                "source": r.get("data-source", ""),
                "added_by": r.get("data-added-by", ""),
                "when": r.get("data-when", ""),
                "url": r.get("data-url", "") or None,
            }
        )
    st["research"] = research

    # Comments (section-anchored). A record's anchor is its own data-section,
    # wherever the element sits: a landing record written into a section's body
    # belongs to that section as much as one the comments section holds.
    # Records the comments section holds come first, in the order that section
    # reads them, and body-resident records follow — write_state renders only
    # the former, so that order is what keeps the section byte-stable.
    comments: dict[str, list] = {}
    for c in sorted(_comment_elements(soup), key=_in_comments_section, reverse=True):
        sid = c.get("data-section", "_top")
        comments.setdefault(sid, []).append(_comment_record(c))
    st["comments"] = comments
    if warnings:
        st["compatibility_warnings"] = warnings
    return st


def read_state_file(path: Path) -> dict:
    """Return isolated semantic state, reparsing only when the file changes."""
    from reckon.file_memo import memoized

    return memoized(
        "read_state",
        path,
        lambda: read_state(_read_plan_text(path)),
    )


def read_state_and_text_file(path: Path) -> tuple[dict, str]:
    """Return a plan's semantic state and the exact text it was parsed from.

    The state and the text are two halves of one read: ``read_state_file`` and
    ``_read_plan_text`` are separate memoised lookups, each keyed on its own
    stat of the file, so a write landing between them pairs one file's state
    with another file's text. Callers that derive anything from both — the
    unparsed-section diagnostics read the text against the parsed state — take
    this pair instead, memoised together on one stat identity.
    """
    from reckon.file_memo import memoized

    def compute() -> tuple[dict, str]:
        text = _read_plan_text(path)
        return read_state(text), text

    return memoized("read_state_and_text", path, compute)


# ── Render ───────────────────────────────────────────────────────────────--


def _section_record_attributes(record: dict, attempts_markup: str = "") -> str:
    """Render metadata while preserving an existing legacy attempts attribute."""
    return (
        f' data-effort-hours="{_esc(record["effort_hours"])}"'
        + _capability_attributes(record["capability"])
        + attempts_markup
        + f' data-status="{_esc(record["status"])}"'
        + f' data-links="{_esc(",".join(record["links"]))}"'
    )


def _render_section_record(record: dict, attempts_markup: str = "") -> str:
    return (
        f'<section data-reckon="section" data-id="{_esc(record["id"])}"'
        + _section_record_attributes(record, attempts_markup)
        + "></section>"
    )


_LEGACY_ATTEMPTS_ATTRIBUTE_RE = re.compile(
    r"\s+data-attempts\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)


def _legacy_attempts_markup(opening: str) -> str:
    """Return the source attribute unchanged, never a value from plan state."""
    match = _LEGACY_ATTEMPTS_ATTRIBUTE_RE.search(opening.split(">", 1)[0])
    return match.group(0) if match else ""


_SECTION_RECORD_ATTRIBUTE_RE = re.compile(
    r"\s+data-(?:reckon|effort-hours|attempts|status|links|capability-(?:"
    r"version|class|reasoning|context|tool-autonomy|verification|risk))"
    r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?(?=\s|>)",
    re.IGNORECASE,
)


def _splice_section_records(html_text: str, records: list[dict]) -> str:
    """Regenerate only record spans, preserving heading text and authored prose."""
    if not records and not _RECORD_MARKER_RE.search(html_text):
        return html_text
    parser = _StructuredSectionSpanParser(html_text)
    parser.feed(html_text)
    parser.close()
    soup = BeautifulSoup(html_text, "html.parser")
    _read_section_records(soup, {})
    spans = {
        record.start: record.open_end if record.heading else record.end
        for record in parser.records
        if record.protected
    }
    pending = {record["id"]: record for record in records}
    replacements = []
    for element in _section_record_elements(soup):
        identity = section_record_id(element.attrs)
        start = parser.line_offsets[element.sourceline - 1] + element.sourcepos
        if start not in spans:
            raise ValueError(
                f"sections[{identity!r}]: record has no complete source span"
            )
        end = spans[start]
        record = pending.pop(identity, None)
        attempts_markup = _legacy_attempts_markup(html_text[start:end])
        if element.name == "h2":
            opening = _SECTION_RECORD_ATTRIBUTE_RE.sub("", html_text[start:end])
            rendered = (
                opening[:-1]
                + ' data-reckon="section"'
                + _section_record_attributes(record, attempts_markup)
                + ">"
                if record is not None
                else opening
            )
        else:
            rendered = (
                _render_section_record(record, attempts_markup)
                if record is not None
                else ""
            )
            if not rendered and html_text[end : end + 1] == "\n":
                end += 1
        replacements.append((start, end, rendered))
    for identity, record in pending.items():
        headings = [
            record.heading
            for record in parser.records
            if record.heading
            and record.heading.level == 2
            and record.heading.identity == identity
        ]
        if len(headings) != 1:
            raise ValueError(
                f"sections[{identity!r}].id: expected exactly one matching h2"
            )
        end = headings[0].heading_span[1]
        replacements.append((end, end, "\n" + _render_section_record(record)))
    for start, end, rendered in sorted(replacements, reverse=True):
        html_text = html_text[:start] + rendered + html_text[end:]
    return html_text


def _render_gates(gates: list) -> str:
    if not gates or not isinstance(gates, list):
        return ""
    items = []
    for gate in gates:
        gate = gate or {}
        gated_sections = ",".join(gate.get("gated_sections") or [])
        transition_attributes = "".join(
            f' data-{field.replace("_", "-")}="{_esc(gate[field])}"'
            for field in ("transition", "gating_plan", "decision")
            if field in gate
        )
        evidence = (
            f'<a class="r-gate-evidence" href="{_esc(gate.get("evidence"))}">Evidence</a>\n'
            if gate.get("evidence")
            else ""
        )
        items.append(
            f'<div class="r-gate" data-id="{_esc(gate.get("id"))}"'
            f' data-section="{_esc(gate.get("section"))}"'
            f' data-gated-sections="{_esc(gated_sections)}"'
            f"{transition_attributes}"
            f' data-status="{_esc(gate.get("status"))}"'
            f' data-verdict="{_esc(gate.get("verdict"))}">\n'
            f'    <h4 class="r-gate-measure">{_esc(gate.get("measure"))}</h4>\n'
            f'    <p class="r-gate-required-evidence">{_body(gate.get("required_evidence"))}</p>\n'
            f"    {evidence}</div>"
        )
    return (
        '<section data-reckon="gates" id="gates" class="r-gates">\n'
        '<h2><span class="sec">§</span> Evidence gates</h2>\n'
        + "\n".join(items)
        + "\n</section>"
    )


def _render_decisions(decisions: dict) -> str:
    if not decisions or not isinstance(decisions, dict):
        return ""
    rows = []
    for key, d in decisions.items():
        d = d or {}
        labels = d.get("option_labels") or {}
        opts = ""
        for v in d.get("choices") or []:
            label = labels.get(v, v)
            chosen = " chosen" if d.get("choice") == v else ""
            opts += f'<button class="r-opt{chosen}" data-value="{_esc(v)}">{_esc(label)}</button>\n      '
        opts_block = f'<p class="r-dec-opts">\n      {opts}</p>\n    ' if opts else ""
        ctx = (
            f'<p class="r-dec-ctx">{_esc(d.get("context"))}</p>\n    '
            if d.get("context")
            else ""
        )
        rat = (
            f'<p class="r-dec-rat">{_esc(d.get("rationale"))}</p>\n    '
            if d.get("rationale")
            else '<p class="r-dec-rat"></p>\n    '
        )
        recommended = str(d.get("recommended") or "")
        recommended_by = str(d.get("recommended_by") or "")
        recommendation_attrs = ""
        if recommended:
            recommendation_attrs += f' data-recommended="{_esc(recommended)}"'
        if recommended_by:
            recommendation_attrs += f' data-recommended-by="{_esc(recommended_by)}"'
        sections = ",".join(d.get("sections") or [])
        sections_attr = f' data-sections="{_esc(sections)}"' if sections else ""
        rows.append(
            f'<div class="r-dec" data-key="{_esc(key)}" data-choice="{_esc(d.get("choice"))}"'
            f' data-by="{_esc(d.get("by"))}" data-when="{_esc(d.get("when"))}"'
            f"{sections_attr}{recommendation_attrs}>\n    "
            f'<p class="r-dec-q">{_esc(d.get("title") or key)}</p>\n    '
            f"{ctx}{opts_block}{rat}</div>"
        )
    return (
        '<section data-reckon="decisions" id="decisions" class="r-decisions">\n'
        '<h2><span class="sec">§</span> Decisions</h2>\n'
        + "\n".join(rows)
        + "\n</section>"
    )


def _render_followups(followups: list) -> str:
    if not followups or not isinstance(followups, list):
        return ""
    arts = []
    for f in followups:
        f = f or {}
        # prompt is plain text → escaped; body / outcome are authored HTML → raw.
        prompt = (
            f'<pre class="r-fu-prompt">{_esc(f.get("prompt"))}</pre>\n    '
            if f.get("prompt")
            else ""
        )
        outcome = (
            f'<p class="r-fu-outcome">{_body(f.get("outcome"))}</p>\n    '
            if f.get("outcome")
            else ""
        )
        # Derive status from resolved_at (mirrors _render_questions). A followup
        # with a resolved_at is resolved regardless of a stale literal status —
        # resolve_followup sets resolved_at/by/outcome but not the status field.
        status = "resolved" if f.get("resolved_at") else (f.get("status") or "open")
        legacy_tier = f' data-tier="{_esc(f.get("tier"))}"' if f.get("tier") else ""
        arts.append(
            f'<article class="r-fu" data-id="{_esc(f.get("id"))}" data-status="{_esc(status)}"'
            f"{legacy_tier}{_capability_attributes(f.get('capability'))}"
            f' data-written-by="{_esc(f.get("written_by"))}"'
            f' data-written-at="{_esc(f.get("written_at"))}" data-recommends-skill="{_esc(f.get("recommends_skill"))}"'
            f' data-resolved-at="{_esc(f.get("resolved_at") or "")}" data-resolved-by="{_esc(f.get("resolved_by") or "")}">\n    '
            f'<h4 class="r-fu-title">{_esc(f.get("title"))}</h4>\n    '
            f'<div class="r-fu-body">{_body(f.get("body"))}</div>\n    '
            f"{prompt}{outcome}</article>"
        )
    return (
        '<section data-reckon="followups" id="followups" class="r-followups">\n'
        '<h2><span class="sec">§</span> Followups</h2>\n'
        + "\n".join(arts)
        + "\n</section>"
    )


def _render_questions(questions: list) -> str:
    if not questions or not isinstance(questions, list):
        return ""
    items = []
    for q in questions:
        q = q or {}
        res = (
            f'<p class="r-q-resolution">{_body(q.get("resolution"))}</p>\n    '
            if q.get("resolution")
            else ""
        )
        status = "resolved" if q.get("resolved_at") else "open"
        items.append(
            f'<div class="r-q" data-id="{_esc(q.get("id"))}" data-section="{_esc(q.get("section"))}"'
            f' data-status="{status}" data-opened-by="{_esc(q.get("opened_by"))}"'
            f' data-opened-at="{_esc(q.get("opened_at"))}"'
            f' data-resolved-at="{_esc(q.get("resolved_at") or "")}" data-resolved-by="{_esc(q.get("resolved_by") or "")}">\n    '
            f'<p class="r-q-body">{_body(q.get("body"))}</p>\n    {res}</div>'
        )
    return (
        '<section data-reckon="questions" id="questions" class="r-questions">\n'
        '<h2><span class="sec">§</span> Open questions</h2>\n'
        + "\n".join(items)
        + "\n</section>"
    )


def _render_research(research: list) -> str:
    if not research or not isinstance(research, list):
        return ""
    items = []
    for r in research:
        r = r or {}
        url = f' data-url="{_esc(r.get("url"))}"' if r.get("url") else ""
        title = _esc(r.get("title"))
        title_html = (
            f'<a href="{_esc(r.get("url"))}">{title}</a>' if r.get("url") else title
        )
        items.append(
            f'<div class="r-research" data-id="{_esc(r.get("id"))}" data-type="{_esc(r.get("type"))}"'
            f' data-source="{_esc(r.get("source"))}" data-added-by="{_esc(r.get("added_by"))}"'
            f' data-when="{_esc(r.get("when"))}"{url}>\n    '
            f'<span class="r-research-title">{title_html}</span></div>'
        )
    return (
        '<section data-reckon="research" id="research" class="r-research-list">\n'
        '<h2><span class="sec">§</span> Research</h2>\n'
        + "\n".join(items)
        + "\n</section>"
    )


def merge_comment_collections(current: object, incoming: object) -> dict[str, list]:
    """Union two section-addressed comment collections without dropping an entry.

    Comments are append-only, so a write carrying fewer of them than the file
    already holds is a writer whose base revision moved under it: the entries it
    lacks are a concurrent append rather than a removal. Merging by section and
    id makes that append commutative — whichever writer lands second keeps both
    — while an entry the file holds first keeps its position, so the result does
    not depend on which writer arrived last.
    """
    merged: dict[str, list] = {}
    for source in (current, incoming):
        if not isinstance(source, dict):
            continue
        for section, items in source.items():
            if not isinstance(items, list):
                continue
            bucket = merged.setdefault(str(section), [])
            seen = {
                str(item.get("id") or "")
                for item in bucket
                if isinstance(item, dict) and item.get("id")
            }
            for item in items:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id") or "")
                if ident and ident in seen:
                    continue
                bucket.append(item)
                if ident:
                    seen.add(ident)
    return merged


def _comments_in_section(comments: object, body_resident: Counter) -> dict:
    """The comment records a write renders into the comments section.

    A record the document already holds in a section's own body stays where it
    is: rendering it here as well would be the second copy the read widening
    must not create. Matching is by the record's own fields, counted, so a
    document holding one record in both places keeps both.
    """
    if not isinstance(comments, dict):
        return {}
    remaining = Counter(body_resident)
    kept: dict[str, list] = {}
    for sid, items in comments.items():
        bucket = []
        for raw in items or []:
            item = raw or {}
            identity = _comment_identity(item)
            if remaining[identity]:
                remaining[identity] -= 1
                continue
            bucket.append(item)
        if bucket:
            kept[sid] = bucket
    return kept


def _render_comments(comments: dict) -> str:
    if not comments or not isinstance(comments, dict):
        return ""
    items = []
    for sid, arr in comments.items():
        for c in arr or []:
            c = c or {}
            quote = f' data-quote="{_esc(c.get("quote"))}"' if c.get("quote") else ""
            items.append(
                f'<div class="r-comment" data-section="{_esc(sid)}" data-id="{_esc(c.get("id"))}"'
                f' data-who="{_esc(c.get("who"))}" data-when="{_esc(c.get("when"))}"{quote}>\n    '
                f'<div class="r-comment-body">{_body(c.get("body"))}</div>\n</div>'
            )
    if not items:
        return ""
    return (
        '<section data-reckon="comments" id="comments" class="r-comments">\n'
        + "\n".join(items)
        + "\n</section>"
    )


_RENDERERS = {
    "gates": _render_gates,
    "decisions": _render_decisions,
    "followups": _render_followups,
    "questions": _render_questions,
    "research": _render_research,
    "comments": _render_comments,
}


def _set_meta(html_text: str, name: str, content: str) -> str:
    """Insert or replace <meta name="plan-NAME" content="..."> in <head>."""
    tag = f'<meta name="{name}" content="{_esc(content)}">'
    pat = re.compile(rf'<meta\s+name="{re.escape(name)}"[^>]*>', re.IGNORECASE)
    if pat.search(html_text):
        return pat.sub(tag, html_text, count=1)
    # insert before </head>, else after <body>
    idx = html_text.lower().find("</head>")
    if idx != -1:
        return html_text[:idx] + tag + "\n" + html_text[idx:]
    return tag + "\n" + html_text


def _remove_meta(html_text: str, name: str) -> str:
    """Remove a meta tag while preserving all unrelated authored HTML."""
    pat = re.compile(
        rf'<meta\b(?=[^>]*\bname=["\']{re.escape(name)}["\'])[^>]*>\s*',
        re.IGNORECASE,
    )
    return pat.sub("", html_text)


def _metas_are_contiguous(html_text: str, names: list[str]) -> bool:
    """True when each name occurs exactly once, in order, separated by whitespace only."""
    spans: list[tuple[int, int]] = []
    for name in names:
        matches = list(
            re.finditer(
                rf'<meta\s+name="{re.escape(name)}"[^>]*>', html_text, re.IGNORECASE
            )
        )
        if len(matches) != 1:
            return False
        spans.append(matches[0].span())
    for (_, end), (start, _) in pairwise(spans):
        if start < end or html_text[end:start].strip():
            return False
    return True


def _set_meta_block(html_text: str, pairs: list[tuple[str, str]]) -> str:
    """Insert or replace several ``<meta>`` tags as ONE contiguous block.

    Three lifecycle scalars written as three independently positionable lines
    are three independently unionisable hunks: a merge of two landing records
    takes a line from each, and the document then carries two ``plan-version``
    lines whose writer updates one and whose reader takes the other. One
    contiguous block makes a three-way merge treat them as a single hunk.

    An existing document is changed as little as the block allows. A document
    that already carries every name contiguously is rewritten in place, and
    otherwise the block is written where the FIRST pair's tag already sits —
    for the lifecycle scalars, where ``plan-impl`` sits when it is emitted —
    so that tag holds its position and only the later names move to it. The
    placement is not tidiness: a writer that relocates a scalar another reader
    or test pins in place rewrites bytes no edit asked it to rewrite, which is
    churn in every plan on every write. Every later occurrence of a name is
    removed, so a document that already carries duplicates converges to one
    copy, and a document carrying none gets the block before ``</head>``.
    """
    if not pairs:
        return html_text
    names = [name for name, _ in pairs]
    block = "\n".join(
        f'<meta name="{name}" content="{_esc(content)}">' for name, content in pairs
    )
    if _metas_are_contiguous(html_text, names):
        out = html_text
        for name, content in pairs:
            out = _set_meta(out, name, content)
        return out
    anchor = re.search(
        rf'<meta\s+name="{re.escape(names[0])}"[^>]*>', html_text, re.IGNORECASE
    )
    if anchor is None:
        out = html_text
        for name in names:
            out = _remove_meta(out, name)
        idx = out.lower().find("</head>")
        if idx != -1:
            return out[:idx] + block + "\n" + out[idx:]
        return block + "\n" + out
    head = html_text[: anchor.start()]
    rest = html_text[anchor.start() :]
    for name in names:
        head = _remove_meta(head, name)
        rest = _remove_meta(rest, name)
    return head + block + "\n" + rest


# A section the parser selects nothing from can still hold authored content in
# a spelling the parser does not recognise. read_state reports that as an empty
# collection — indistinguishable on its own from "nothing was authored" — and a
# read-modify-write then splices the empty collection over the section, silently
# deleting the authored items on the first structured write. Refuse instead: a
# rewrite must never delete what the model never contained, whatever the cause.
# Only element children the renderers would not regenerate count as authored
# items; structural section headings are scaffolding and never trigger refusal.
_STRUCTURAL_SECTION_CHILDREN = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "header"})

_SECTION_RECOGNISED_SPELLING = {
    "gates": '<div class="r-gate" data-id="...">',
    "decisions": '<div class="r-dec" data-key="...">',
    "followups": '<article class="r-fu" data-id="...">',
    "questions": '<div class="r-q" data-id="...">',
    "research": '<div class="r-research" data-id="...">',
    "comments": '<div class="r-comment" data-section="...">',
}


class UnparsedSectionWriteError(ValueError):
    """Raised when a write would delete authored section content the parser
    never recognised — the parsed collection is empty but the section is not."""


def _authored_child_count(html_text: str, reckon_id: str) -> int:
    """Count direct element children of section[data-reckon=ID] the renderers
    do not regenerate — the authored items a splice would remove."""
    soup = BeautifulSoup(html_text, "html.parser")
    section = soup.select_one(f'section[data-reckon="{reckon_id}"]')
    if section is None:
        return 0
    return sum(
        1
        for child in section.children
        if getattr(child, "name", None)
        and child.name not in _STRUCTURAL_SECTION_CHILDREN
    )


def _reject_emptying_unparsed_section(out: str, reckon_id: str, parsed: object) -> None:
    """Raise if splicing `parsed` over section `reckon_id` would delete
    authored children the parser never recognised."""
    if parsed:
        return
    count = _authored_child_count(out, reckon_id)
    if count == 0:
        return
    spelling = _SECTION_RECOGNISED_SPELLING.get(reckon_id)
    expected = f" Expected spelling: {spelling}" if spelling else ""
    raise UnparsedSectionWriteError(
        f'refusing plan write: re-rendering section data-reckon="{reckon_id}" '
        f"would empty it — it holds {count} authored child element(s) the "
        f"parser did not recognise (the parsed collection is empty).{expected}"
    )


def _splice_section(html_text: str, reckon_id: str, rendered: str) -> str:
    """Replace <section data-reckon="ID">…</section> with `rendered`
    (removes it when `rendered` is empty); inserts before </main> otherwise."""
    pat = re.compile(
        rf'<section[^>]*data-reckon="{re.escape(reckon_id)}"[^>]*>.*?</section>',
        re.IGNORECASE | re.DOTALL,
    )
    if pat.search(html_text):
        return (
            pat.sub(lambda _: rendered, html_text, count=1)
            if rendered
            else pat.sub("", html_text, count=1)
        )
    if not rendered:
        return html_text
    for anchor in ("</main>", "</body>", "</html>"):
        idx = html_text.lower().rfind(anchor)
        if idx != -1:
            return html_text[:idx] + rendered + "\n" + html_text[idx:]
    return html_text + "\n" + rendered


def _impl_is_carried_by_records(state: dict, html_text: str) -> bool:
    """Whether this plan's records answer for its impl around this write.

    Records carry the figure exactly when the coverage rule answers: a plan
    whose records cover every declared non-deferred section holds its impl
    there, and one whose records are partial keeps its authored figure, so the
    meta write must proceed for it. A state naming no record leaves the
    question to the document, where the parser's own selector settles it — a
    text match would also fire on prose that quotes the record syntax, and a
    plan documenting the contract would then lose an authored write it must
    keep, and a plan whose records are being removed stays byte-stable beyond
    the record spans, its state's figure being the one those records derived.
    """
    if (
        derive_impl_from_sections(
            state.get("sections"), state.get("section_declarations")
        )
        is not None
    ):
        return True
    if state.get("sections"):
        return False
    return bool(_section_record_elements(BeautifulSoup(html_text or "", "html.parser")))


def write_state(html_text: str, state: dict) -> str:
    """Regenerate the reckon-owned meta + sections from `state`.

    Authored prose (everything outside the data-reckon sections) is untouched.
    """
    out = html_text
    artifact_type = _canonical_type(state.get("type", "plan"))
    if state.get("type"):
        out = _set_meta(out, "reckon-type", artifact_type)
    if artifact_type != "plan":
        for meta_name in _PLAN_ONLY_METAS:
            out = _remove_meta(out, meta_name)
        out = _splice_section_records(out, [])
    for f in _SCALARS:
        if f == "modified":
            # Emitted with impl and version as one block, below.
            continue
        if f in state and state[f] is not None:
            out = _set_meta(out, f"plan-{f.replace('_', '-')}", state[f])
    if "effort_hours" in state and state.get("effort_calibrated") is not False:
        out = _set_meta(out, "plan-effort-hours", state["effort_hours"])
    if state.get("wall_clock_hours") is not None:
        out = _set_meta(out, "plan-wall-clock-hours", state["wall_clock_hours"])
    for f in _LIST_SCALARS:
        if f in state:
            values = state.get(f) or []
            if f == "tags":
                values = list(dict.fromkeys(normalise_tag(tag) for tag in values))
            out = _set_meta(out, f"plan-{f.replace('_', '-')}", ",".join(values))
    if "capability" in state and state.get("capability"):
        capability = state["capability"]
        requirements = capability.get("requirements") or {}
        out = _set_meta(
            out,
            "plan-capability-version",
            capability.get("version") or CAPABILITY_SCHEMA_VERSION,
        )
        out = _set_meta(
            out,
            "plan-capability-class",
            capability.get("class") or "",
        )
        for key in _CAPABILITY_REQUIREMENTS:
            meta_name = f"plan-capability-{key.replace('_', '-')}"
            if requirements.get(key):
                out = _set_meta(out, meta_name, requirements[key])
            else:
                out = _remove_meta(out, meta_name)
        if "tier" not in state:
            out = _remove_meta(out, "plan-tier")
    # impl, version and modified are the lifecycle scalars a landing merge
    # rewrites on both sides, so they are emitted as one contiguous block: a
    # single hunk for a three-way merge rather than three independently
    # unionisable lines. A plan carrying records holds its impl as records, so
    # that figure stays out of the block there — on either side of the write,
    # since regeneration must stay byte-stable whether the records are on disk
    # or only in the state, and a derived figure must never be stored as the
    # authored one.
    lifecycle_block: list[tuple[str, str]] = []
    if "impl" in state and not _impl_is_carried_by_records(state, html_text):
        lifecycle_block.append(("plan-impl", state["impl"]))
    if "version" in state:
        lifecycle_block.append(("plan-version", int(state.get("version") or 0)))
    if state.get("modified") is not None:
        lifecycle_block.append(("plan-modified", state["modified"]))
    out = _set_meta_block(out, lifecycle_block)
    if artifact_type == "plan" and "section_declarations" in state:
        declarations = state.get("section_declarations") or {}
        out = _set_meta(
            out,
            "plan-section-declarations",
            json.dumps(declarations, ensure_ascii=True, separators=(",", ":")),
        )
    if artifact_type == "plan" and "sections" in state:
        sections = PlanState.model_validate(
            {
                "sections": state["sections"],
                "section_declarations": state.get("section_declarations", {}),
            }
        ).canonical_dump()["sections"]
        out = _splice_section_records(out, sections)
    # Records the source holds in a section's own body are not rendered into
    # the comments section: the write would otherwise copy them there, and they
    # are already where they belong. Judged against the source rather than the
    # state so a writer that never carried them still cannot duplicate them.
    body_resident_comments = _body_resident_comment_counts(html_text)
    for sid in SECTION_IDS:
        if sid in state:
            collection = state[sid]
            if sid == "comments":
                collection = _comments_in_section(collection, body_resident_comments)
            _reject_emptying_unparsed_section(out, sid, collection)
            out = _splice_section(out, sid, _RENDERERS[sid](collection))
    return out


# ── Schema-typed wrappers (PlanState contract) ──────────────────────────────
#
# These WRAP read_state/write_state — they do not replace them. read_state and
# write_state keep their dict signatures and current output; existing callers
# (serve.py, _store.py) stay untouched. Explicit write-boundary callers use
# from_html / validate_for_write.


def from_html(html_text: str) -> PlanState:
    """Parse HTML into a typed :class:`reckon._schema.PlanState` — LENIENT.

    Equivalent to ``PlanState.model_validate(read_state(html_text))`` with the
    schema's lenient coercion (roi med→mid, type doc→research, derived statuses,
    unknown attrs dropped). Legacy scalar fields retain read defaults. Explicit
    typed section records validate on read and refuse invalid metadata. Use
    :meth:`PlanState.validate_for_write` for the strict write path.
    """
    return PlanState.model_validate(read_state(html_text))


def to_html(html_text: str, state: PlanState) -> str:
    """Render a typed :class:`PlanState` back into HTML.

    Equivalent to ``write_state(html_text, state.canonical_dump())``. The
    canonical dump uses ``exclude_unset`` so the regenerated meta + sections
    match what ``write_state(html_text, read_state(html_text))`` would produce
    on a round-trip (byte-identical reckon-owned sections). ``state.project`` is
    carried in the dump but write_state ignores it — the authored docs-project
    meta survives untouched.
    """
    return write_state(html_text, state.canonical_dump())


# ── Lightweight inventory record (scales to thousands of docs) ──────────────

# A decision is OPEN until it has a choice OR a recorded rationale — mirrors the
# SPA decision widget's isTaken predicate (docs/ui/decision.jsx). A
# rationale-only decision is taken, so the inventory must not report it as
# open. The block regex captures each whole .r-dec element
# (decisions contain only <p> children, so the first </div> closes it) so the
# fast inventory path can inspect both data-choice and the .r-dec-rat text.
_DEC_BLOCK_RE = re.compile(
    r'<div\b[^>]*\bclass="r-dec".*?</div>', re.IGNORECASE | re.DOTALL
)
_DEC_CHOICE_RE = re.compile(r'\bdata-choice="([^"]*)"', re.IGNORECASE)
_DEC_RAT_RE = re.compile(r'class="r-dec-rat"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_PROSE_SKIP_TAGS = frozenset({"script", "style", "head"})


def _strip_tags(text: str) -> str:
    """Extract entity-decoded prose, omitting non-prose element interiors."""
    for tag in _PROSE_SKIP_TAGS:
        text = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}\s*>",
            " ",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    text = re.sub(r"<!--.*?-->|<![^>]*>|<\?[^>]*>", " ", text, flags=re.DOTALL)
    return " ".join(_htmlmod.unescape(_TAG_RE.sub(" ", text)).split())


def _decision_open(choice: str | None, rationale: str | None) -> bool:
    """True iff a decision has neither a choice nor a rationale (i.e. untaken)."""
    return not ((choice or "").strip() or (rationale or "").strip())


def count_open_decisions(text: str) -> int:
    """Regex-count open decisions from raw plan HTML (fast inventory path).

    Honours rationale: a .r-dec with empty data-choice but non-empty .r-dec-rat
    is taken, not open — matching the widget and parse_plan.
    """
    n = 0
    for block in _DEC_BLOCK_RE.findall(text):
        cm = _DEC_CHOICE_RE.search(block)
        choice = cm.group(1) if cm else ""
        rm = _DEC_RAT_RE.search(block)
        rationale = _TAG_RE.sub("", rm.group(1)) if rm else ""
        if _decision_open(choice, rationale):
            n += 1
    return n


_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
# an attribute value runs to the quote that opened it, so a double-quoted value may
# carry apostrophes and a single-quoted one double quotes
_NAME_RE = re.compile(r'\bname=(["\'])(.+?)\1', re.IGNORECASE)
_CONTENT_RE = re.compile(r'\bcontent=(["\'])(.*?)\1', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _read_plan_text(path: Path) -> str:
    """Read one plan's bytes once while its stat identity is unchanged."""
    from reckon.file_memo import memoized

    def read() -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    return memoized("plan_text", path, read)


def parse_meta(path: Path, slug: str | None = None) -> dict:
    """Fast inventory record: <meta> + <title> + a regex open-decision count,
    parsed by regex (no bs4) so a project with thousands of docs stays cheap.
    Full state is read per-doc via parse_plan. The record is memoised against
    the file's stat identity, so an unchanged file is read once.
    """
    from reckon.file_memo import memoized

    return memoized(
        "parse_meta", path, lambda: _parse_meta_uncached(path, slug), variant=slug
    )


def _parse_meta_uncached(path: Path, slug: str | None) -> dict:
    text = _read_plan_text(path)
    head = text[:16384]
    rec = dict(_DEFAULTS)
    metas: dict[str, str] = {}
    duplicated: list[str] = []
    for tag in _META_RE.findall(head):
        nm = _NAME_RE.search(tag)
        if not nm:
            continue
        key = nm.group(2).lower()
        if key in metas:
            if key.startswith("plan-"):
                duplicated.append(key)
            continue
        ct = _CONTENT_RE.search(tag)
        metas[key] = ct.group(2) if ct else ""
    for name, content in metas.items():
        if not name.startswith("plan-") or content == "":
            continue
        field = name[len("plan-") :].replace("-", "_")
        if field in _SCALARS:
            rec[field] = content
        elif field in _LIST_SCALARS:
            rec[field] = [x.strip() for x in content.split(",") if x.strip()]
        elif field in ("impl", "effort_hours", "wall_clock_hours"):
            try:
                rec[field] = float(content)
            except ValueError:
                pass
        elif field == "version":
            try:
                rec["version"] = int(content)
            except ValueError:
                pass
    capability = _capability_from_values(
        metas,
        prefix="plan-capability-",
    )
    if capability:
        rec["capability"] = capability
    elif rec.get("tier"):
        mapped, diagnostic = from_legacy_tier(rec["tier"])
        if mapped:
            rec["capability"] = mapped
            rec["compatibility_warnings"] = [f"plan: {diagnostic}"]
    warnings = list(rec.get("compatibility_warnings") or [])
    for duplicate_name in dict.fromkeys(duplicated):
        warnings.append(
            f"{duplicate_name}: duplicated in the document; the first "
            "occurrence in document order wins and every later copy is ignored"
        )
    # The record contract is validated in one place only: a document carrying
    # record metadata is read by the parser, so this path can never report a
    # figure the parsed read refuses to produce.
    if _document_carries_records(text):
        try:
            parsed_state = read_state(text)
        except ValueError:
            parsed_state = None
        derived = (
            None
            if parsed_state is None
            else derive_impl_from_sections(
                parsed_state.get("sections"), parsed_state.get("section_declarations")
            )
        )
        if derived is not None:
            rec["impl"] = derived
            rec["impl_source"] = "computed"
        elif "plan-impl" in metas:
            rec["impl_source"] = "authored"
    authored_hours = "plan-effort-hours" in metas and "effort_hours" in rec
    legacy_effort = metas.get("plan-effort", "")
    if authored_hours:
        rec["effort_calibrated"] = True
        if legacy_effort:
            warnings.append(
                f"effort: legacy letter {legacy_effort!r} is redundant; "
                "explicit worker-hours win"
            )
    elif legacy_effort in LEGACY_EFFORT_HOURS:
        rec["effort_hours"] = LEGACY_EFFORT_HOURS[legacy_effort]
        rec["effort_calibrated"] = False
        warnings.append(
            f"effort: legacy letter {legacy_effort!r} maps to "
            f"{rec['effort_hours']:.1f} worker-hours; plan is uncalibrated"
        )
    if warnings:
        rec["compatibility_warnings"] = warnings
    rec["type"] = _canonical_type(metas.get("reckon-type"))
    tm = _TITLE_RE.search(head)
    if tm and not rec.get("title"):
        rec["title"] = tm.group(1).strip().split("|")[0].strip()
    rec["slug"] = slug or rec.get("slug") or path.stem
    rec["title"] = rec.get("title") or rec["slug"]
    rec["informs"] = rec.get("informs") or []
    rec["evidence_for"] = rec.get("evidence_for") or []
    rec["verifies"] = rec.get("verifies") or []
    rec["supersedes"] = rec.get("supersedes") or []
    rec["commits"] = rec.get("commits") or []
    rec["artifacts"] = rec.get("artifacts") or []
    rec["depends_on"] = rec.get("depends_on") or []
    rec["after"] = rec.get("after") or []
    rec["dec_open"] = count_open_decisions(text)
    rec["impl"] = float(rec.get("impl", 0) or 0)
    rec["version"] = int(rec.get("version", 0) or 0)
    rec["blockers"] = 0
    return rec


# ── Plan record (inventory + full state) ────────────────────────────────────


def parse_plan(path: Path, slug: str | None = None) -> dict:
    """Return an isolated full-plan record, reparsing only changed files."""
    from reckon.file_memo import memoized

    return memoized(
        "parse_plan", path, lambda: _parse_plan_uncached(path, slug), variant=slug
    )


def _parse_plan_uncached(path: Path, slug: str | None) -> dict:
    text = _read_plan_text(path)
    st = read_state(text)
    rec = dict(_DEFAULTS)
    rec.update({k: v for k, v in st.items() if v is not None})
    rec["slug"] = slug or st.get("slug") or path.stem
    rec["title"] = st.get("title") or rec["slug"]
    rec["type"] = st.get("type") or "plan"
    rec["informs"] = st.get("informs") or []
    rec["evidence_for"] = st.get("evidence_for") or []
    rec["verifies"] = st.get("verifies") or []
    rec["supersedes"] = st.get("supersedes") or []
    rec["commits"] = st.get("commits") or []
    rec["artifacts"] = st.get("artifacts") or []

    decisions_map = st.get("decisions") or {}
    rec["decisions"] = [
        {
            "key": k,
            **{kk: vv for kk, vv in (d or {}).items()},
            "chosen": (d or {}).get("choice", ""),
        }
        for k, d in decisions_map.items()
    ]
    rec["followups"] = st.get("followups") or []
    rec["comments"] = st.get("comments") or {}
    rec["questions"] = st.get("questions") or []
    rec["research"] = st.get("research") or []
    rec["depends_on"] = st.get("depends_on") or []
    rec["after"] = st.get("after") or []
    rec["blocks"] = st.get("blocks") or []
    rec["dec_open"] = sum(
        1
        for d in rec["decisions"]
        if _decision_open(d.get("choice"), d.get("rationale"))
    )
    rec["blockers"] = int(st.get("blockers", 0) or 0)
    rec["impl"] = float(rec.get("impl", 0) or 0)
    rec["version"] = int(st.get("version", 0) or 0)
    return rec
