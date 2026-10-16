"""One audit read walks the resource tree once, through the shared scan.

The audit used to enumerate its plans with its own tree walk,
``sorted(docs_dir.rglob("*.html"))`` and a direct :func:`identify_resource` per
file, beside the scan the rest of the read already reaches through
:func:`iter_resources`. Now the whole read runs inside one
:func:`resource_scan_scope`: the tree is walked once, its identification memo is
reused, and the files the scan drops as invalid are reported from the scan's own
record rather than from a walk the audit keeps.

The count of walks is the property under test, so ``resources._scan_resources``
is wrapped and counted. The audit's findings are checked to be unchanged, both
for a clean corpus and for one carrying an invalid resource path, and the
audit's invalid-resource findings are checked against a direct walk computed in
the test.

The negative controls restore the audit's own ``rglob`` walk — the walk-count
case then fails — and drop invalid files from the scan without recording them —
the invalid-resource case then fails, because the audit loses its
``invalid-resource-path`` finding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import file_memo, mcp, metadata_index, resources
from reckon.resources import (
    ResourceCollision,
    identify_resource,
    iter_resources,
    resource_scan_scope,
)

PROJECT = "sample"


def _plan(slug: str, *, doc_type: str = "plan") -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f'<meta name="reckon-type" content="{doc_type}">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="1.0">'
        '<meta name="plan-modified" content="2026-10-01">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"><h2 id="s1">Body</h2></main></body></html>'
    )


def _corpus(tmp_path: Path, *, invalid: bool, invalid_name: str = "bad.html") -> Path:
    """Two live plans, and — when ``invalid`` — a file whose location type
    conflicts with its declared ``reckon-type``."""
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
    for slug in ("alpha", "beta"):
        (docs / "plans" / f"{slug}.html").write_text(_plan(slug), encoding="utf-8")
    if invalid:
        (docs / "plans" / invalid_name).write_text(
            _plan(Path(invalid_name).stem, doc_type="research"), encoding="utf-8"
        )
    return docs


@pytest.fixture(autouse=True)
def _isolated_caches():
    """The identification memo is process-wide; each test starts empty."""
    file_memo.clear()
    metadata_index.clear()
    yield
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture
def counted_walks(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record every call to the tree walk, delegating to the real one."""
    calls: list[tuple] = []
    real = resources._scan_resources

    def counting(docs_dir, project, **flags):
        calls.append((docs_dir, project, flags))
        return real(docs_dir, project, **flags)

    monkeypatch.setattr(resources, "_scan_resources", counting)
    return calls


def _findings(result: dict) -> list[tuple]:
    return sorted(
        (f.get("code"), f.get("slug"), f.get("path"), f.get("severity"))
        for f in result.get("findings", [])
    )


def _direct_invalid_resources(docs: Path) -> list[tuple[str, str]]:
    """The invalid files a walk of the tree finds, computed without the audit."""
    found: list[tuple[str, str]] = []
    for path in sorted(docs.rglob("*.html")):
        try:
            identify_resource(docs, path, PROJECT)
        except ResourceCollision as exc:
            found.append((str(path.relative_to(docs)), str(exc)))
    return found


def test_one_audit_read_walks_the_tree_once(
    tmp_path: Path, counted_walks: list[tuple]
) -> None:
    docs = _corpus(tmp_path, invalid=True)

    mcp._audit(project=PROJECT, checkout_path=str(docs.parent))

    assert len(counted_walks) == 1, counted_walks
    _docs_dir, _project, flags = counted_walks[0]
    assert flags == {
        "include_archived": True,
        "include_legacy": True,
        "ignore_invalid": True,
    }


def test_the_audit_reports_the_invalid_resource_path(tmp_path: Path) -> None:
    docs = _corpus(tmp_path, invalid=True)

    result = mcp._audit(project=PROJECT, checkout_path=str(docs.parent))

    reported = sorted(
        (f["path"], f["message"])
        for f in result["findings"]
        if f["code"] == "invalid-resource-path"
    )
    assert reported == _direct_invalid_resources(docs)
    expected_message = (
        "plans/bad.html: location type 'plan' conflicts with reckon-type 'research'"
    )
    assert reported == [("plans/bad.html", expected_message)]


def test_a_clean_corpus_reports_no_invalid_resource_path(tmp_path: Path) -> None:
    docs = _corpus(tmp_path, invalid=False)

    result = mcp._audit(project=PROJECT, checkout_path=str(docs.parent))

    assert _direct_invalid_resources(docs) == []
    assert [f for f in result["findings"] if f["code"] == "invalid-resource-path"] == []


@pytest.mark.parametrize("invalid", [False, True])
def test_the_audit_findings_are_unchanged_for_the_corpus(
    tmp_path: Path, invalid: bool
) -> None:
    """The shared scan changes how the audit reads, not what it reports.

    The expected rows are the ones the audit produced before the audit shared
    the scan, for this fixed corpus: alpha and beta are open and unscheduled,
    and each is held to the wiring rule, so the clean corpus carries those four
    rows and the invalid corpus adds the dropped file's own row.
    """
    docs = _corpus(tmp_path, invalid=invalid)

    result = mcp._audit(project=PROJECT, checkout_path=str(docs.parent))

    expected = [
        ("unscheduled-open-plan", "alpha", None, "info"),
        ("unscheduled-open-plan", "beta", None, "info"),
        ("unwired-plan", "alpha", None, "error"),
        ("unwired-plan", "beta", None, "error"),
    ]
    if invalid:
        expected.append(
            (
                "invalid-resource-path",
                None,
                "plans/bad.html",
                "error",
            )
        )
    assert _findings(result) == sorted(expected)
    assert result["checked"] == 2


def test_a_scope_over_two_docs_trees_keeps_their_drops_apart(tmp_path: Path) -> None:
    """One scope can span several docs trees, and each read sees its own drop.

    The resource cache is keyed by ``(docs_dir, project)`` because a read such
    as promotion resolves resources across more than one docs tree at once. The
    record of files the scan dropped as invalid must be keyed the same way: a
    single record shared by the scope would let a strict read of the second tree
    raise on the first tree's file — naming a path that tree does not contain,
    so a consumer taking ``relative_to(docs_dir)`` of it would raise too.
    """
    first = _corpus(tmp_path / "first", invalid=True, invalid_name="alpha_bad.html")
    second = _corpus(tmp_path / "second", invalid=True, invalid_name="beta_bad.html")

    with resource_scan_scope() as dropped:
        for tree in (first, second):
            assert [
                r.slug for r in iter_resources(tree, PROJECT, ignore_invalid=True)
            ] == [
                "alpha",
                "beta",
            ]
        seen = {}
        for tree in (first, second):
            with pytest.raises(ResourceCollision) as excinfo:
                iter_resources(tree, PROJECT, ignore_invalid=False)
            seen[tree] = str(excinfo.value)
        assert "plans/alpha_bad.html" in seen[first]
        assert "beta_bad.html" not in seen[first]
        assert "plans/beta_bad.html" in seen[second]
        assert "alpha_bad.html" not in seen[second]
        assert set(dropped) == {(str(first), PROJECT), (str(second), PROJECT)}
