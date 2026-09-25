#!/usr/bin/env python3
"""Doc-audit — validate an authored plan/doc HTML against the SPA render contract.

The reckon SPA renders authored HTML *faithfully* via a raw-HTML passthrough
(``docs/ui/plan.jsx``): no markdown is rendered, the doc's ``<head><style>`` is
dropped, and images resolve against the project mount (``/<project>/...``). A
doc that relies on markdown, head-local CSS, or relative image paths renders
wrong. This module flags those problems before they ship.

Contract enforced (see AGENTS.md "agents author HTML directly"):
  - Body fields (comment / followup / question bodies, prose) are HTML —
    NOT markdown. Literal ``**bold**`` / leading ``- ``/``# `` render verbatim.
  - Images must use a project-absolute ``src="/<project>/figures/..."`` —
    relative ``src="figures/..."`` 404s under the no-trailing-slash plan URL.
  - ``<head><style>`` is dropped by the SPA — doc-local CSS never applies.
  - ``<pre>`` lines over ~120 chars wrap (informational — handled by CSS now).
  - Required plan or typed-resource metadata must be present.
  - Stub prose ("See state §…", empty reckon sections) is flagged.
  - Internal links (<a href> and plan-* meta slug references) must resolve to
    an existing doc file or in-page anchor id.

Severity: ERROR (exit non-zero) for things that render visibly wrong;
WARN for fragile-but-rendering; INFO for advisory.

Usage:
    python -m reckon.doccheck docs/my-plan.html
    reckon audit-doc docs/my-plan.html            # via the reckon CLI
    reckon audit-doc docs/*.html                  # multiple files
    reckon audit-doc docs/*.html --check-links    # also check internal links
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from reckon import _plan_html
from reckon._schema import (
    parse_plan_ref,
    standalone_reason,
    unwired_plan_message,
    unwired_plan_severity,
)
from reckon._store import (
    PLAN_SUMMARY_MAX_LENGTH,
    _mounts_path,
    _section_contract_refusal,
    plan_summary_length,
)
from reckon.lifecycle import TERMINAL_STATUSES

# Required scalar meta tags for plan-family documents. Research, evidence, and
# general documents relax ``status``. Distributed project-state resources use
# their native reckon identity/version metadata instead.
_REQUIRED_META = ("plan-slug", "plan-status")
_TYPED_RESOURCE_META = ("reckon-id", "reckon-version")
_TYPED_RESOURCE_TYPES = {"sprint", "milestone", "blocker", "timeline", "review"}
# Body-bearing classes the SPA renders as HTML (markdown there renders verbatim).
_BODY_CLASSES = (
    "r-comment-body",
    "r-fu-body",
    "r-fu-outcome",
    "r-q-body",
    "r-q-resolution",
)
# Markdown tells we should never see in a rendered body / prose paragraph.
_MD_BOLD = re.compile(r"\*\*[^*\n]+\*\*")
_MD_LEADING = re.compile(r"^\s*(?:[-*+]\s+|#{1,6}\s+)", re.MULTILINE)
_STUB_PROSE = re.compile(r"\bSee (?:state|plan) §|^\s*TODO\b|^\s*TBD\b", re.IGNORECASE)
_PRE_LINE_LIMIT = 120
# The scalar family whose duplicate the reader resolves last-wins: two lines,
# one value read, and a writer updating the other line reports success while
# the surface keeps the old value.
_SCALAR_META_PREFIX = "plan-"
# Tags whose authored imbalance the parser repairs into a well-formed tree, so
# every later check reads the repaired document and the audit says OK. Each is
# counted from the raw tag stream (see _StructureScanner).
_BALANCE_TAGS = ("tr",)
# Elements that carry no end tag. A stack of open elements built from the tag
# stream pops on each end tag, so a void element that is never pushed keeps the
# stack aligned with HTML's own element nesting.
_VOID_ELEMENTS = frozenset(
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
# The class that marks a section collapsed to its landed summary card. The
# collapse-on-landing template puts a <header> as the section's direct child, so
# a header under one of these is presentation, not a spliced document header.
_LANDED_SECTION_CLASS = "section-landed"

# ── Section contract ───────────────────────────────────────────────────────
#
# A section is the unit that carries a stated effort, a capability and a place
# in the roadmap; the plan's computed figure and its dispatch route both read
# it. Two authored shapes leave work outside that unit: a section declared
# implementable without a typed record, and a followup whose body states
# section-sized work. Neither holds the fleet: the section finding is an error
# only where the plan already carries a record to compare against, and the
# followup finding is a warning, so plans that predate the contract stay
# auditable while they are converted.
_SECTION_WITHOUT_CONTRACT = "section-without-contract"
_FOLLOWUP_DESCRIBES_SECTION_WORK = "followup-describes-section-work"
_SECTION_RECORD_INVALID = "section-record-invalid"

# Section identities are numbered from one: s0 states why the plan exists and
# carries no work.
_NUMBERED_SECTION_ID = re.compile(r"s[1-9][0-9]*")

# A separately-stated deliverable in a followup body — the corpus's own
# convention for a multipart remainder ("(1) … (2) …").
_FOLLOWUP_WORK_ITEM = re.compile(r"\(\s*\d+\s*\)\s*\S")

# Number of separately-stated deliverables at which a followup body describes
# section-sized work rather than a remainder. A followup states no effort of
# its own — the record has no such field, and an explicit hour figure appears
# in 1 of 468 open followups over the mounted projects (measured 2026-09-25) —
# so the work a body describes is sized by the deliverables it enumerates: one
# item is a remainder, two or more is work the plan's own unit must carry.
FOLLOWUP_WORK_ITEMS_THRESHOLD = 2

SEVERITIES = ("error", "warn", "info")
ACTIVE_PLAN_STALE_AFTER_DAYS = 30
OPEN_RESEARCH_STALE_AFTER_DAYS = 60
UNAUTHORISED_PLAN_STALE_AFTER_DAYS = 60


@dataclass
class Finding:
    severity: str  # "error" | "warn" | "info"
    code: str
    message: str

    def fmt(self) -> str:
        glyph = {"error": "✗", "warn": "!", "info": "·"}[self.severity]
        return f"  {glyph} [{self.code}] {self.message}"


@dataclass(frozen=True)
class LifecycleFinding:
    project: str
    slug: str
    flag: str
    age_days: int
    impl: float | None
    last_modified: str
    summary_length: int | None = None


@dataclass(frozen=True)
class ForeignFollowupFinding:
    """An open followup names a plan outside its own project.

    ``flag`` distinguishes the resolution condition so an unmounted or missing
    target can never be mistaken for a terminal one:
      - ``FOLLOWUP_FOREIGN_TERMINAL``: target resolves and its status is terminal
        (shipped/done/archived/superseded/abandoned/historical/reference)
      - ``FOLLOWUP_FOREIGN_UNMOUNTED``: the target project is not a registered
        mount (or its docs directory is absent)
      - ``FOLLOWUP_FOREIGN_MISSING``: the target project is mounted but no plan
        with that slug resolves there
    ``slug``/``project`` are the holding plan; the finding names both the
    holder and its foreign target.
    """

    project: str
    slug: str
    flag: str
    age_days: int
    impl: float | None
    last_modified: str
    target_project: str
    target_slug: str
    target_status: str
    followup_id: str


def modified_age_days(
    last_modified: str | None,
    *,
    today: date | None = None,
) -> int | None:
    """Return whole calendar days since an ISO-formatted modification date."""

    if not last_modified:
        return None
    try:
        modified = date.fromisoformat(str(last_modified)[:10])
    except ValueError:
        return None
    return max(0, ((today or date.today()) - modified).days)


def derived_plan_age(
    last_modified: str | None,
    *,
    created_at: int | float | str | None = None,
    fallback_path: Path | None = None,
    today: date | None = None,
) -> tuple[int | None, str]:
    """Return plan age and the semantic or filesystem source used to derive it."""

    age_days = modified_age_days(last_modified, today=today)
    if age_days is not None:
        return age_days, "plan-modified"

    reference_day = today or date.today()
    if created_at not in (None, ""):
        try:
            created_day = datetime.fromtimestamp(float(created_at)).date()
        except (OSError, OverflowError, TypeError, ValueError):
            created_day = None
        if created_day is not None:
            return max(0, (reference_day - created_day).days), "file-created"

    if fallback_path is not None:
        try:
            modified_day = datetime.fromtimestamp(fallback_path.stat().st_mtime).date()
        except (OSError, OverflowError, ValueError):
            modified_day = None
        if modified_day is not None:
            return max(0, (reference_day - modified_day).days), "file-mtime"

    return None, "unknown"


def lifecycle_staleness(
    *,
    doc_type: str,
    status: str,
    impl: float | None,
    age_days: int | None,
) -> str:
    """Return the advisory freshness verdict used by lifecycle auditing."""

    if age_days is None:
        return "unknown"
    if doc_type == "research":
        is_stale = (
            status not in {"done", "archived"}
            and age_days > OPEN_RESEARCH_STALE_AFTER_DAYS
        )
    elif doc_type == "evidence":
        is_stale = False
    else:
        is_stale = (
            status == "active"
            and (impl or 0.0) < 1.0
            and age_days > ACTIVE_PLAN_STALE_AFTER_DAYS
        )
    return "stale" if is_stale else "current"


def authorisation_staleness(*, status: str, age_days: int | None) -> str:
    """Return the advisory age verdict for authored but unauthorised plans."""

    if status != "draft":
        return "not_applicable"
    if age_days is None:
        return "unknown"
    return "stale" if age_days > UNAUTHORISED_PLAN_STALE_AFTER_DAYS else "current"


def _visible_text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _prose_text(el) -> str:
    """Visible text with the verbatim subtrees removed.

    Text inside ``<pre>`` and ``<code>`` renders literally, so an asterisk
    pair there is the subject a document is quoting rather than markdown a
    reader was meant to see rendered.
    """

    if not el:
        return ""
    parts: list[str] = []
    for node in el.find_all(string=True):
        for parent in node.parents:
            if getattr(parent, "name", None) in ("pre", "code"):
                break
        else:
            text = str(node).strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def _load_mounts() -> dict[str, Path]:
    mounts_file = _mounts_path()
    if not mounts_file.exists():
        return {}
    try:
        raw = json.loads(mounts_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    mounts: dict[str, Path] = {}
    for project, docs_dir in raw.items():
        if isinstance(project, str) and isinstance(docs_dir, str):
            mounts[project] = Path(docs_dir).expanduser().resolve()
    return mounts


def _iter_doc_files(docs_dir: Path, project: str):
    from reckon.resources import resource_map

    for resource in resource_map(docs_dir, project, include_archived=False).values():
        if resource.type != "sprint":
            yield resource.path


def _read_lifecycle_state(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "slug": path.stem,
            "type": "plan",
            "status": "",
            "impl": None,
            "summary": "",
        }
    state = _plan_html.read_state(text)
    impl = state.get("impl")
    return {
        "slug": (state.get("slug") or path.stem),
        "type": ((state.get("type") or "plan").strip().lower()),
        "status": ((state.get("status") or "").strip().lower()),
        "impl": float(impl) if impl is not None else None,
        "summary": str(state.get("summary") or ""),
        "followups": state.get("followups") or [],
    }


#: Flag codes for followups that point outside their owning project. The
#: unmounted and missing conditions are deliberately distinct codes from the
#: terminal one so a target the audit cannot resolve is never read as shipped.
FOLLOWUP_FOREIGN_TERMINAL = "FOLLOWUP_FOREIGN_TERMINAL"
FOLLOWUP_FOREIGN_UNMOUNTED = "FOLLOWUP_FOREIGN_UNMOUNTED"
FOLLOWUP_FOREIGN_MISSING = "FOLLOWUP_FOREIGN_MISSING"

# A project-qualified plan ref ``project:slug[#stage]`` — the only ref form
# that can point outside the owning project. Segments mirror the grammar in
# ``_schema.parse_plan_ref``, which confirms each match.
_FOREIGN_REF_RE = re.compile(
    r"(?P<project>[A-Za-z0-9][A-Za-z0-9_-]*):"
    r"(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:#(?P<stage>[A-Za-z0-9][A-Za-z0-9._-]*))?"
)


def _external_followup_refs(followup: Mapping[str, Any]) -> list[str]:
    """Return project-qualified plan refs an open followup names.

    Ref extraction reads the machine-facing invocation fields (the skill it
    recommends and the dispatch prompt it carries), not the prose body, so a
    ``word:word`` resemblance in free text is not treated as a target.
    """

    raw = " ".join(
        value
        for value in (
            followup.get("recommends_skill"),
            followup.get("prompt"),
        )
        if isinstance(value, str)
    )
    refs: list[str] = []
    for match in _FOREIGN_REF_RE.finditer(raw):
        token = match.group(0)
        if parse_plan_ref(token) is not None and token not in refs:
            refs.append(token)
    return refs


def _foreign_followup_findings(
    *,
    followups: list[Any],
    holding_project: str,
    holding_slug: str,
    age_days: int,
    impl: float | None,
    last_modified: str,
    mounts: dict[str, Path],
) -> list[ForeignFollowupFinding]:
    """Audit one plan's open followups for refs to foreign plans.

    A followup naming a foreign plan whose status is terminal is work neither
    roadmap can show; the finding names the holder and the target. Live targets
    emit nothing. Unmounted and unresolved targets use distinct codes so they
    are not mistaken for terminal ones.
    """

    from reckon.lifecycle import TERMINAL_STATUSES
    from reckon.resources import read_plan_record

    findings: list[ForeignFollowupFinding] = []
    for followup in followups:
        if not isinstance(followup, Mapping):
            continue
        if str(followup.get("status") or "open") != "open":
            continue
        followup_id = str(followup.get("id") or "")
        for raw_ref in _external_followup_refs(followup):
            parsed = parse_plan_ref(raw_ref)
            if parsed is None or not parsed.is_external(holding_project):
                continue
            target_project = str(parsed.project)
            target_slug = parsed.slug
            docs_dir = mounts.get(target_project)
            if docs_dir is None or not docs_dir.is_dir():
                findings.append(
                    ForeignFollowupFinding(
                        project=holding_project,
                        slug=holding_slug,
                        flag=FOLLOWUP_FOREIGN_UNMOUNTED,
                        age_days=age_days,
                        impl=impl,
                        last_modified=last_modified,
                        target_project=target_project,
                        target_slug=target_slug,
                        target_status="",
                        followup_id=followup_id,
                    )
                )
                continue
            target = read_plan_record(docs_dir, target_project, target_slug)
            target_status = str(target.get("status") or "").strip().lower()
            if not target_status:
                findings.append(
                    ForeignFollowupFinding(
                        project=holding_project,
                        slug=holding_slug,
                        flag=FOLLOWUP_FOREIGN_MISSING,
                        age_days=age_days,
                        impl=impl,
                        last_modified=last_modified,
                        target_project=target_project,
                        target_slug=target_slug,
                        target_status="",
                        followup_id=followup_id,
                    )
                )
                continue
            if target_status in TERMINAL_STATUSES:
                findings.append(
                    ForeignFollowupFinding(
                        project=holding_project,
                        slug=holding_slug,
                        flag=FOLLOWUP_FOREIGN_TERMINAL,
                        age_days=age_days,
                        impl=impl,
                        last_modified=last_modified,
                        target_project=target_project,
                        target_slug=target_slug,
                        target_status=target_status,
                        followup_id=followup_id,
                    )
                )
    return findings


def audit_lifecycle(
    *,
    project: str | None = None,
    docs_dir: Path | None = None,
    now_ts: float | None = None,
) -> list[LifecycleFinding | ForeignFollowupFinding]:
    if docs_dir is not None:
        if project is None:
            raise ValueError("project is required when docs_dir is provided")
        mounts = {project: docs_dir.resolve()}
    else:
        mounts = _load_mounts()
        if project is not None:
            if project not in mounts:
                raise ValueError(f"project {project!r} not found in {_mounts_path()}")
            mounts = {project: mounts[project]}

    current_ts = time.time() if now_ts is None else now_ts
    findings: list[LifecycleFinding | ForeignFollowupFinding] = []
    flag_order = {
        "MISSING_IMPL": 0,
        "SUMMARY_TOO_LONG": 1,
        "STALE": 2,
        "STALE_RCA": 3,
        FOLLOWUP_FOREIGN_TERMINAL: 4,
        FOLLOWUP_FOREIGN_UNMOUNTED: 5,
        FOLLOWUP_FOREIGN_MISSING: 6,
    }

    for project_name, docs_dir in mounts.items():
        if not docs_dir.is_dir():
            continue
        for html_file in _iter_doc_files(docs_dir, project_name):
            state = _read_lifecycle_state(html_file)
            modified_ts = os.path.getmtime(html_file)
            age_days = max(0, int((current_ts - modified_ts) // 86400))
            last_modified = datetime.fromtimestamp(modified_ts).strftime("%Y-%m-%d")
            status = state["status"]
            doc_type = state["type"]
            impl = state["impl"]
            summary_length = plan_summary_length(state["summary"])

            staleness = lifecycle_staleness(
                doc_type=doc_type,
                status=status,
                impl=impl,
                age_days=age_days,
            )

            if doc_type == "research":
                if staleness == "stale":
                    findings.append(
                        LifecycleFinding(
                            project=project_name,
                            slug=state["slug"],
                            flag="STALE_RCA",
                            age_days=age_days,
                            impl=impl,
                            last_modified=last_modified,
                        )
                    )
                continue
            if doc_type == "evidence":
                continue

            if summary_length > PLAN_SUMMARY_MAX_LENGTH:
                findings.append(
                    LifecycleFinding(
                        project=project_name,
                        slug=state["slug"],
                        flag="SUMMARY_TOO_LONG",
                        age_days=age_days,
                        impl=impl,
                        last_modified=last_modified,
                        summary_length=summary_length,
                    )
                )

            if staleness == "stale":
                findings.append(
                    LifecycleFinding(
                        project=project_name,
                        slug=state["slug"],
                        flag="STALE",
                        age_days=age_days,
                        impl=impl,
                        last_modified=last_modified,
                    )
                )
            if status in {"shipped", "done"} and (impl is None or impl == 0.0):
                findings.append(
                    LifecycleFinding(
                        project=project_name,
                        slug=state["slug"],
                        flag="MISSING_IMPL",
                        age_days=age_days,
                        impl=impl,
                        last_modified=last_modified,
                    )
                )
            findings.extend(
                _foreign_followup_findings(
                    followups=state.get("followups") or [],
                    holding_project=project_name,
                    holding_slug=state["slug"],
                    age_days=age_days,
                    impl=impl,
                    last_modified=last_modified,
                    mounts=mounts,
                )
            )

    findings.sort(key=lambda item: (item.project, flag_order[item.flag], item.slug))
    return findings


class _StructureScanner(HTMLParser):
    """Section balance and header count read from the RAW tag stream.

    BeautifulSoup repairs unbalanced markup. A document whose ``<section>``
    never closes parses into a well-formed tree, so every later check reads it
    as valid and the audit says OK — while the authored section tree has every
    following sibling nested inside the unclosed one. The balance question is
    about what was authored, and only the raw tag stream carries that.

    ``headers`` counts only document-level ``<header>`` elements. A ``<header>``
    whose direct parent is a ``<section class="section-landed">`` is the
    collapse-on-landing summary card the authoring skill prescribes, so it is
    not a second shell header and is not counted.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_sections: list[str] = []
        self.stray_closes = 0
        self.headers = 0
        # (tag, class_attr) for every open element, so a header's direct parent
        # is the stack top. Void elements are never pushed.
        self.open_elements: list[tuple[str, str]] = []
        # {tag: [opens, closes]} for the balance-checked tags.
        self.tag_balance: dict[str, list[int]] = {tag: [0, 0] for tag in _BALANCE_TAGS}

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrd = dict(attrs)
        if tag in self.tag_balance:
            self.tag_balance[tag][0] += 1
        if tag == "section":
            self.open_sections.append(attrd.get("id") or "")
        elif tag == "header" and not self._in_landed_section():
            self.headers += 1
        if tag not in _VOID_ELEMENTS:
            self.open_elements.append((tag, attrd.get("class") or ""))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.tag_balance:
            self.tag_balance[tag][1] += 1
        for index in range(len(self.open_elements) - 1, -1, -1):
            if self.open_elements[index][0] == tag:
                del self.open_elements[index:]
                break
        if tag != "section":
            return
        if self.open_sections:
            self.open_sections.pop()
        else:
            self.stray_closes += 1

    def _in_landed_section(self) -> bool:
        """True when the innermost open element is a landed-section wrapper."""
        if not self.open_elements:
            return False
        tag, classes = self.open_elements[-1]
        return tag == "section" and _LANDED_SECTION_CLASS in classes.split()


def _structure_findings(html_text: str) -> list[Finding]:
    """Report structural imbalance the parser would otherwise repair away.

    Severity: an unbalanced section set reports at ``error``. A section that is
    never closed makes every following sibling a child of it, so the section
    tree a reader or a writer derives from the document no longer matches the
    section tree that was authored — a later write aimed at one section can
    land under another, and both reported as if it had succeeded. Because the
    browser and the parser repair the nesting, nothing downstream notices:
    that is a confident wrong answer, and an error is what the exit code is
    for. A repeated document header reports at ``warn``: HTML permits several
    headers, it renders and it does not change section nesting, but the shell
    owns one and a second is the trace of content from another document
    spliced into this one — real, worth surfacing, not worth failing a build
    over on its own. A header that is the direct child of a landed-section
    wrapper is the collapse-on-landing summary card and is not counted, so a
    document may carry one plus one per landed section's own header.
    """
    scanner = _StructureScanner()
    try:
        scanner.feed(html_text or "")
        scanner.close()
    except Exception:  # noqa: BLE001 - never let a balance scan crash the audit
        return []

    out: list[Finding] = []
    for sid in scanner.open_sections:
        label = f' id="{sid}"' if sid else ""
        out.append(
            Finding(
                "error",
                "section-unclosed",
                f"<section{label}> is never closed — every element after it"
                " nests inside it instead of standing beside it",
            )
        )
    if scanner.stray_closes:
        noun = "closing tag" if scanner.stray_closes == 1 else "closing tags"
        out.append(
            Finding(
                "error",
                "section-stray-close",
                f"{scanner.stray_closes} stray </section> {noun} with no"
                " matching <section> open — an authored boundary is misplaced",
            )
        )
    if scanner.headers > 1:
        out.append(
            Finding(
                "warn",
                "header-duplicate",
                f"document carries {scanner.headers} <header> elements; the"
                " document shell owns one, and a second signals content from"
                " another document spliced into this one",
            )
        )
    for tag, (opens, closes) in scanner.tag_balance.items():
        if opens == closes:
            continue
        if opens > closes:
            missing = opens - closes
            out.append(
                Finding(
                    "error",
                    f"{tag}-unclosed",
                    f"{missing} <{tag}> opened and never closed — the browser"
                    " reflows every following element into the last one instead"
                    " of standing them beside it",
                )
            )
            continue
        stray = closes - opens
        out.append(
            Finding(
                "error",
                f"{tag}-stray-close",
                f"{stray} stray </{tag}> with no matching <{tag}> open — an"
                " authored row boundary is misplaced",
            )
        )
    return out


def _scalar_duplicate_findings(soup: BeautifulSoup) -> list[Finding]:
    """Report a duplicated ``plan-*`` scalar, which the reader resolves last-wins.

    ``read_state`` walks every ``<meta>`` in document order and overwrites the
    field each time, so a duplicate is not an ambiguity the reader reports — it
    is one value silently chosen. A union-resolved merge of two landing records
    leaves exactly this shape: two ``plan-version`` lines, the writer updates
    one, and every write is reported as a successful change to a value the
    surface never reads.
    """
    seen: dict[str, list[str]] = {}
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").strip().lower()
        if not name.startswith(_SCALAR_META_PREFIX):
            continue
        seen.setdefault(name, []).append(str(meta.get("content") or ""))
    out: list[Finding] = []
    for name, values in seen.items():
        if len(values) < 2:
            continue
        rendered = ", ".join(f"'{value}'" for value in values)
        out.append(
            Finding(
                "error",
                "duplicate-plan-scalar",
                f'<meta name="{name}"> appears {len(values)} times ({rendered}) —'
                " the reader takes the last one, so a writer updating any other"
                " line reports success while the surface keeps the final value",
            )
        )
    return out


def _resource_island_findings(doc_type: str, soup: BeautifulSoup) -> list[Finding]:
    """Report a typed resource whose state island is absent or unparseable.

    A sprint / milestone / blocker / timeline / review page carries its state in
    the ``reckon-resource-state`` island; the markup around it is presentation.
    With the island absent the document still renders and still passes every
    presentation check, while any plan read in the project fails — the project
    enumerates every sprint document, so one unreadable member fails the whole
    read for every session.
    """
    if doc_type not in _TYPED_RESOURCE_TYPES:
        return []
    # Only a page that renders the resource body carries the state contract: a
    # fragment holding the reckon-* identity metas and no resource shell is
    # metadata, not a resource page, and requiring an island of it would fail a
    # document that presents nothing to read.
    if soup.find(class_="reckon-resource") is None:
        return []
    from reckon.project_state import RESOURCE_SCRIPT_ID

    island = soup.find("script", id=RESOURCE_SCRIPT_ID)
    if island is None:
        return [
            Finding(
                "error",
                "resource-island-missing",
                f"{doc_type} resource with no"
                f' <script id="{RESOURCE_SCRIPT_ID}"> state island — the island'
                " IS the resource's state and the markup around it is"
                " presentation; without it every plan read in the project fails",
            )
        ]
    try:
        data = json.loads(island.string or "")
    except (TypeError, ValueError) as exc:
        return [
            Finding(
                "error",
                "resource-island-malformed",
                f'<script id="{RESOURCE_SCRIPT_ID}"> does not parse as JSON: {exc}',
            )
        ]
    if not isinstance(data, dict):
        return [
            Finding(
                "error",
                "resource-island-malformed",
                f'<script id="{RESOURCE_SCRIPT_ID}"> must hold a JSON object',
            )
        ]
    return []


def _unwired_plan_findings(
    declared_type: str,
    state: Mapping[str, Any],
    soup: BeautifulSoup,
    html_text: str,
    slug: str,
) -> list[Finding]:
    """Flag an implementable plan that declares no wire and no standalone reason."""

    status = str(state.get("status") or "").strip().lower()
    gate_count = len(soup.select('section[data-reckon="gates"] .r-gate[data-id]'))
    finding = unwired_plan_finding(
        doc_type=str(declared_type or "").strip().lower(),
        status=status,
        modified=str(state.get("modified") or ""),
        links=(
            list(state.get("depends_on") or [])
            + list(state.get("blocks") or [])
            + list(state.get("informs") or [])
        ),
        gate_count=gate_count,
        standalone=standalone_reason(html_text),
        slug=slug,
    )
    return [finding] if finding else []


def unwired_plan_finding(
    *,
    doc_type: str,
    status: str,
    modified: str,
    links: list[str],
    gate_count: int,
    standalone: str | None,
    slug: str,
) -> Finding | None:
    """Build the ``unwired-plan`` finding for an unwired implementable plan.

    ``doc_type`` must be the DECLARED type — ``"plan"`` only when the document
    says it is one — so an authored prose fragment is not held to a plan's
    wiring contract. ``None`` when the document is not a plan, is already
    terminal, is wired by a link or a gate, or carries a plan-standalone
    reason. Severity is ``error`` for a plan modified since the rule landed and
    ``warn`` before it, so earlier plans are visible without stopping the fleet.
    """

    if doc_type != "plan" or status in TERMINAL_STATUSES:
        return None
    if links or gate_count or (standalone or "").strip():
        return None
    return Finding(
        unwired_plan_severity(modified),
        "unwired-plan",
        unwired_plan_message(slug=slug),
    )


def _implementable_section_ids(
    state: Mapping[str, Any], heading_ids: list[str]
) -> list[str]:
    """Sections the plan states are still work, in document order.

    The declarations map is the plan's own statement of what is implementable;
    a plan that declares nothing at all states nothing, so its numbered
    headings stand in for it. A section id declared implementable but carrying
    no heading is a different defect — this check reports records, and would
    otherwise report a section the reader cannot open.
    """

    declarations = state.get("section_declarations")
    if isinstance(declarations, Mapping) and declarations:
        return [
            sid
            for sid in heading_ids
            if str(declarations.get(sid) or "").strip() == "implementable"
        ]
    return [sid for sid in heading_ids if _NUMBERED_SECTION_ID.fullmatch(sid)]


def _section_contract_findings(
    doc_type: str,
    state: Mapping[str, Any],
    soup: BeautifulSoup,
) -> list[Finding]:
    """Report an implementable section that carries no typed section record.

    Severity is the plan's own state: a plan that already carries at least one
    typed record is held to the contract at ``error``, and a plan that carries
    none predates it and is reported at ``warn``, so converting the existing
    plans does not stop the fleet. The message carries the same worked append
    example the write boundary refuses with, because the lane that authors the
    record needs a shape it can copy rather than a rule it must translate.
    """

    if doc_type != "plan":
        return []
    heading_ids = [str(h.get("id") or "") for h in soup.find_all("h2", id=True)]
    recorded = {
        str(record.get("id") or "")
        for record in state.get("sections") or []
        if isinstance(record, Mapping)
    }
    severity = "error" if recorded else "warn"
    return [
        Finding(
            severity,
            _SECTION_WITHOUT_CONTRACT,
            _section_contract_refusal(
                f"section {sid!r} is implementable but carries no typed section "
                "record (effort_hours, capability, links)"
            ),
        )
        for sid in _implementable_section_ids(state, heading_ids)
        if sid not in recorded
    ]


def _followup_section_findings(
    doc_type: str, state: Mapping[str, Any]
) -> list[Finding]:
    """Report an open followup whose body states section-sized work.

    A followup states no effort, so the work it describes is sized by the
    deliverables it enumerates; at ``FOLLOWUP_WORK_ITEMS_THRESHOLD`` or more
    the body is describing work the plan's own unit should carry, where the
    computed figure and the dispatch route can both see it.
    """

    if doc_type != "plan":
        return []
    out: list[Finding] = []
    for followup in state.get("followups") or []:
        if not isinstance(followup, Mapping):
            continue
        if str(followup.get("status") or "open").strip() != "open":
            continue
        body_soup = BeautifulSoup(str(followup.get("body") or ""), "html.parser")
        items = len(_FOLLOWUP_WORK_ITEM.findall(_visible_text(body_soup)))
        if items < FOLLOWUP_WORK_ITEMS_THRESHOLD:
            continue
        followup_id = str(followup.get("id") or "") or "<no-id>"
        out.append(
            Finding(
                "warn",
                _FOLLOWUP_DESCRIBES_SECTION_WORK,
                f"followup {followup_id!r} states {items} deliverables — one is a "
                "followup-sized remainder, more than one is section-sized work; "
                "author it as a section so its effort, capability and route are "
                "visible (edit_plan mode=state, append target 'sections')",
            )
        )
    return out


def audit_html(html_text: str, *, project: str | None = None) -> list[Finding]:
    """Audit one document's HTML, returning findings (worst-first ordering)."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    out: list[Finding] = []

    # (g) Structural balance — read from the raw tags, since the parser
    # repairs an unclosed section into a well-formed tree (see _StructureScanner).
    out.extend(_structure_findings(html_text))

    # (g) Scalar duplicates — the authoritative state, not the prose around it.
    out.extend(_scalar_duplicate_findings(soup))

    # Document type — research/doc are non-actionable; plan requires status.
    rt = soup.find("meta", attrs={"name": "reckon-type"})
    doc_type = ((rt.get("content") if rt else "") or "plan").strip().lower()

    # (g) Typed-resource state island — again the state, not the presentation.
    out.extend(_resource_island_findings(doc_type, soup))

    # (e) Required meta tags ------------------------------------------------
    present = {
        (m.get("name") or "").lower() for m in soup.find_all("meta") if m.get("name")
    }
    required = (
        _TYPED_RESOURCE_META if doc_type in _TYPED_RESOURCE_TYPES else _REQUIRED_META
    )
    for req in required:
        if req == "plan-status" and doc_type in {"research", "evidence", "doc"}:
            continue
        if req not in present:
            out.append(
                Finding(
                    "error", "meta-missing", f'missing required <meta name="{req}">'
                )
            )

    # A typed section record that does not parse raises out of the state read;
    # letting it through ends the audit at the first malformed record and takes
    # every later check with it, including the section finding written to catch
    # exactly that. Report the record and audit the rest with the state that
    # could be read.
    state: dict[str, Any] = {}
    try:
        state = _plan_html.read_state(html_text)
    except ValueError as exc:
        out.append(
            Finding(
                "error",
                _SECTION_RECORD_INVALID,
                "typed section record does not parse: " + " ".join(str(exc).split()),
            )
        )
    summary = str(state.get("summary") or "")
    summary_length = plan_summary_length(summary)
    if doc_type == "plan" and summary_length > PLAN_SUMMARY_MAX_LENGTH:
        out.append(
            Finding(
                "warn",
                "summary-too-long",
                f"plan summary is {summary_length} characters; "
                f"the maximum is {PLAN_SUMMARY_MAX_LENGTH}",
            )
        )

    # Landed/evidence records must carry the plan -> evidence back-link;
    # without it the graph shows research->plan (informs) but never which
    # evidence a plan produced.
    slug_meta = soup.find("meta", attrs={"name": "plan-slug"})
    slug = ((slug_meta.get("content") if slug_meta else "") or "").strip()
    evidence_meta = soup.find("meta", attrs={"name": "plan-evidence-for"})
    evidence_for = (
        (evidence_meta.get("content") if evidence_meta else "") or ""
    ).strip()
    if (doc_type == "evidence" or slug.endswith("-landed")) and not evidence_for:
        out.append(
            Finding(
                "warn",
                "evidence-for-missing",
                "landed/evidence record without plan-evidence-for — the "
                "plan → generated-evidence link is missing; name the plan(s) "
                "whose execution this record documents",
            )
        )

    # Wiring — an implementable plan must declare what it waits on or feeds,
    # or say in words that it stands alone. The roadmap can only follow wires
    # that were drawn; an unwired plan is invisible on the critical path. The
    # gate is the DECLARED type, so a prose fragment that merely carries a
    # slug is not held to a plan's wiring contract.
    out.extend(
        _unwired_plan_findings(
            (rt.get("content") if rt else "") or "", state, soup, html_text, slug
        )
    )

    # Section contract — the unit that carries effort, capability and a route.
    declared_type = ((rt.get("content") if rt else "") or "").strip().lower()
    out.extend(_section_contract_findings(declared_type, state, soup))
    out.extend(_followup_section_findings(declared_type, state))

    # Project for image-path checks — meta, then fallback arg.
    dp = soup.find("meta", attrs={"name": "docs-project"})
    proj = ((dp.get("content") if dp else "") or project or "").strip()

    # (a) Image src that won't resolve --------------------------------------
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            out.append(Finding("warn", "img-no-src", "<img> with empty src"))
            continue
        if re.match(r"^(?:https?:)?//|^data:", src):
            continue  # absolute / data URI — fine
        if src.startswith("/"):
            # Project-absolute. If we know the project, require the prefix.
            if proj and not src.startswith(f"/{proj}/"):
                out.append(
                    Finding(
                        "warn",
                        "img-wrong-project",
                        f'<img src="{src}"> not under /{proj}/ — verify it resolves',
                    )
                )
            continue
        # Relative (e.g. figures/foo.svg) — 404s under the no-slash plan URL.
        fix = f"/{proj}/{src}" if proj else f"/<project>/{src}"
        out.append(
            Finding(
                "error",
                "img-relative-src",
                f'relative <img src="{src}"> will 404 in the SPA — use src="{fix}"',
            )
        )

    # (b) Literal markdown in rendered text ---------------------------------
    body_scopes: list[tuple[str, object]] = []
    for cls in _BODY_CLASSES:
        for el in soup.select(f".{cls}"):
            body_scopes.append((cls, el))
    # Also prose paragraphs in the authored body (outside reckon-owned widgets).
    for p in soup.find_all("p"):
        classes = p.get("class") or []
        if any(c.startswith("r-") or c in ("dec-choice",) for c in classes):
            continue
        body_scopes.append(("p", p))

    for cls, el in body_scopes:
        txt = _visible_text(el)
        prose = _prose_text(el)
        if _MD_BOLD.search(prose):
            out.append(
                Finding(
                    "error",
                    "md-bold",
                    f"literal markdown **bold** in <{cls}> — author <strong>…</strong>"
                    f" (renders verbatim): '{_MD_BOLD.search(prose).group()[:40]}…'",
                )
            )
        # Leading list/heading markers only meaningful on the raw inner text.
        raw = el.decode_contents() if hasattr(el, "decode_contents") else txt
        if _MD_LEADING.search(raw):
            out.append(
                Finding(
                    "warn",
                    "md-list-or-heading",
                    f"leading markdown marker (- / # ) in <{cls}> — use <ul>/<li> or <h3>",
                )
            )

    # (c) Reliance on <head><style> -----------------------------------------
    head = soup.find("head")
    if head and head.find("style"):
        out.append(
            Finding(
                "warn",
                "head-style-dropped",
                "<head><style> is dropped by the SPA — doc-local CSS will not apply;"
                " move rules into reckon styles or use inline style= sparingly",
            )
        )

    # (d) <pre> long lines (informational — CSS now wraps these) -------------
    for pre in soup.find_all("pre"):
        longest = max((len(ln) for ln in pre.get_text().splitlines()), default=0)
        if longest > _PRE_LINE_LIMIT:
            out.append(
                Finding(
                    "info",
                    "pre-long-line",
                    f"<pre> has a {longest}-char line (> {_PRE_LINE_LIMIT}); wraps via"
                    " CSS but consider shorter lines for readability",
                )
            )

    # (f) Stub prose / empty reckon sections --------------------------------
    full_text = soup.get_text(" ", strip=True)
    m = _STUB_PROSE.search(full_text)
    if m:
        out.append(
            Finding(
                "warn",
                "stub-prose",
                f"stub/placeholder prose detected: '{m.group().strip()[:40]}'",
            )
        )
    for sec in soup.select("section[data-reckon]"):
        sid = sec.get("data-reckon", "?")
        # A section's typed record is an empty element by construction: it
        # carries the section's effort, capability and status as attributes, and
        # holds no items, so it is not an empty widget.
        if sid == "section":
            continue
        # A reckon section with a heading but no item rows is an empty stub.
        has_items = bool(sec.select(".r-dec, .r-fu, .r-q, .r-research, .r-comment"))
        if not has_items and not _visible_text(sec).strip().strip(
            "§ Decisions Followups Open questions Research Comments"
        ):
            out.append(
                Finding(
                    "info",
                    "empty-section",
                    f'<section data-reckon="{sid}"> has no items',
                )
            )

    # Worst-first: error, warn, info
    out.sort(key=lambda f: SEVERITIES.index(f.severity))
    return out


def audit_file(path: Path, *, project: str | None = None) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [Finding("error", "io", f"cannot read {path}: {e}")]
    return audit_html(text, project=project)


# ── Dangling internal-link check (corpus-aware) ────────────────────────────
#
# Internal links that reference a plan slug or in-page anchor id that does not
# exist produce a 404 in the SPA.  This is a corpus-level check: we first scan
# all HTML files in a docs directory to build a slug→file and file→id map, then
# check each doc's links against it.
#
# Link forms recognised (per grep of the existing docs corpus):
#   /<project>/<slug>.html       → must resolve to a known slug
#   /<project>/<slug>            → same (no extension)
#   archive/<slug>.html          → relative to docs/; archive/ is a valid target
#   <slug>.html                  → relative to the same directory as the source
#   #anchor                      → must match an id attribute in the same file
#   <slug>.html#anchor           → slug must resolve; anchor must be in that file
#
# Meta slug references (plan-depends-on, plan-blocks, plan-informs) are
# comma-separated slug lists that are also checked for resolution.
#
# Skipped: http(s):// · // · mailto: · data: · /_shared/ · /_ui/ · bare
# non-slug hrefs (like index.html which is the SPA shell).

_SKIP_HREF_PREFIXES = (
    "http:",
    "https:",
    "//",
    "mailto:",
    "data:",
    "/_shared/",
    "/_ui/",
)
# Infrastructure filenames the SPA generates (not real plan slugs).
_INFRA_FILENAMES = frozenset(
    [
        "index.html",
        "sprint.html",
        "sprints.html",
        "milestones.html",
        "decisions.html",
        "inventory.html",
        "blockers.html",
        "implementation.html",
        "questions.html",
        "home.html",
        "project.html",
        "plan.html",
        "README.html",
    ]
)
# Meta fields that hold comma-separated plan-slug references.
_SLUG_META_FIELDS = (
    "plan-depends-on",
    "plan-blocks",
    "plan-informs",
    "plan-evidence-for",
    "plan-verifies",
    "plan-supersedes",
)


def _local_ref_slug(ref: str, project: str | None) -> str | None:
    """Resolve an opaque provenance ref to a local slug for corpus checks.

    Accepted forms are ``slug``, ``slug#stage``, ``project:slug`` and
    ``project:slug#stage``. Cross-project refs are valid but cannot be checked
    against one project's corpus, so they return ``None``.
    """
    base = ref.split("#", 1)[0].strip()
    if ":" not in base:
        return base
    ref_project, slug = base.split(":", 1)
    return slug if project and ref_project == project else None


def _collect_corpus(
    docs_dir: Path, project: str
) -> tuple[dict[str, Path], dict[Path, set[str]]]:
    """Scan a docs directory and return:

    - slug_to_file: {slug → Path} for every HTML doc (including archive/ targets).
    - file_to_ids: {Path → set(id)} collecting element id attributes per file.

    Archive files are valid link targets even though they are excluded from the
    live inventory; they are included here so links into docs/archive/ resolve.
    """
    slug_to_file: dict[str, Path] = {}
    file_to_ids: dict[Path, set[str]] = {}

    from reckon.resources import INFRA_DIRS, NON_RESOURCE_FILES

    del project
    for html_file in sorted(docs_dir.rglob("*.html")):
        relative = html_file.relative_to(docs_dir)
        if html_file.name in NON_RESOURCE_FILES or any(
            part in INFRA_DIRS for part in relative.parts[:-1]
        ):
            continue
        try:
            text = html_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        soup = BeautifulSoup(text, "html.parser")

        slug_meta = soup.find("meta", attrs={"name": "plan-slug"})
        slug = ((slug_meta.get("content") if slug_meta else "") or "").strip()
        slug = slug or html_file.stem
        type_meta = soup.find("meta", attrs={"name": "reckon-type"})
        resource_type = (
            ((type_meta.get("content") if type_meta else "") or "plan").strip().lower()
        )
        archived = "archive" in relative.parts[:-1]

        # Untyped compatibility links retain the historical plan preference.
        if (resource_type == "plan" and not archived) or slug not in slug_to_file:
            slug_to_file[slug] = html_file
        # Also index by stem so relative <slug>.html links resolve.
        stem = html_file.stem
        if stem not in slug_to_file:
            slug_to_file[stem] = html_file

        # Collect all id= attributes in the document.
        ids: set[str] = set()
        for el in soup.find_all(id=True):
            eid = (el.get("id") or "").strip()
            if eid:
                ids.add(eid)
        file_to_ids[html_file] = ids

    return slug_to_file, file_to_ids


def _resolve_href(
    href: str,
    source_file: Path,
    docs_dir: Path,
    project: str | None,
    slug_to_file: dict[str, Path],
) -> tuple[Path | None, str | None]:
    """Parse an href and return (target_path, anchor) — target_path is None if unresolvable.

    Skips external hrefs; returns (None, None) for hrefs that should be ignored.
    Returns (False, None) to signal "link recognised but target file not found".
    """
    if not href or any(href.startswith(p) for p in _SKIP_HREF_PREFIXES):
        return None, None

    parsed = urlsplit(href)
    file_part = parsed.path
    anchor = parsed.fragment or None

    # Bare anchor (same-file link: "#id").
    if not file_part:
        return source_file, anchor

    # Strip leading / for project-absolute links: /<project>/<slug>.html
    if file_part.startswith("/") and project:
        prefix = f"/{project}/"
        if file_part.startswith(prefix):
            project_route = file_part[len(prefix) :]
            route_path = docs_dir / project_route
            candidates = [route_path]
            if not route_path.suffix:
                candidates.append(route_path.with_suffix(".html"))
            for candidate in candidates:
                resolved_candidate = candidate.resolve()
                try:
                    resolved_candidate.relative_to(docs_dir.resolve())
                except ValueError:
                    return False, anchor  # type: ignore[return-value]
                if resolved_candidate.is_file():
                    return resolved_candidate, anchor
            file_part = project_route
        elif file_part.startswith("/"):
            # /<other-project>/... — can't validate cross-project from here.
            return None, None

    # Normalise: strip .html suffix to get slug/relative-path stem.
    stem = file_part.removesuffix(".html")

    # Infra files are always valid (they are served by the SPA engine).
    base_name = Path(file_part).name
    if base_name in _INFRA_FILENAMES:
        return None, None

    # Try relative resolution from source directory.
    candidate = (source_file.parent / file_part).resolve()
    try:
        candidate.relative_to(docs_dir.resolve())
    except ValueError:
        return False, anchor  # type: ignore[return-value]
    if candidate.is_file():
        return candidate, anchor

    # Try legacy untyped lookup only after exact relative/typed resolution.
    resolved = slug_to_file.get(stem) or slug_to_file.get(Path(stem).name)
    if resolved is not None:
        return resolved, anchor

    # Couldn't resolve.
    return False, anchor  # type: ignore[return-value]


def audit_links(
    paths: list[Path],
    docs_dir: Path,
    *,
    project: str | None = None,
) -> dict[Path, list[Finding]]:
    """Corpus-aware dangling internal-link check.

    Scans all HTML files in ``docs_dir`` to build a slug/id corpus, then checks
    each path in ``paths`` for links that reference slugs or anchors that do not
    exist.

    Returns {path → [Finding, …]} — only paths with findings are included.
    """
    corpus_project = project
    if not corpus_project:
        for candidate in paths:
            try:
                soup = BeautifulSoup(candidate.read_text(), "html.parser")
            except OSError:
                continue
            meta = soup.find("meta", attrs={"name": "docs-project"})
            corpus_project = ((meta.get("content") if meta else "") or "").strip()
            if corpus_project:
                break
    slug_to_file, file_to_ids = _collect_corpus(docs_dir, corpus_project or "doccheck")

    results: dict[Path, list[Finding]] = {}
    for path in paths:
        findings: list[Finding] = []

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            findings.append(Finding("error", "io", f"cannot read {path}: {e}"))
            results[path] = findings
            continue

        soup = BeautifulSoup(text, "html.parser")

        # Infer project from meta if not supplied.
        dp = soup.find("meta", attrs={"name": "docs-project"})
        proj = ((dp.get("content") if dp else "") or project or "").strip() or None

        # (g) Check <a href> internal links.
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            target, anchor = _resolve_href(href, path, docs_dir, proj, slug_to_file)
            if target is None:
                continue  # external or infra — skip
            if target is False:
                findings.append(
                    Finding(
                        "warn",
                        "dangling-link",
                        f'<a href="{href}"> — target file not found',
                    )
                )
                continue
            # Check anchor resolution in target file.
            if anchor:
                known_ids = file_to_ids.get(target, set())
                if anchor not in known_ids:
                    findings.append(
                        Finding(
                            "warn",
                            "dangling-anchor",
                            f'<a href="{href}"> — anchor #{anchor} not found in target',
                        )
                    )

        # (g) Check plan-depends-on / plan-blocks / plan-informs slug references.
        for meta_name in _SLUG_META_FIELDS:
            m = soup.find("meta", attrs={"name": meta_name})
            if not m:
                continue
            raw = (m.get("content") or "").strip()
            if not raw:
                continue
            for raw_ref in [s.strip() for s in raw.split(",") if s.strip()]:
                slug_ref = _local_ref_slug(raw_ref, proj)
                if slug_ref is not None and slug_ref not in slug_to_file:
                    findings.append(
                        Finding(
                            "warn",
                            "dangling-slug-ref",
                            f'<meta name="{meta_name}"> references unknown slug "{raw_ref}"',
                        )
                    )

        if findings:
            results[path] = findings

    review_path = docs_dir / "state" / (corpus_project or project or "") / "review.html"
    if review_path.is_file():
        review_findings = _audit_review_resource(
            review_path,
            docs_dir,
            corpus_project or project or "",
        )
        if review_findings:
            results[review_path] = review_findings

    return results


def _audit_review_resource(
    path: Path,
    docs_dir: Path,
    project: str,
) -> list[Finding]:
    """Report review enum, ordering, and subject-reference conformance."""

    from reckon.project_state import (
        _PRIORITY_REASONS,
        _REVIEW_ACTIONS,
        _REVIEW_CATEGORIES,
        _REVIEW_SEVERITIES,
        _REVIEW_SUBJECT_KINDS,
        _REVIEW_VALIDATIONS,
        RESOURCE_SCRIPT_ID,
    )
    from reckon.resources import resource_map

    try:
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        island = soup.find("script", id=RESOURCE_SCRIPT_ID)
        data = json.loads(island.string or "") if island else None
    except (OSError, json.JSONDecodeError) as exc:
        return [
            Finding(
                "error",
                "review-state-malformed",
                f"cannot parse review state: {exc}",
            )
        ]
    if not isinstance(data, dict):
        return [
            Finding("error", "review-state-malformed", "review state must be an object")
        ]

    findings: list[Finding] = []
    enum_fields = (
        ("category", _REVIEW_CATEGORIES),
        ("severity", _REVIEW_SEVERITIES),
        ("validated", _REVIEW_VALIDATIONS),
    )
    resources = resource_map(
        docs_dir,
        project,
        include_archived=False,
        ignore_invalid=True,
    )
    live_plans = {
        resource.slug for resource in resources.values() if resource.type == "plan"
    }
    for index, row in enumerate(data.get("findings") or []):
        if not isinstance(row, dict):
            continue
        for field, allowed in enum_fields:
            if row.get(field) not in allowed:
                findings.append(
                    Finding(
                        "warn",
                        "review-enum-violation",
                        f"findings[{index}].{field} must be one of {', '.join(sorted(allowed))}",
                    )
                )
        subject = row.get("subject") if isinstance(row.get("subject"), dict) else {}
        if subject.get("kind") not in _REVIEW_SUBJECT_KINDS:
            findings.append(
                Finding(
                    "warn",
                    "review-enum-violation",
                    f"findings[{index}].subject.kind is outside the review vocabulary",
                )
            )
        action = (
            row.get("recommended_action")
            if isinstance(row.get("recommended_action"), dict)
            else {}
        )
        if action.get("verb") not in _REVIEW_ACTIONS:
            findings.append(
                Finding(
                    "warn",
                    "review-enum-violation",
                    f"findings[{index}].recommended_action.verb is outside the review vocabulary",
                )
            )
        if subject.get("kind") == "plan":
            raw_ref = str(subject.get("id") or "")
            local_slug = _local_ref_slug(raw_ref, project)
            if local_slug is not None and local_slug not in live_plans:
                findings.append(
                    Finding(
                        "warn",
                        "dangling-review-subject",
                        f"findings[{index}].subject references unknown plan {raw_ref!r}",
                    )
                )

    priority = data.get("priority") or []
    ranks = [row.get("rank") for row in priority if isinstance(row, dict)]
    if ranks != list(range(1, len(ranks) + 1)):
        findings.append(
            Finding(
                "warn",
                "review-priority-noncontiguous",
                "review priority ranks must be contiguous from 1",
            )
        )
    for index, row in enumerate(priority):
        if not isinstance(row, dict):
            continue
        for reason in row.get("reasons") or []:
            if reason not in _PRIORITY_REASONS:
                findings.append(
                    Finding(
                        "warn",
                        "review-enum-violation",
                        f"priority[{index}].reasons contains {reason!r} outside the review vocabulary",
                    )
                )
    findings.sort(key=lambda item: SEVERITIES.index(item.severity))
    return findings


def _doc_relative(path: Path, docs_dir: Path) -> str:
    """Return ``path`` relative to the docs root, or its own text if outside it."""
    try:
        return path.resolve().relative_to(docs_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def slug_collision_findings(
    path: Path, *, docs_dir: Path, project: str
) -> list[Finding]:
    """Report a slug a different resource type in the same project carries.

    ``resolve_resource`` resolves one slug per project, so when a plan and a
    resource of another type share a slug it raises "ambiguous across types"
    and every slug-only ``edit_plan`` on the plan fails — while a per-file
    audit of either document passes. The project's resource map already indexes
    every resource by type and slug, so the collision is read from that map
    rather than from a second tree scan. The finding names both documents, and
    either one reports it when audited, so no side depends on being named.
    """
    from reckon.resources import canonical_type, resource_map

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    soup = BeautifulSoup(text, "html.parser")
    slug_meta = soup.find("meta", attrs={"name": "plan-slug"})
    slug = ((slug_meta.get("content") if slug_meta else "") or "").strip()
    if not slug:
        return []
    type_meta = soup.find("meta", attrs={"name": "reckon-type"})
    own_type = canonical_type((type_meta.get("content") if type_meta else "") or "plan")
    own_relative = _doc_relative(path, docs_dir)
    others = [
        resource
        for resource in resource_map(
            docs_dir, project, include_archived=False, ignore_invalid=True
        ).values()
        if resource.slug == slug
        and resource.type != own_type
        and _doc_relative(resource.path, docs_dir) != own_relative
    ]
    if not others:
        return []
    other = min(others, key=lambda item: (item.type, str(item.relative_path)))
    return [
        Finding(
            "error",
            "slug-collision",
            f"{own_relative} carries plan-slug {slug!r}, also carried by"
            f" {other.type} {other.relative_path} — a slug-only lookup cannot"
            " tell them apart; give one a distinct slug",
        )
    ]


def run(
    paths: list[str], *, project: str | None = None, check_links: bool = False
) -> int:
    """Audit each path; print findings; return process exit code (0 = no errors)."""
    path_objs = [Path(raw).expanduser() for raw in paths]

    # Cross-type slug collisions, read from the project's resource map. Only a
    # project-aware run has a docs root to resolve the map from, so a bare
    # per-file audit stays exactly as it was.
    collision_findings: dict[Path, list[Finding]] = {}
    if project is not None:
        docs_dir = _load_mounts().get(project)
        if docs_dir is not None and docs_dir.is_dir():
            for p in path_objs:
                found = slug_collision_findings(p, docs_dir=docs_dir, project=project)
                if found:
                    collision_findings[p] = found

    # Build corpus for link check if requested.
    link_findings: dict[Path, list[Finding]] = {}
    if check_links:
        # Infer the corpus root from the common path so nested resources remain
        # inside the same read-only audit rather than collapsing to the cwd.
        common_path = Path(os.path.commonpath([str(path) for path in path_objs]))
        docs_dir = common_path.parent if common_path.is_file() else common_path
        link_findings = audit_links(path_objs, docs_dir, project=project)

    any_error = False
    for p in path_objs:
        findings = audit_file(p, project=project)
        # Merge in link and cross-type slug-collision findings for this path.
        findings = findings + link_findings.get(p, []) + collision_findings.get(p, [])
        # Re-sort worst-first.
        findings.sort(key=lambda f: SEVERITIES.index(f.severity))

        errors = [f for f in findings if f.severity == "error"]
        warns = [f for f in findings if f.severity == "warn"]
        infos = [f for f in findings if f.severity == "info"]
        if not findings:
            print(f"{p}: OK")
            continue
        status = "FAIL" if errors else "warn"
        print(
            f"{p}: {status} ({len(errors)} error, {len(warns)} warn, {len(infos)} info)"
        )
        for f in findings:
            print(f.fmt())
        if errors:
            any_error = True
    return 1 if any_error else 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="reckon-audit-doc",
        description="Validate authored plan/doc HTML against the SPA render contract.",
        epilog="example:  python -m reckon.doccheck docs/my-plan.html",
    )
    ap.add_argument("paths", nargs="+", help="HTML file(s) to audit")
    ap.add_argument(
        "--project",
        default=None,
        help="project key for image-path checks (default: <meta name=docs-project>)",
    )
    ap.add_argument(
        "--check-links",
        action="store_true",
        default=False,
        help="also check internal links for dangling targets (corpus-aware; requires"
        " all docs to be in the same directory)",
    )
    ns = ap.parse_args(argv)
    return run(ns.paths, project=ns.project, check_links=ns.check_links)


if __name__ == "__main__":
    sys.exit(main())
