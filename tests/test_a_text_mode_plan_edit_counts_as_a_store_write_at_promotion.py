"""Promotion admits store text edits without claiming the editor's prose."""

from pathlib import Path

import pytest

from reckon import _store
from reckon.crew.node import CrewError
from reckon.mcp_edit_plan import _edit_plan
from tests import test_landing_commit_carries_only_its_own_comment as landing

repository = landing.repository

ORIGINAL = '<p id="intro">The introduction.</p>'
REVISED = '<p id="intro">The revised introduction.</p>'


def _seed_prose(repository: Path) -> Path:
    path = repository / landing.PLAN_RELATIVE
    path.write_text(path.read_text().replace("</main>", ORIGINAL + "</main>"))
    landing._git(repository, "add", "--", landing.PLAN_RELATIVE)
    landing._git(repository, "commit", "-q", "-m", "test: seed authored prose")
    return path


@pytest.mark.parametrize("mode", ["state", "text"])
def test_a_store_edit_allows_promotion(repository: Path, mode: str) -> None:
    path = _seed_prose(repository)
    _, version = _store.read_plan(landing.PROJECT, landing.PLAN, repository)
    arguments = {"old_html": ORIGINAL, "new_html": REVISED} if mode == "text" else {}
    edited = _edit_plan(
        landing.PROJECT,
        landing.PLAN,
        None if mode == "text" else [{"op": "set", "path": "owner", "value": "Editor"}],
        version,
        checkout_path=str(repository),
        doc_type="plan",
        mode=mode,
        **arguments,
    )
    assert edited["ok"] is True, edited
    assert landing._dirty(repository, path), "the edit must remain uncommitted"
    landing._pointer(repository)

    result = landing._promote(repository, "the completed work is recorded")

    assert result["plan_comment"]["recorded"] is True
    assert landing.RUN_ID in path.read_text()
    committed = landing._git(repository, "show", f"HEAD:{landing.PLAN_RELATIVE}")
    assert landing.RUN_ID in committed, "the promotion comment must be committed"
    if mode == "text":
        assert REVISED in path.read_text()
        assert ORIGINAL in committed, (
            "promotion must leave the editor's prose uncommitted"
        )
        assert REVISED not in committed
        assert landing._dirty(repository, path)


def test_an_unrecorded_prose_edit_still_refuses_promotion(repository: Path) -> None:
    path = _seed_prose(repository)
    path.write_text(path.read_text().replace(ORIGINAL, REVISED))
    before = path.read_bytes()
    landing._pointer(repository)

    with pytest.raises(CrewError, match="authored content outside"):
        landing._promote(repository, "an unrecorded edit must refuse")

    assert path.read_bytes() == before
    assert landing.RUN_ID not in landing._git(
        repository, "show", f"HEAD:{landing.PLAN_RELATIVE}"
    )
