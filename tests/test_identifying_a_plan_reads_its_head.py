"""Identifying a plan reads its head, not its body.

A slug resolve identifies every sibling in the typed root to settle uniqueness,
and identity — the type, slug and archived flag — is declared in the head. Once
``parse_meta`` began deriving ``impl`` from section records, identification paid
for the whole-body parser on every resolve whose memo was cold, which is the
cost the picker's capability step and the roadmap read both absorbed.

The count of ``read_state`` entries is the property under test: identification
builds no soup, so a cold resolve and a bare identification enter it zero times.
``parse_meta`` still answers the derived figure, so the record fast path keeps
deferring to the parser.

The negative control routes identification back through the full ``parse_meta``
(``identify_resource`` calling ``parse_meta`` rather than ``_parse_meta_head``),
under which the zero-entry case fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, metadata_index
from reckon.resources import identify_resource, resolve_resource

#: A plan carrying section records. s1 is done and s2 implementable, each worth
#: two hours, so the parser derives impl 0.5 (``computed``) — a figure distinct
#: from the authored ``plan-impl`` the head alone declares, which is what makes
#: "the record still defers to the parser" a real assertion rather than a
#: tautology on a default of zero.
_PLAN_DOC = """<!doctype html><html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="reckon-id" content="{slug}">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-impl" content="0.1">
<title>{title}</title>
</head><body>
<h2 id="s1">Section one</h2>
<section data-reckon="section" data-id="s1" data-effort-hours="2.0" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="deep" data-capability-verification="strict" data-capability-risk="low" data-status="done" data-links=""></section>
<h2 id="s2">Section two</h2>
<section data-reckon="section" data-id="s2" data-effort-hours="2.0" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="deep" data-capability-verification="strict" data-capability-risk="low" data-status="implementable" data-links=""></section>
</body></html>
"""

_PLAN_COUNT = 12


@pytest.fixture(autouse=True)
def _isolated_caches():
    """Both memos are process-wide; each test starts from an empty pair."""
    file_memo.clear()
    metadata_index.clear()
    yield
    file_memo.clear()
    metadata_index.clear()


def _build_tree(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    plans = docs / "plans"
    for index in range(_PLAN_COUNT):
        slug = f"plan-{index:02d}"
        (plans).mkdir(parents=True, exist_ok=True)
        (plans / f"{slug}.html").write_text(
            _PLAN_DOC.format(slug=slug, title=f"Plan {index}"),
            encoding="utf-8",
        )
    return docs


def _counting_read_state(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Count the parsed reads identification performs, without changing them."""
    calls = {"count": 0}
    real = _plan_html.read_state

    def counting(text: str) -> dict:
        calls["count"] += 1
        return real(text)

    monkeypatch.setattr(_plan_html, "read_state", counting)
    return calls


def test_cold_resolve_enters_the_parser_zero_times(tmp_path, monkeypatch):
    """A cold slug resolve identifies siblings from their heads alone."""
    docs = _build_tree(tmp_path)
    calls = _counting_read_state(monkeypatch)
    file_memo.clear()
    metadata_index.clear()

    resource = resolve_resource(docs, "proj", "plan-05", "plan")

    assert resource is not None
    assert resource.path == docs / "plans" / "plan-05.html"
    assert calls["count"] == 0


def test_identification_enters_the_parser_zero_times(tmp_path, monkeypatch):
    """A bare identification of a record-carrying plan builds no soup."""
    docs = _build_tree(tmp_path)
    calls = _counting_read_state(monkeypatch)
    file_memo.clear()
    metadata_index.clear()

    resource = identify_resource(docs, docs / "plans" / "plan-07.html", "proj")

    assert resource is not None
    assert resource.slug == "plan-07"
    assert calls["count"] == 0


def test_parse_meta_still_defers_to_the_parser(tmp_path, monkeypatch):
    """The record reader answers the derived figure the head does not carry."""
    docs = _build_tree(tmp_path)
    path = docs / "plans" / "plan-03.html"
    text = path.read_text(encoding="utf-8")

    head_rec = _plan_html._parse_meta_head(path)
    # The head declares the authored figure and no derived source.
    assert head_rec["impl"] == 0.1
    assert head_rec.get("impl_source") is None

    parsed_state = _plan_html.read_state(text)
    expected_impl, expected_source = _plan_html.derive_impl_with_source(
        parsed_state.get("sections"), parsed_state.get("section_declarations")
    )
    assert expected_source == "computed"
    assert expected_impl == 0.5

    rec = _plan_html.parse_meta(path)

    assert rec["impl"] == expected_impl
    assert rec["impl_source"] == expected_source
    # The reader classifies with the head record, so identification never asks
    # for the derived figure it would have to parse the body to produce.
    assert _plan_html._parse_meta_head(path)["slug"] == rec["slug"]
    assert _plan_html._parse_meta_head(path)["type"] == rec["type"]
