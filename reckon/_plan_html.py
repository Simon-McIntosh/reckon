#!/usr/bin/env python3
"""HTML plan state engine — the plan's data IS semantic HTML.

A plan page carries its data as ordinary HTML the reader can see and the
reckon server reads and writes directly. There is NO embedded JSON blob.

  - Scalars (status, impl, version, roi, effort, milestone, sprint, tier,
    summary, owner, modified, slug, depends_on) live in <meta name="plan-*">.
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

# ── Scalar fields carried in <meta name="plan-*"> ──────────────────────────
_SCALARS = (
    "slug", "title", "summary", "status", "roi", "effort", "milestone",
    "sprint", "tier", "owner", "modified",
)
_LIST_SCALARS = ("depends_on", "blocks", "informs")  # comma-separated in meta

_DEFAULTS = {
    "status": "draft", "roi": "mid", "effort": "M", "milestone": "—",
    "sprint": None, "tier": "sonnet", "summary": "", "modified": "",
    "owner": "", "impl": 0.0, "version": 0,
}

# reckon-owned section ids — regenerated on write, stripped by the SPA before
# rendering the authored prose (it renders interactive widgets instead).
SECTION_IDS = ("decisions", "followups", "questions", "research", "comments")


def _esc(s) -> str:
    return _htmlmod.escape("" if s is None else str(s), quote=True)


def _txt(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


# ── Read ───────────────────────────────────────────────────────────────────

def read_state(html_text: str) -> dict:
    """Parse a plan's semantic HTML into the canonical state dict."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    st: dict = {}

    # Scalars from <meta name="plan-*">
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").lower()
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

    # Document type: plan (actionable) | research (non-actionable input).
    rt = soup.find("meta", attrs={"name": "reckon-type"})
    st["type"] = ((rt.get("content") if rt else "") or "plan").strip().lower()

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
        f = {
            "id": fu.get("data-id", ""),
            "status": fu.get("data-status", "open"),
            "tier": fu.get("data-tier", ""),
            "written_by": fu.get("data-written-by", ""),
            "written_at": fu.get("data-written-at", ""),
            "recommends_skill": fu.get("data-recommends-skill", ""),
            "title": _txt(fu.select_one(".r-fu-title")),
            "body": _txt(fu.select_one(".r-fu-body")),
            "prompt": (fu.select_one(".r-fu-prompt").get_text() if fu.select_one(".r-fu-prompt") else ""),
        }
        if fu.get("data-resolved-at"):
            f["resolved_at"] = fu.get("data-resolved-at")
        if fu.get("data-resolved-by"):
            f["resolved_by"] = fu.get("data-resolved-by")
        outcome = _txt(fu.select_one(".r-fu-outcome"))
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
            "body": _txt(q.select_one(".r-q-body")) or _txt(q),
            "resolution": _txt(q.select_one(".r-q-resolution")) or None,
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
            "body": _txt(c.select_one(".r-comment-body")) or _txt(c),
        })
    st["comments"] = comments
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
        prompt = f'<pre class="r-fu-prompt">{_esc(f.get("prompt"))}</pre>\n    ' if f.get("prompt") else ""
        outcome = f'<p class="r-fu-outcome">{_esc(f.get("outcome"))}</p>\n    ' if f.get("outcome") else ""
        arts.append(
            f'<article class="r-fu" data-id="{_esc(f.get("id"))}" data-status="{_esc(f.get("status") or "open")}"'
            f' data-tier="{_esc(f.get("tier"))}" data-written-by="{_esc(f.get("written_by"))}"'
            f' data-written-at="{_esc(f.get("written_at"))}" data-recommends-skill="{_esc(f.get("recommends_skill"))}"'
            f' data-resolved-at="{_esc(f.get("resolved_at") or "")}" data-resolved-by="{_esc(f.get("resolved_by") or "")}">\n    '
            f'<h4 class="r-fu-title">{_esc(f.get("title"))}</h4>\n    '
            f'<div class="r-fu-body">{_esc(f.get("body"))}</div>\n    '
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
        res = f'<p class="r-q-resolution">{_esc(q.get("resolution"))}</p>\n    ' if q.get("resolution") else ""
        status = "resolved" if q.get("resolved_at") else "open"
        items.append(
            f'<div class="r-q" data-id="{_esc(q.get("id"))}" data-section="{_esc(q.get("section"))}"'
            f' data-status="{status}" data-opened-by="{_esc(q.get("opened_by"))}"'
            f' data-opened-at="{_esc(q.get("opened_at"))}"'
            f' data-resolved-at="{_esc(q.get("resolved_at") or "")}" data-resolved-by="{_esc(q.get("resolved_by") or "")}">\n    '
            f'<p class="r-q-body">{_esc(q.get("body"))}</p>\n    {res}</div>'
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
                f'<div class="r-comment-body">{_esc(c.get("body"))}</div>\n</div>'
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
    if state.get("type"):
        out = _set_meta(out, "reckon-type", state["type"])
    for f in _SCALARS:
        if f in state and state[f] is not None:
            out = _set_meta(out, f"plan-{f}", state[f])
    for f in _LIST_SCALARS:
        if f in state:
            out = _set_meta(out, f"plan-{f.replace('_', '-')}", ",".join(state.get(f) or []))
    if "impl" in state:
        out = _set_meta(out, "plan-impl", state["impl"])
    if "version" in state:
        out = _set_meta(out, "plan-version", int(state.get("version") or 0))
    for sid in SECTION_IDS:
        if sid in state:
            out = _splice_section(out, sid, _RENDERERS[sid](state[sid]))
    return out


# ── Lightweight inventory record (scales to thousands of docs) ──────────────

_OPEN_DEC_RE = re.compile(r'class="r-dec"[^>]*\bdata-choice=""', re.IGNORECASE)
_ANY_DEC_RE = re.compile(r'class="r-dec"[\s">]', re.IGNORECASE)


def parse_meta(path: Path, slug: str | None = None) -> dict:
    """Fast inventory record: head <meta> + title + a regex open-decision
    count, without a full-body parse. Used by discovery so a project with
    thousands of docs stays cheap; full state is read per-doc via parse_plan.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    soup = BeautifulSoup(text[:16384], "html.parser")  # head only — cheap
    rec = dict(_DEFAULTS)
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").lower()
        content = m.get("content", "")
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
    rt = soup.find("meta", attrs={"name": "reckon-type"})
    rec["type"] = ((rt.get("content") if rt else "") or "plan").strip().lower()
    title_tag = soup.find("title")
    if title_tag and not rec.get("title"):
        rec["title"] = title_tag.get_text(strip=True).split("|")[0].strip()
    rec["slug"] = slug or rec.get("slug") or path.stem
    rec["title"] = rec.get("title") or rec["slug"]
    rec["informs"] = rec.get("informs") or []
    rec["depends_on"] = rec.get("depends_on") or []
    rec["dec_open"] = len(_OPEN_DEC_RE.findall(text))
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
    rec["dec_open"] = sum(1 for d in rec["decisions"] if not d.get("choice"))
    rec["blockers"] = int(st.get("blockers", 0) or 0)
    rec["impl"] = float(rec.get("impl", 0) or 0)
    rec["version"] = int(st.get("version", 0) or 0)
    return rec
