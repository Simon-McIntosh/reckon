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


def _edit_text(repository: Path, old: str, revised: str) -> None:
    _, version = _store.read_plan(landing.PROJECT, landing.PLAN, repository)
    result = _edit_plan(
        landing.PROJECT,
        landing.PLAN,
        None,
        version,
        checkout_path=str(repository),
        doc_type="plan",
        mode="text",
        old_html=old,
        new_html=revised,
    )
    assert result["ok"] is True, result


@pytest.mark.parametrize("revised", ["", "<p>A repeated paragraph.</p>"])
def test_deletion_and_duplicate_prose_preserve_bookkeeping(
    repository: Path,
    revised: str,
) -> None:
    path = _seed_prose(repository)
    path.write_text(
        path.read_text().replace(
            "</main>",
            '<div data-status="active"><p>A repeated paragraph.</p></div></main>',
        )
    )
    landing._git(repository, "add", "--", landing.PLAN_RELATIVE)
    landing._git(repository, "commit", "-q", "-m", "test: seed a bookkeeping attribute")
    _edit_text(repository, ORIGINAL, revised)
    path.write_text(
        path.read_text().replace('data-status="active"', 'data-status="done"')
    )
    landing._pointer(repository)

    result = landing._promote(repository, "the bookkeeping survives the text edit")

    committed = landing._git(repository, "show", f"HEAD:{landing.PLAN_RELATIVE}")
    assert ORIGINAL in committed
    assert 'data-status="done"' in committed
    assert result["landing_check"]["ok"] is True
    assert landing.RUN_ID in committed
    assert ORIGINAL not in path.read_text()


@pytest.mark.parametrize("when", ["before", "after"])
def test_a_text_write_does_not_license_unrecorded_prose(
    repository: Path,
    when: str,
) -> None:
    path = _seed_prose(repository)

    def tamper() -> None:
        path.write_text(
            path.read_text().replace("</main>", "<p>Unrecorded.</p></main>")
        )

    if when == "before":
        tamper()
    _edit_text(repository, ORIGINAL, REVISED)
    if when == "after":
        tamper()
    before = path.read_bytes()
    landing._pointer(repository)

    with pytest.raises(CrewError, match="authored content outside"):
        landing._promote(repository, "an unrelated change still refuses")

    assert path.read_bytes() == before


def test_receipts_follow_text_edits_across_state_writes_and_author_commits(
    repository: Path,
) -> None:
    path = _seed_prose(repository)
    _edit_text(repository, ORIGINAL, REVISED)
    state, version = _store.read_plan(landing.PROJECT, landing.PLAN, repository)
    _store.write_plan(
        landing.PROJECT, landing.PLAN, {**state, "owner": "Editor"}, version, repository
    )
    next_prose = '<p id="intro">The continued introduction.</p>'
    _edit_text(repository, REVISED, next_prose)
    landing._pointer(repository)
    landing._promote(repository, "the text write chain is admitted")
    committed = landing._git(repository, "show", f"HEAD:{landing.PLAN_RELATIVE}")
    assert ORIGINAL in committed
    assert 'name="plan-owner" content="Editor"' in committed
    assert next_prose in path.read_text()
    landing._git(repository, "add", "--", landing.PLAN_RELATIVE)
    landing._git(
        repository, "commit", "-q", "-m", "docs: commit the edited introduction"
    )
    _edit_text(repository, next_prose, REVISED)
    landing._pointer(repository, landing.RUN_ID_SECOND)
    result = landing._promote(
        repository, "another text edit follows the author commit", landing.RUN_ID_SECOND
    )
    assert result["plan_comment"]["recorded"] is True
    assert next_prose in landing._git(
        repository, "show", f"HEAD:{landing.PLAN_RELATIVE}"
    )
    assert REVISED in path.read_text()


def test_a_text_landing_preserves_unrelated_staged_content(repository: Path) -> None:
    path = _seed_prose(repository)
    unrelated = repository / "notes.txt"
    unrelated.write_text("Another editor's staged notes.\n")
    landing._git(repository, "add", "--", "notes.txt")
    _edit_text(repository, ORIGINAL, REVISED)
    landing._pointer(repository)

    result = landing._promote(repository, "only promotion paths are committed")

    assert result["landing_check"]["ok"] is True
    assert landing._git(repository, "diff", "--cached", "--name-only") == "notes.txt"
    assert "notes.txt" not in landing._promotion_commit_files(repository)
    assert REVISED in path.read_text()


def test_a_failed_text_landing_preserves_the_edit_and_removes_its_comment(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reckon.crew import merge

    path = _seed_prose(repository)
    _edit_text(repository, ORIGINAL, REVISED)
    before = path.read_bytes()
    landing._pointer(repository)

    def refuse_commit(*_args, **_kwargs):
        raise merge.MergeError("the fixture refuses the commit")

    monkeypatch.setattr(merge, "_commit_tree", refuse_commit)
    with pytest.raises(CrewError, match="could not commit the landing"):
        landing._promote(repository, "this comment must be rolled back")

    assert path.read_bytes() == before
    assert landing._dirty(repository, path)
    assert landing.RUN_ID not in path.read_text()
