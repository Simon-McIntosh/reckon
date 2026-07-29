#!/usr/bin/env python3
"""HTML plan state engine — the plan's data IS semantic HTML.

A plan page carries its data as ordinary HTML the reader can see and the
reckon server reads and writes directly. There is NO embedded JSON blob.

  - Scalars and the versioned capability request live in
    ``<meta name="plan-*">`` elements.
  - Decisions are <div class="r-dec" data-key=…> elements inside
    <section data-reckon="decisions">, with visible <button class="r-opt"
    data-value=…> options, a data-choice attribute (the locked answer, an
    option value OR free text), and a free-form .r-dec-rat rationale.
  - Followups / questions / comments are matching <section data-reckon=…>
    blocks of semantic elements.

`read_state` parses this into the canonical dict; `write_state` regenerates
the reckon-owned sections + meta from the dict, leaving authored prose
untouched. Reads degrade gracefully: a bare page with no plan markup still
yields a valid record (slug from filename, title from <title>, status=draft).
"""

from __future__ import annotations

import html as _htmlmod
import re
from pathlib import Path

from bs4 import BeautifulSoup

from reckon.capability import (
    CAPABILITY_SCHEMA_VERSION,
    from_legacy_tier,
)

# ── Scalar fields carried in <meta name="plan-*"> ──────────────────────────
_SCALARS = (
    "slug", "title", "summary", "status", "roi", "effort", "milestone",
    "sprint", "tier", "owner", "modified",
    # Lifecycle visibility — set via UI status menu.
    # archived: "1" hides the plan from default inventory views (separate from status).
    # read:     "1" marks a research/doc as reviewed (de-emphasises in lists).
    "archived", "read", "reviewed_at", "recorded_at", "verdict", "environment",
    "source", "source_quality",
)
_LIST_SCALARS = (
    "depends_on",
    "blocks",
    "informs",
    "evidence_for",
    "verifies",
    "supersedes",
    "commits",
    "artifacts",
)  # comma-separated in meta

_PLAN_ONLY_METAS = (
    "plan-status",
    "plan-roi",
    "plan-effort",
    "plan-milestone",
    "plan-sprint",
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
    "plan-impl",
)

_DEFAULTS = {
    "status": "draft", "roi": "mid", "effort": "M", "milestone": "—",
    "sprint": None, "summary": "", "modified": "",
    "owner": "", "impl": 0.0, "version": 0,
}

# reckon-owned section ids — regenerated on write, stripped by the SPA before
# rendering the authored prose (it renders interactive widgets instead).
SECTION_IDS = ("decisions", "followups", "questions", "research", "comments")

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

def read_state(html_text: str) -> dict:
    """Parse a plan's semantic HTML into the canonical state dict."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    st: dict = {}
    meta_values: dict[str, str] = {}

    # Scalars from <meta name="plan-*">
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").lower()
        meta_values[name] = m.get("content", "")
        if not name.startswith("plan-"):
            continue
        field = name[len("plan-"):].replace("-", "_")
        content = m.get("content", "")
        if field in _SCALARS:
            st[field] = content
        elif field in _LIST_SCALARS:
            st[field] = [x.strip() for x in content.split(",") if x.strip()]
        elif field in ("impl", "version"):
            try:
                st[field] = float(content) if field == "impl" else int(content)
            except (TypeError, ValueError):
                pass

    warnings: list[str] = []
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

    # Decisions
    decisions: dict[str, dict] = {}
    for dec in soup.select('section[data-reckon="decisions"] .r-dec[data-key]'):
        key = dec.get("data-key", "").strip()
        if not key:
            continue
        opts = [{"value": b.get("data-value", _txt(b)), "label": _txt(b)}
                for b in dec.select(".r-opt")]
        decisions[key] = {
            "title": _txt(dec.select_one(".r-dec-q")),
            "context": _txt(dec.select_one(".r-dec-ctx")),
            "choices": [o["value"] for o in opts],
            "option_labels": {o["value"]: o["label"] for o in opts},
            "choice": dec.get("data-choice", "") or "",
            "rationale": _txt(dec.select_one(".r-dec-rat")),
            "when": dec.get("data-when", "") or "",
            "by": dec.get("data-by", "") or "",
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
            "prompt": (fu.select_one(".r-fu-prompt").get_text() if fu.select_one(".r-fu-prompt") else ""),
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
                warnings.append(
                    f"followup {f['id'] or '<no-id>'}: {diagnostic}"
                )
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
        questions.append({
            "id": q.get("data-id", ""),
            "section": q.get("data-section", ""),
            "opened_by": q.get("data-opened-by", ""),
            "opened_at": q.get("data-opened-at", ""),
            "body": _inner_html(q.select_one(".r-q-body")) or _txt(q),
            "resolution": _inner_html(q.select_one(".r-q-resolution")) or None,
            "resolved_at": q.get("data-resolved-at", "") or None,
            "resolved_by": q.get("data-resolved-by", "") or None,
        })
    st["questions"] = questions

    # Research
    research = []
    for r in soup.select('section[data-reckon="research"] .r-research'):
        research.append({
            "id": r.get("data-id", ""),
            "type": r.get("data-type", ""),
            "title": _txt(r.select_one(".r-research-title")) or _txt(r),
            "source": r.get("data-source", ""),
            "added_by": r.get("data-added-by", ""),
            "when": r.get("data-when", ""),
            "url": r.get("data-url", "") or None,
        })
    st["research"] = research

    # Comments (section-anchored)
    comments: dict[str, list] = {}
    for c in soup.select('section[data-reckon="comments"] .r-comment'):
        sid = c.get("data-section", "_top")
        comments.setdefault(sid, []).append({
            "id": c.get("data-id", ""),
            "who": c.get("data-who", ""),
            "when": c.get("data-when", ""),
            "quote": c.get("data-quote", "") or None,
            "body": _inner_html(c.select_one(".r-comment-body")) or _txt(c),
        })
    st["comments"] = comments
    if warnings:
        st["compatibility_warnings"] = warnings
    return st


# ── Render ───────────────────────────────────────────────────────────────--

def _render_decisions(decisions: dict) -> str:
    if not decisions or not isinstance(decisions, dict):
        return ""
    rows = []
    for key, d in decisions.items():
        d = d or {}
        labels = d.get("option_labels") or {}
        opts = ""
        for v in (d.get("choices") or []):
            label = labels.get(v, v)
            chosen = " chosen" if d.get("choice") == v else ""
            opts += f'<button class="r-opt{chosen}" data-value="{_esc(v)}">{_esc(label)}</button>\n      '
        opts_block = f'<p class="r-dec-opts">\n      {opts}</p>\n    ' if opts else ""
        ctx = f'<p class="r-dec-ctx">{_esc(d.get("context"))}</p>\n    ' if d.get("context") else ""
        rat = f'<p class="r-dec-rat">{_esc(d.get("rationale"))}</p>\n    ' if d.get("rationale") else '<p class="r-dec-rat"></p>\n    '
        rows.append(
            f'<div class="r-dec" data-key="{_esc(key)}" data-choice="{_esc(d.get("choice"))}"'
            f' data-by="{_esc(d.get("by"))}" data-when="{_esc(d.get("when"))}">\n    '
            f'<p class="r-dec-q">{_esc(d.get("title") or key)}</p>\n    '
            f'{ctx}{opts_block}{rat}</div>'
        )
    return ('<section data-reckon="decisions" id="decisions" class="r-decisions">\n'
            '<h2><span class="sec">§</span> Decisions</h2>\n'
            + "\n".join(rows) + "\n</section>")


def _render_followups(followups: list) -> str:
    if not followups or not isinstance(followups, list):
        return ""
    arts = []
    for f in followups:
        f = f or {}
        # prompt is plain text → escaped; body / outcome are authored HTML → raw.
        prompt = f'<pre class="r-fu-prompt">{_esc(f.get("prompt"))}</pre>\n    ' if f.get("prompt") else ""
        outcome = f'<p class="r-fu-outcome">{_body(f.get("outcome"))}</p>\n    ' if f.get("outcome") else ""
        # Derive status from resolved_at (mirrors _render_questions). A followup
        # with a resolved_at is resolved regardless of a stale literal status —
        # resolve_followup sets resolved_at/by/outcome but not the status field.
        status = "resolved" if f.get("resolved_at") else (f.get("status") or "open")
        legacy_tier = (
            f' data-tier="{_esc(f.get("tier"))}"' if f.get("tier") else ""
        )
        arts.append(
            f'<article class="r-fu" data-id="{_esc(f.get("id"))}" data-status="{_esc(status)}"'
            f'{legacy_tier}{_capability_attributes(f.get("capability"))}'
            f' data-written-by="{_esc(f.get("written_by"))}"'
            f' data-written-at="{_esc(f.get("written_at"))}" data-recommends-skill="{_esc(f.get("recommends_skill"))}"'
            f' data-resolved-at="{_esc(f.get("resolved_at") or "")}" data-resolved-by="{_esc(f.get("resolved_by") or "")}">\n    '
            f'<h4 class="r-fu-title">{_esc(f.get("title"))}</h4>\n    '
            f'<div class="r-fu-body">{_body(f.get("body"))}</div>\n    '
            f'{prompt}{outcome}</article>'
        )
    return ('<section data-reckon="followups" id="followups" class="r-followups">\n'
            '<h2><span class="sec">§</span> Followups</h2>\n'
            + "\n".join(arts) + "\n</section>")


def _render_questions(questions: list) -> str:
    if not questions or not isinstance(questions, list):
        return ""
    items = []
    for q in questions:
        q = q or {}
        res = f'<p class="r-q-resolution">{_body(q.get("resolution"))}</p>\n    ' if q.get("resolution") else ""
        status = "resolved" if q.get("resolved_at") else "open"
        items.append(
            f'<div class="r-q" data-id="{_esc(q.get("id"))}" data-section="{_esc(q.get("section"))}"'
            f' data-status="{status}" data-opened-by="{_esc(q.get("opened_by"))}"'
            f' data-opened-at="{_esc(q.get("opened_at"))}"'
            f' data-resolved-at="{_esc(q.get("resolved_at") or "")}" data-resolved-by="{_esc(q.get("resolved_by") or "")}">\n    '
            f'<p class="r-q-body">{_body(q.get("body"))}</p>\n    {res}</div>'
        )
    return ('<section data-reckon="questions" id="questions" class="r-questions">\n'
            '<h2><span class="sec">§</span> Open questions</h2>\n'
            + "\n".join(items) + "\n</section>")


def _render_research(research: list) -> str:
    if not research or not isinstance(research, list):
        return ""
    items = []
    for r in research:
        r = r or {}
        url = f' data-url="{_esc(r.get("url"))}"' if r.get("url") else ""
        title = _esc(r.get("title"))
        title_html = f'<a href="{_esc(r.get("url"))}">{title}</a>' if r.get("url") else title
        items.append(
            f'<div class="r-research" data-id="{_esc(r.get("id"))}" data-type="{_esc(r.get("type"))}"'
            f' data-source="{_esc(r.get("source"))}" data-added-by="{_esc(r.get("added_by"))}"'
            f' data-when="{_esc(r.get("when"))}"{url}>\n    '
            f'<span class="r-research-title">{title_html}</span></div>'
        )
    return ('<section data-reckon="research" id="research" class="r-research-list">\n'
            '<h2><span class="sec">§</span> Research</h2>\n'
            + "\n".join(items) + "\n</section>")


def _render_comments(comments: dict) -> str:
    if not comments or not isinstance(comments, dict):
        return ""
    items = []
    for sid, arr in comments.items():
        for c in (arr or []):
            c = c or {}
            quote = f' data-quote="{_esc(c.get("quote"))}"' if c.get("quote") else ""
            items.append(
                f'<div class="r-comment" data-section="{_esc(sid)}" data-id="{_esc(c.get("id"))}"'
                f' data-who="{_esc(c.get("who"))}" data-when="{_esc(c.get("when"))}"{quote}>\n    '
                f'<div class="r-comment-body">{_body(c.get("body"))}</div>\n</div>'
            )
    if not items:
        return ""
    return ('<section data-reckon="comments" id="comments" class="r-comments">\n'
            + "\n".join(items) + "\n</section>")


_RENDERERS = {
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


def _splice_section(html_text: str, reckon_id: str, rendered: str) -> str:
    """Replace <section data-reckon="ID">…</section> with `rendered`
    (removes it when `rendered` is empty); inserts before </main> otherwise."""
    pat = re.compile(
        rf'<section[^>]*data-reckon="{re.escape(reckon_id)}"[^>]*>.*?</section>',
        re.IGNORECASE | re.DOTALL,
    )
    if pat.search(html_text):
        return pat.sub(lambda _: rendered, html_text, count=1) if rendered else pat.sub("", html_text, count=1)
    if not rendered:
        return html_text
    for anchor in ("</main>", "</body>", "</html>"):
        idx = html_text.lower().rfind(anchor)
        if idx != -1:
            return html_text[:idx] + rendered + "\n" + html_text[idx:]
    return html_text + "\n" + rendered


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
    for f in _SCALARS:
        if f in state and state[f] is not None:
            out = _set_meta(out, f"plan-{f.replace('_', '-')}", state[f])
    for f in _LIST_SCALARS:
        if f in state:
            out = _set_meta(out, f"plan-{f.replace('_', '-')}", ",".join(state.get(f) or []))
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
    if "impl" in state:
        out = _set_meta(out, "plan-impl", state["impl"])
    if "version" in state:
        out = _set_meta(out, "plan-version", int(state.get("version") or 0))
    for sid in SECTION_IDS:
        if sid in state:
            out = _splice_section(out, sid, _RENDERERS[sid](state[sid]))
    return out


# ── Schema-typed wrappers (PlanState contract) ──────────────────────────────
#
# These WRAP read_state/write_state — they do not replace them. read_state and
# write_state keep their dict signatures and current output; existing callers
# (serve.py, _store.py) stay untouched. Explicit write-boundary callers use
# from_html / validate_for_write.

def from_html(html_text: str) -> "PlanState":  # noqa: F821  (forward ref)
    """Parse HTML into a typed :class:`reckon._schema.PlanState` — LENIENT.

    Equivalent to ``PlanState.model_validate(read_state(html_text))`` with the
    schema's lenient coercion (roi med→mid, type doc→research, derived statuses,
    unknown attrs dropped). NEVER raises on a real plan — every existing plan
    validates on read; required-on-write fields carry read defaults. Use
    :meth:`PlanState.validate_for_write` for the strict write path.
    """
    from reckon._schema import PlanState
    return PlanState.model_validate(read_state(html_text))


def to_html(html_text: str, state: "PlanState") -> str:  # noqa: F821
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
_DEC_BLOCK_RE = re.compile(r'<div\b[^>]*\bclass="r-dec".*?</div>', re.IGNORECASE | re.DOTALL)
_DEC_CHOICE_RE = re.compile(r'\bdata-choice="([^"]*)"', re.IGNORECASE)
_DEC_RAT_RE = re.compile(r'class="r-dec-rat"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')


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


_META_RE = re.compile(r'<meta\b[^>]*>', re.IGNORECASE)
_NAME_RE = re.compile(r'\bname=["\']([^"\']+)["\']', re.IGNORECASE)
_CONTENT_RE = re.compile(r'\bcontent=["\']([^"\']*)["\']', re.IGNORECASE)
_TITLE_RE = re.compile(r'<title>(.*?)</title>', re.IGNORECASE | re.DOTALL)


def parse_meta(path: Path, slug: str | None = None) -> dict:
    """Fast inventory record: <meta> + <title> + a regex open-decision count,
    parsed by regex (no bs4) so a project with thousands of docs stays cheap.
    Full state is read per-doc via parse_plan.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    head = text[:16384]
    rec = dict(_DEFAULTS)
    metas: dict[str, str] = {}
    for tag in _META_RE.findall(head):
        nm = _NAME_RE.search(tag)
        if not nm:
            continue
        ct = _CONTENT_RE.search(tag)
        metas[nm.group(1).lower()] = ct.group(1) if ct else ""
    for name, content in metas.items():
        if not name.startswith("plan-") or content == "":
            continue
        field = name[len("plan-"):].replace("-", "_")
        if field in _SCALARS:
            rec[field] = content
        elif field in _LIST_SCALARS:
            rec[field] = [x.strip() for x in content.split(",") if x.strip()]
        elif field == "impl":
            try:
                rec["impl"] = float(content)
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
    rec["dec_open"] = count_open_decisions(text)
    rec["impl"] = float(rec.get("impl", 0) or 0)
    rec["version"] = int(rec.get("version", 0) or 0)
    rec["blockers"] = 0
    return rec


# ── Plan record (inventory + full state) ────────────────────────────────────

def parse_plan(path: Path, slug: str | None = None) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
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
        {"key": k, **{kk: vv for kk, vv in (d or {}).items()}, "chosen": (d or {}).get("choice", "")}
        for k, d in decisions_map.items()
    ]
    rec["followups"] = st.get("followups") or []
    rec["comments"] = st.get("comments") or {}
    rec["questions"] = st.get("questions") or []
    rec["research"] = st.get("research") or []
    rec["depends_on"] = st.get("depends_on") or []
    rec["blocks"] = st.get("blocks") or []
    rec["dec_open"] = sum(1 for d in rec["decisions"] if _decision_open(d.get("choice"), d.get("rationale")))
    rec["blockers"] = int(st.get("blockers", 0) or 0)
    rec["impl"] = float(rec.get("impl", 0) or 0)
    rec["version"] = int(st.get("version", 0) or 0)
    return rec
