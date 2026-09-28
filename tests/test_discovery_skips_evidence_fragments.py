"""Typed discovery passes over a plan's evidence fragments.

A node writes its landing record as its own fragment under
``docs/evidence/fragments/<plan>/<node>.html`` and the fragment is composed into
that plan's cumulative record at read time. The fragment is not a typed resource
of its own, so a full-tree walk must pass over the subtree rather than refuse a
path shape the typed roots reserve for their own documents.

Three cases stand against the one skip: every read of a plan, including the plan
the fragment does not belong to, succeeds because the walk behind it does not
refuse; the fragment stays readable by the composer, so the composed record
carries its anchor; and a document outside the fragment subtree that is still
mis-shaped is refused exactly as before, so the skip is narrower than "any
nested path under a typed root".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.evidence import compose_landed_record
from reckon.mcp import _read_plan_tool
from reckon.resources import ResourceCollision, iter_resources, resource_map

PROJECT = "sample"
OWNER_PLAN = "owner-plan"
UNRELATED_PLAN = "unrelated-plan"
NODE = "node"
FRAGMENT_ANCHOR = "fragment-anchor"


def _plan(path: Path, project: str, slug: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        f"<title>{slug}</title></head><body><main><h1>{slug}</h1></main></body></html>"
    )
    return path


def _record(path: Path, project: str, plan: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="reckon-type" content="evidence">'
        f'<meta name="plan-slug" content="{plan}-landed">'
        f'<meta name="plan-evidence-for" content="{plan}">'
        "</head><body><main><h1>Record</h1></main></body></html>"
    )
    return path


def _fragment(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<article class="landed-fragment" data-node="{NODE}">'
        f"{FRAGMENT_ANCHOR}</article>\n"
    )
    return path


def _foreign_evidence(path: Path) -> Path:
    """A document under a typed root that is neither canonical nor a fragment."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="evidence">'
        '<meta name="plan-slug" content="stray">'
        "</head><body><main><h1>Stray</h1></main></body></html>"
    )
    return path


def _mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docs: Path) -> None:
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state))
    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two plans, one cumulative record and one fragment for the owner plan."""

    root = tmp_path / "repo"
    docs = root / "docs"
    _plan(docs / "plans" / f"{OWNER_PLAN}.html", PROJECT, OWNER_PLAN)
    _plan(docs / "plans" / f"{UNRELATED_PLAN}.html", PROJECT, UNRELATED_PLAN)
    _record(
        docs / "evidence" / "archive" / f"{OWNER_PLAN}-landed.html", PROJECT, OWNER_PLAN
    )
    _fragment(docs / "evidence" / "fragments" / OWNER_PLAN / f"{NODE}.html")
    _mount(tmp_path, monkeypatch, docs)
    return root


def _fragment_path(repository: Path) -> Path:
    return repository / "docs" / "evidence" / "fragments" / OWNER_PLAN / f"{NODE}.html"


def test_read_plan_of_the_unrelated_plan_is_not_refused(repository: Path) -> None:
    """The plan the fragment does not belong to reads, and its walk does not refuse.

    The read resolves through a full-tree walk. Reading the plan the fragment
    does not belong to is the case that fails when the fragment subtree is not
    skipped: the walk then refuses ``evidence/fragments/<plan>/<node>.html`` as
    a mis-shaped typed resource.
    """

    assert _fragment_path(repository).is_file()
    docs = repository / "docs"

    owner = _read_plan_tool(
        project=PROJECT, slug=OWNER_PLAN, checkout_path=str(repository), view="raw"
    )
    assert owner["data"]["slug"] == OWNER_PLAN
    assert owner["data"]["type"] == "plan"

    unrelated = _read_plan_tool(
        project=PROJECT,
        slug=UNRELATED_PLAN,
        checkout_path=str(repository),
        view="raw",
    )
    assert unrelated["data"]["slug"] == UNRELATED_PLAN
    assert unrelated["data"]["type"] == "plan"

    # The walk the read resolves through, with no tolerant flag of its own: a
    # fragment must not be refused and both plans must still be discovered.
    slugs = {resource.slug for resource in iter_resources(docs, PROJECT)}
    assert {OWNER_PLAN, UNRELATED_PLAN} <= slugs


def test_the_fragment_is_walked_past_rather_than_discovered(repository: Path) -> None:
    """The fragment is not a resource: discovery neither refuses nor yields it."""

    docs = repository / "docs"
    discovered = {
        str(resource.relative_path) for resource in iter_resources(docs, PROJECT)
    }

    assert str(_fragment_path(repository).relative_to(docs)) not in discovered
    # And the tolerant map agrees, keyed the way a caller would look it up.
    indexed = resource_map(docs, PROJECT)
    assert all(key[0] != "evidence" or key[1] != NODE for key in indexed)


def test_the_composed_record_still_carries_the_fragment_anchor(
    repository: Path,
) -> None:
    """The skip keeps discovery away without hiding the fragment from its reader."""

    record = repository / "docs" / "evidence" / "archive" / f"{OWNER_PLAN}-landed.html"
    composed = compose_landed_record(record, OWNER_PLAN, project=PROJECT)

    assert composed.startswith(record.read_bytes())
    assert FRAGMENT_ANCHOR in composed.decode()


def test_a_mis_shaped_document_outside_the_fragment_subtree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip is the fragment subtree, not every nested path under a typed root."""

    root = tmp_path / "repo"
    docs = root / "docs"
    _plan(docs / "plans" / f"{OWNER_PLAN}.html", PROJECT, OWNER_PLAN)
    stray = _foreign_evidence(docs / "evidence" / "extra" / "x.html")
    _mount(tmp_path, monkeypatch, docs)

    with pytest.raises(ResourceCollision, match=r"evidence/extra/x\.html"):
        iter_resources(docs, PROJECT)
    assert stray.is_file()
