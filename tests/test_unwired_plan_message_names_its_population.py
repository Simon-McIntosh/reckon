"""The unwired-plan message names the population its count is a count of.

The wiring rule reads a document's DECLARED type, so what an audit counts is
plan documents — an evidence or research document beside a plan in the same
docs tree is never a row. A reader of a count has to be able to tell what was
counted, so the message states the population, and this file pins both halves:
the message carries it, and an audit over a synthetic tree holding evidence
and research documents beside plans counts the plans alone.
"""

from __future__ import annotations

from pathlib import Path

from reckon import _schema
from reckon.doccheck import audit_file

ENFORCED_FROM = "2026-09-19"


def _document(slug: str, *, declared_type: str) -> str:
    head = "".join(
        [
            f'<meta name="reckon-type" content="{declared_type}">',
            f'<meta name="plan-slug" content="{slug}">',
            '<meta name="plan-status" content="active">',
            f'<meta name="plan-modified" content="{ENFORCED_FROM}">',
        ]
    )
    return (
        '<!doctype html><html lang="en"><head>'
        f"{head}<title>{slug}</title></head>"
        f'<body><main class="plan-doc"><p>{slug}</p></main></body></html>'
    )


def _tree(tmp_path: Path) -> tuple[list[Path], list[Path]]:
    """A synthetic docs tree and the plan documents inside it.

    Two unwired implementable plans sit beside one evidence document and one
    research document, so a count that swallowed the whole tree would read
    four while a plans-scoped one reads two.
    """

    root = tmp_path / "docs"
    root.mkdir()
    declared = {
        "alpha": "plan",
        "beta": "plan",
        "a-landed": "evidence",
        "a-study": "research",
    }
    paths = []
    plans = []
    for slug, declared_type in declared.items():
        path = root / f"{slug}.html"
        path.write_text(_document(slug, declared_type=declared_type), encoding="utf-8")
        paths.append(path)
        if declared_type == "plan":
            plans.append(path)
    return sorted(paths), sorted(plans)


def _unwired(path: Path):
    return [finding for finding in audit_file(path) if finding.code == "unwired-plan"]


def test_message_states_the_population_the_count_is_over():
    message = _schema.unwired_plan_message(slug="sample")

    assert "plan documents" in message
    assert "not every document" in message


def test_audit_counts_plan_documents_beside_evidence_and_research(tmp_path: Path):
    paths, plans = _tree(tmp_path)

    reported = {path.name: _unwired(path) for path in paths}
    counted = sorted(name for name, rows in reported.items() if rows)

    # The count is the plan documents alone; the evidence and research
    # documents in the same tree carry no wiring finding.
    assert counted == [path.name for path in plans]
    assert sum(len(rows) for rows in reported.values()) == len(plans)
    for rows in reported.values():
        for row in rows:
            assert "plan documents" in row.message
