"""Resource identification reuses one parse of byte-identical content.

A promotion resolves resources across several docs trees — the main checkout,
the run's worktree and the tip tree — each holding a copy of the same
documents. The metadata parse is memoised per file path, so identical bytes in
a second tree were parsed again, and the parsed read dominates the cost of the
whole promotion. Keying the parse on the file's content digest lets the trees
share one parse; a file whose bytes changed is parsed afresh.

The negative control keys the same reuse by path again, under which the
cross-tree case parses identical content once per tree and this file's
``read_state`` count fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reckon.resources as resources_module
from reckon import _plan_html, file_memo, metadata_index
from reckon.resources import ResourceCollision, identify_resource

#: A plan document carrying an opted-in section record: its presence makes
#: ``parse_meta`` hand the document to the parsed ``read_state`` read, which is
#: the cost this node's change shares across trees.
_PLAN_DOC = """<!doctype html><html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="{type}">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<title>{title}</title>
</head><body>
<h2 id="s1">Section one</h2>
<section data-reckon="section" data-id="s1" data-status="implementable" data-effort-hours="2.0" data-attempts="0"></section>
</body></html>
"""


@pytest.fixture(autouse=True)
def _isolated_caches():
    """Both memos are process-wide; each test starts from an empty pair so a
    parse from a neighbouring test never stands in for its own."""
    file_memo.clear()
    metadata_index.clear()
    yield
    file_memo.clear()
    metadata_index.clear()


def _write_plan(
    docs: Path, slug: str, title: str, *, artifact_type: str = "plan"
) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _PLAN_DOC.format(type=artifact_type, slug=slug, title=title),
        encoding="utf-8",
    )
    return path


def _counting_read_state(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Count the parsed reads ``parse_meta`` performs, without changing them."""
    calls = {"count": 0}
    real = _plan_html.read_state

    def counting(text: str) -> dict:
        calls["count"] += 1
        return real(text)

    monkeypatch.setattr(_plan_html, "read_state", counting)
    return calls


def test_identical_content_parses_once_across_trees(tmp_path, monkeypatch):
    tree_a = tmp_path / "a" / "docs"
    tree_b = tmp_path / "b" / "docs"
    tree_c = tmp_path / "c" / "docs"
    path_a = _write_plan(tree_a, "alpha", "Alpha")
    path_b = _write_plan(tree_b, "alpha", "Alpha")  # byte-identical to path_a
    path_c = _write_plan(tree_c, "alpha", "Alpha changed")  # one changed file
    assert metadata_index._content_digest(path_a) == metadata_index._content_digest(
        path_b
    )
    assert metadata_index._content_digest(path_a) != metadata_index._content_digest(
        path_c
    )

    calls = _counting_read_state(monkeypatch)
    a = identify_resource(tree_a, path_a, "proj")
    b = identify_resource(tree_b, path_b, "proj")
    c = identify_resource(tree_c, path_c, "proj")

    # Identity is unchanged: each tree's copy still resolves to the same typed
    # resource, and each resource still names the file it was asked about.
    assert a is not None and b is not None and c is not None
    assert a.identity.key == b.identity.key == c.identity.key == "proj:plan:alpha"
    assert (a.path, b.path, c.path) == (path_a, path_b, path_c)
    # Two distinct contents, so two parsed reads: identical bytes once across
    # the two trees, and the changed file once on its own.
    assert calls["count"] == 2


def test_changed_bytes_are_parsed_again(tmp_path, monkeypatch):
    """A rewrite of one tree's copy does not reuse the other tree's parse."""
    tree_a = tmp_path / "a" / "docs"
    tree_b = tmp_path / "b" / "docs"
    path_a = _write_plan(tree_a, "alpha", "Alpha")
    path_b = _write_plan(tree_b, "alpha", "Alpha")
    calls = _counting_read_state(monkeypatch)

    identify_resource(tree_a, path_a, "proj")
    identify_resource(tree_b, path_b, "proj")
    # The second tree reused the first parse; now change its bytes.
    path_b.write_text(
        _PLAN_DOC.format(type="plan", slug="alpha", title="Alpha edited"),
        encoding="utf-8",
    )
    identify_resource(tree_b, path_b, "proj")
    assert calls["count"] == 2


def test_type_conflict_still_raises_across_trees(tmp_path, monkeypatch):
    """Sharing a parse does not weaken the location/type collision check."""
    _counting_read_state(monkeypatch)
    tree_a = tmp_path / "a" / "docs"
    tree_b = tmp_path / "b" / "docs"
    path_a = _write_plan(tree_a, "alpha", "Alpha", artifact_type="research")
    path_b = _write_plan(tree_b, "alpha", "Alpha", artifact_type="research")

    with pytest.raises(ResourceCollision):
        identify_resource(tree_a, path_a, "proj")
    with pytest.raises(ResourceCollision):
        identify_resource(tree_b, path_b, "proj")


def test_parse_memo_is_keyed_on_content_not_path(tmp_path, monkeypatch):
    """The shared parse answers a second path from the first path's bytes."""
    calls = _counting_read_state(monkeypatch)
    tree_a = tmp_path / "a" / "docs"
    tree_b = tmp_path / "b" / "docs"
    path_a = _write_plan(tree_a, "alpha", "Alpha")
    path_b = _write_plan(tree_b, "alpha", "Alpha")

    first = metadata_index.parse_meta_shared(path_a)
    second = metadata_index.parse_meta_shared(path_b)
    assert first == second
    assert calls["count"] == 1
    # A caller mutating its copy cannot poison the entry the next tree reads.
    first["slug"] = "poisoned"
    assert metadata_index.parse_meta_shared(path_b)["slug"] == "alpha"


def test_identical_content_bounds_the_path_walk(tmp_path, monkeypatch):
    """A resource walk over two identical trees parses each content once."""
    calls = _counting_read_state(monkeypatch)
    tree_a = tmp_path / "a" / "docs"
    tree_b = tmp_path / "b" / "docs"
    for index in range(4):
        _write_plan(tree_a, f"plan-{index}", f"Plan {index}")
        _write_plan(tree_b, f"plan-{index}", f"Plan {index}")

    found_a = resources_module.iter_resources(tree_a, "proj")
    found_b = resources_module.iter_resources(tree_b, "proj")
    assert {r.slug for r in found_a} == {f"plan-{i}" for i in range(4)}
    assert {r.slug for r in found_b} == {f"plan-{i}" for i in range(4)}
    # Four distinct contents across both trees: four parsed reads, not eight.
    assert calls["count"] == 4
