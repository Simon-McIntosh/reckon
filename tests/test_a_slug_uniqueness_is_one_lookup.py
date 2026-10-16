"""A slug's uniqueness is one lookup into its root's index, not a scan per sibling.

A named resolve asks whether another file in the same typed root claims the same
identity. Identifying every sibling on each of a read's many resolves
re-identified the whole plan population per resolve, so the check reads one
identity index of the directory instead — held across the read's resolves and
revalidated against the directory listing and each sibling's stamp. The
properties under test are the count of identifications a read performs, the
collision refusal the check exists to make, and the rebuild the index promises
when a sibling moves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import resources
from reckon.resources import ResourceCollision, resolve_resource, resource_scan_scope

PROJECT = "sample"
PLAN_COUNT = 24
RESOLVE_COUNT = 20


def _plan(root: Path, relative: str, *, slug: str, archived: bool = False) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-archived" content="{"1" if archived else "0"}">'
        f"<title>{slug}</title></head><body><main><h1>{slug}</h1></main></body></html>"
    )
    return path


@pytest.fixture(autouse=True)
def _cold_index() -> None:
    """Drop the held identity indexes so each test builds its own."""
    resources._INDEX_MEMO.clear()


@pytest.fixture
def docs_tree(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    for number in range(PLAN_COUNT):
        _plan(docs, f"plans/{_name(number)}.html", slug=_name(number))
    return docs


def _name(number: int) -> str:
    return f"plan-{number:02d}"


def test_one_read_identifies_each_sibling_once(
    docs_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real = resources.identify_resource

    def counting(docs_dir: Path, path: Path, project: str):
        calls.append(path)
        return real(docs_dir, path, project)

    monkeypatch.setattr(resources, "identify_resource", counting)

    with resource_scan_scope():
        for number in range(RESOLVE_COUNT):
            found = resolve_resource(docs_tree, PROJECT, _name(number))
            assert found is not None and found.slug == _name(number)

    # One index of the plan root answers every resolve in the read, so the
    # identifications are bounded by the plans themselves — not by the plans
    # times the resolves.
    assert len(calls) <= PLAN_COUNT, calls
    assert len(calls) < RESOLVE_COUNT * PLAN_COUNT, calls


def test_a_second_file_claiming_a_slug_is_refused_naming_both(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    _plan(docs, "plans/alpha.html", slug="alpha")
    _plan(docs, "plans/alpha-second.html", slug="alpha")

    with pytest.raises(ResourceCollision) as refused:
        resolve_resource(docs, PROJECT, "alpha")

    message = str(refused.value)
    assert "plans/alpha.html" in message, message
    assert "plans/alpha-second.html" in message, message


def test_the_index_rebuilds_when_a_sibling_claims_the_slug(
    docs_tree: Path,
) -> None:
    # Warm the held index with a slug that is unique when it is built.
    assert resolve_resource(docs_tree, PROJECT, "plan-01") is not None

    # A sibling that now claims the same slug must be seen by the next resolve,
    # which means the index was rebuilt from the moved listing rather than
    # answering from the entry the earlier resolve left. The added file changes
    # the directory listing, so the move is caught by the listing signature.
    _plan(docs_tree, "plans/late-duplicate.html", slug="plan-01")

    with pytest.raises(ResourceCollision):
        resolve_resource(docs_tree, PROJECT, "plan-01")
