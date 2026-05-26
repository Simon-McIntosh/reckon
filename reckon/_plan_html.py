#!/usr/bin/env python3
"""HTML plan state engine — the plan HTML file is the sole store.

Each plan page embeds a single state island:

    <script type="application/json" id="reckon-state">
    { ...canonical plan data... }
    </script>

All mutable plan data (status, impl, decisions, followups, comments,
questions, research, notes) and authored metadata (title, summary, roi,
effort, milestone, sprint, tier, depends_on) live in that island — there
is no sidecar state/<project>/<slug>.json.

Writes replace ONLY the island's inner text, leaving the rest of the file
byte-for-byte unchanged, so live edits produce minimal git diffs.

Reads degrade gracefully: a bare HTML file with no island still parses into
a valid plan record (slug from filename, title from <title>, status=draft).
"""

from __future__ import annotations

import html.parser
import json
import re
from pathlib import Path

# Matches the state island and captures its inner text (group "body").
_ISLAND_RE = re.compile(
    r'(?P<open><script\b[^>]*\bid=["\']reckon-state["\'][^>]*>)'
    r'(?P<body>.*?)'
    r'(?P<close></script>)',
    re.IGNORECASE | re.DOTALL,
)

# Authored metadata fields read from <meta name="plan-*"> as a fallback when
# the island omits them. The island is always authoritative when present.
_META_FIELDS = {
    "plan-title": "title",
    "plan-summary": "summary",
    "plan-status": "status",
    "plan-roi": "roi",
    "plan-effort": "effort",
    "plan-milestone": "milestone",
    "plan-sprint": "sprint",
    "plan-tier": "tier",
    "plan-modified": "modified",
}

# Inventory entry defaults — every surfaced plan has these keys.
_DEFAULTS = {
    "status": "draft",
    "roi": "mid",
    "effort": "M",
    "milestone": "—",
    "sprint": None,
    "tier": "sonnet",
    "summary": "",
    "modified": "",
    "impl": 0.0,
    "version": 0,
}


class _HeadParser(html.parser.HTMLParser):
    """Extract <meta name=…> and <title> from the <head> only."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._done:
            return
        if tag == "body":
            self._done = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            d = dict(attrs)
            name = (d.get("name") or "").lower()
            if name:
                self.meta[name] = d.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("head", "body"):
            self._done = True
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self._done:
            self.title += data


def _normalise_decisions(raw) -> list[dict]:
    """Map (canonical) or legacy list -> ordered list of renderer records.

    Each record carries: key, title, context, choices[], choice, chosen,
    rationale, when, by. `chosen` mirrors `choice` for the SPA's form state.
    """
    def _rec(key: str, v: dict) -> dict:
        choice = v.get("choice", "") or ""
        return {
            "key": key,
            "title": v.get("title") or v.get("question") or key,
            "context": v.get("context", ""),
            "choices": v.get("choices") or v.get("options") or [],
            "choice": choice,
            "chosen": choice,
            "rationale": v.get("rationale", ""),
            "when": v.get("when", ""),
            "by": v.get("by", ""),
        }

    out: list[dict] = []
    if isinstance(raw, dict):
        for key, v in raw.items():
            out.append(_rec(key, v if isinstance(v, dict) else {}))
    elif isinstance(raw, list):
        for v in raw:
            if isinstance(v, dict) and v.get("key"):
                out.append(_rec(v["key"], v))
    return out


def read_island(html_text: str) -> dict:
    """Return the parsed state island, or {} if absent/unparseable."""
    m = _ISLAND_RE.search(html_text)
    if not m:
        return {}
    try:
        obj = json.loads(m.group("body").strip() or "{}")
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def write_island(html_text: str, state: dict) -> str:
    """Return html_text with the state island's body replaced by `state`.

    If no island exists, one is inserted just before </main> (or </body>).
    The serialized JSON is pretty-printed for readable diffs. Only the island
    text changes; the surrounding document is preserved exactly.
    """
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    block_body = f"\n{payload}\n"

    def _repl(m: re.Match) -> str:
        return f'{m.group("open")}{block_body}{m.group("close")}'

    new_text, n = _ISLAND_RE.subn(_repl, html_text, count=1)
    if n:
        return new_text

    island = (
        '<script type="application/json" id="reckon-state">'
        f"{block_body}</script>\n"
    )
    for anchor in ("</main>", "</body>", "</html>"):
        idx = html_text.lower().rfind(anchor)
        if idx != -1:
            return html_text[:idx] + island + html_text[idx:]
    return html_text + "\n" + island


def _head(html_text: str) -> tuple[str, dict[str, str]]:
    p = _HeadParser()
    try:
        p.feed(html_text[:16384])
    except Exception:
        pass
    return p.title.strip(), p.meta


def parse_plan(path: Path, slug: str | None = None) -> dict:
    """Parse a plan HTML file into a canonical record.

    Precedence: island fields > <meta name=plan-*> > defaults. `slug`
    defaults to the island slug, then plan-slug meta, then the filename stem.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""

    island = read_island(text)
    title_tag, meta = _head(text)

    rec: dict = dict(_DEFAULTS)

    # Authored metadata from <meta> (fallback layer).
    for meta_name, field in _META_FIELDS.items():
        if meta_name in meta and meta[meta_name] != "":
            rec[field] = meta[meta_name]

    # Island overrides everything it specifies.
    rec.update({k: v for k, v in island.items() if v is not None})

    rec["slug"] = (
        slug or island.get("slug") or meta.get("plan-slug") or path.stem
    )
    rec["title"] = (
        island.get("title") or meta.get("plan-title") or title_tag or rec["slug"]
    )

    # Decisions are stored as an ordered MAP keyed by decision key (so the SPA
    # can write `decisions.<key>.choice` dotted patches). parse_plan emits an
    # ordered LIST of records the renderer consumes. A legacy list is accepted.
    rec["decisions"] = _normalise_decisions(island.get("decisions"))
    rec["followups"] = island.get("followups") or []
    rec["comments"] = island.get("comments") or {}
    rec["questions"] = island.get("questions") or []
    rec["research"] = island.get("research") or []
    rec["notes"] = island.get("notes") or []
    rec["depends_on"] = island.get("depends_on") or []
    rec["blocks"] = island.get("blocks") or []

    # Derived counts.
    rec["dec_open"] = sum(1 for d in rec["decisions"] if not d.get("choice"))
    rec["blockers"] = int(island.get("blockers", 0) or 0)
    rec["impl"] = float(rec.get("impl", 0) or 0)
    rec["version"] = int(island.get("version", 0) or 0)
    return rec
