"""Archive-dir docs are discovered, stamped archived, and slug-resolvable.

Per-stage landed records live under docs/archive/; the SPA keeps them behind
its "Show archived" toggle, but they must appear in the inventory and resolve
by slug so links from evergreen plans render.
"""

from pathlib import Path

from reckon.serve import _resolve_plan_file, discover_plans


def _write_doc(path: Path, slug: str, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="proj">'
        f'<meta name="reckon-type" content="doc">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{title}">'
        f'<meta name="plan-status" content="reference">'
        f"<title>{title}</title></head><body><main></main></body></html>"
    )


def test_archive_docs_discovered_as_archived(tmp_path):
    docs = tmp_path / "docs"
    _write_doc(docs / "live-plan.html", "live-plan", "Live plan")
    _write_doc(docs / "archive" / "old-stage-landed.html", "old-stage-landed", "Landed")

    inv = discover_plans(docs, "proj", None)["inventory"]
    by_slug = {r["slug"]: r for r in inv}
    assert "live-plan" in by_slug and "old-stage-landed" in by_slug
    assert by_slug["live-plan"]["archived"] == ""
    assert by_slug["old-stage-landed"]["archived"] == "1"
    # href must include the subdirectory so the SPA fetch resolves
    assert by_slug["old-stage-landed"]["href"] == "archive/old-stage-landed"


def test_archive_doc_resolves_by_slug(tmp_path):
    docs = tmp_path / "docs"
    _write_doc(docs / "archive" / "old-stage-landed.html", "old-stage-landed", "Landed")
    found = _resolve_plan_file(docs, "old-stage-landed")
    assert found is not None and found.name == "old-stage-landed.html"
