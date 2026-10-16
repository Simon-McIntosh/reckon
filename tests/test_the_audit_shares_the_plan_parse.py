"""A cold audit builds each plan file's soup once, through the shared parse.

The audit parses plans through ``_audit_plan_state``, whose route to
:class:`PlanState` was its own ``_plan_html.from_html`` call — a soup built
beside the shared parse the rest of the read reaches through
``read_state_and_text_file``. A record-carrying plan is parsed twice per cold
audit: once by the reader that derives its inventory, and again by the audit's
validation loop. Routing ``_audit_plan_state`` through the shared parse leaves
one soup per plan file.

The count of soups is the property under test, so ``_plan_html.BeautifulSoup``
is wrapped and counted per source text. The audit's findings are checked
against its own-parse route so the output is shown unchanged, and the negative
control restores that own parse, after which the count case fails.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, mcp, metadata_index
from reckon._plan_html import write_state
from reckon._schema import PlanState

PROJECT = "sample"
PLANS = 6

DECLARATIONS = {"s1": "done"}
CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}
SECTION_RECORDS = [
    {
        "id": section_id,
        "effort_hours": 1.0,
        "capability": deepcopy(CAPABILITY),
        "attempts": 0,
        "status": status,
        "links": [],
    }
    for section_id, status in DECLARATIONS.items()
]


@pytest.fixture()
def soups(monkeypatch) -> Counter:
    """Count every soup built, keyed by the text it was built from."""

    counts: Counter = Counter()
    original = _plan_html.BeautifulSoup

    def counting(markup="", *args, **kwargs):
        counts[str(markup)] += 1
        return original(markup, *args, **kwargs)

    monkeypatch.setattr(_plan_html, "BeautifulSoup", counting)
    return counts


@pytest.fixture(autouse=True)
def _isolated_caches():
    """The parse memos are process-wide; each test starts empty."""
    file_memo.clear()
    metadata_index.clear()
    yield
    file_memo.clear()
    metadata_index.clear()


def _authored(slug: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f"<title>{slug}</title></head><body>"
        '<main class="plan-doc">'
        '<h2 id="s1">First section</h2><p>First body.</p>'
        "</main></body></html>"
    )


def _plan_state(slug: str) -> dict:
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": slug,
        "title": slug,
        "status": "active",
        "modified": "2026-10-01",
        "version": 0,
        "section_declarations": deepcopy(DECLARATIONS),
        "sections": deepcopy(SECTION_RECORDS),
    }


def _corpus(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A distributed project of ``PLANS`` record-carrying plans."""

    docs = tmp_path / PROJECT / "docs"
    (docs / ".reckon").mkdir(parents=True)
    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps(
            {
                "format": "distributed",
                "status": "complete",
                "project": PROJECT,
                "resources": [],
            }
        ),
        encoding="utf-8",
    )
    (docs / "plans").mkdir()
    texts: dict[str, str] = {}
    for index in range(PLANS):
        slug = f"plan-{index:02d}"
        text = write_state(_authored(slug), _plan_state(slug))
        (docs / "plans" / f"{slug}.html").write_text(text, encoding="utf-8")
        texts[slug] = text
    return docs, texts


def _findings(result: dict) -> list[tuple]:
    return sorted(
        (f.get("code"), f.get("slug"), f.get("path"), f.get("severity"))
        for f in result.get("findings", [])
    )


def test_a_cold_audit_builds_each_plans_soup_once(tmp_path, soups) -> None:
    docs, texts = _corpus(tmp_path)
    file_memo.clear()
    metadata_index.clear()
    soups.clear()

    mcp._audit(project=PROJECT, checkout_path=str(docs.parent))

    for slug, text in texts.items():
        assert soups[text] == 1, f"a cold audit built {slug}'s soup {soups[text]} times"


def test_the_audit_output_is_unchanged_from_its_own_parse_route(tmp_path) -> None:
    docs, _texts = _corpus(tmp_path)
    file_memo.clear()
    metadata_index.clear()

    shared = mcp._audit(project=PROJECT, checkout_path=str(docs.parent))

    # The route this node replaced: ``_audit_plan_state`` building its own
    # soup from the file's text. Findings must not depend on which route the
    # audit's validation loop takes.
    from reckon import _plan_html as plan_html_module

    own_parse = lambda path: PlanState.model_validate(  # noqa: E731
        plan_html_module.from_html(plan_html_module._read_plan_text(path))
    )
    file_memo.clear()
    metadata_index.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp, "_audit_plan_state", own_parse)
        own = mcp._audit(project=PROJECT, checkout_path=str(docs.parent))

    assert _findings(shared) == _findings(own)
    assert shared.get("checked") == own.get("checked")
    assert shared.get("violations") == own.get("violations")
