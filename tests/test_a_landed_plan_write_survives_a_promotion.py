"""A plan write that answers as landed survives a concurrent promotion.

A section append and a promotion's landing comment meet on one plan file. The
plan measured three appends that answered ``landed: true`` and were absent from
the file afterwards while promotions wrote to the same plan, with nothing
reporting the loss.

The end point is one of two endings, whichever order the two writers arrive
in: both changes are in the file, or the later writer is refused naming the
version conflict and the file still holds the earlier change. A writer that
replaces the file from a read taken before the other writer landed is the
whole-file rewrite the negative control restores; it answers success and drops
the other change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store
from reckon.crew import promotion, promotion_scope
from tests.helpers.git import git as _git

PROJECT = "landed-write-fixture"
PLAN = "landed-write-target"
SECTION = "s2"
ANCHOR = "s2"


def _seed(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title></head>"
        '<body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Landed placeholder",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _seed(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _append_section(root: Path, *, write: bool = True):
    """Read the plan, append one authored section, and land it."""
    state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    _store.apply_ops(
        state,
        [
            {
                "op": "insert_section",
                "id": SECTION,
                "title": "Two",
                "body": "<p>Appended section.</p>",
                "effort_hours": 1.0,
                "capability": {
                    "version": "1.0",
                    "class": "general",
                    "requirements": {
                        "reasoning": "standard",
                        "verification": "strict",
                        "risk": "low",
                    },
                },
                "links": [],
            }
        ],
        False,
    )
    if not write:
        return state, version
    return _store.write_plan(PROJECT, PLAN, state, version, root, artifact_type="plan")


def _record_landing(root: Path, run_id: str) -> dict:
    return promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="§2",
        run_id=run_id,
        narrative="the section append landed",
        author="reckon-build",
        when="2026-11-20T00:00:00Z",
        root=root,
    )


def _landed(root: Path) -> tuple[list[str], list[str], int]:
    state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    sections = [str(row["id"]) for row in state.get("sections") or []]
    comments = [str(row["id"]) for row in state["comments"].get(ANCHOR, [])]
    return sections, comments, version


def test_a_promotion_that_read_before_a_section_append_keeps_it(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The promotion reads the plan, the append lands, and the promotion then
    # writes its landing comment from the state it read. Both must survive.
    original = promotion_scope._landing_comment_update
    landed: list[int] = []
    done: list[bool] = []

    def interleaved(state, **keywords):
        if not done:
            done.append(True)
            landed.append(_append_section(repository))
        return original(state, **keywords)

    monkeypatch.setattr(promotion_scope, "_landing_comment_update", interleaved)
    result = _record_landing(repository, "r-20261120T200000000001-section")

    sections, comments, version = _landed(repository)
    assert landed == [1], "the append itself did not land"
    assert result["recorded"] is True, result
    assert sections == [SECTION], f"the appended section was dropped: {sections}"
    assert comments == [str(result["comment_id"])], comments
    assert version == 2, f"version {version} does not count both landed writes"


def test_a_section_append_that_read_before_a_promotion_is_refused(
    repository: Path,
) -> None:
    # The append reads the plan, the promotion lands its comment, and the
    # append then writes from the state it read. It must be refused naming the
    # version it read, and the comment it would have overwritten must remain.
    stale, stale_version = _append_section(repository, write=False)
    result = _record_landing(repository, "r-20261120T200000000002-comment")
    assert result["recorded"] is True, result

    with pytest.raises(_store.VersionConflict) as refusal:
        _store.write_plan(
            PROJECT, PLAN, stale, stale_version, repository, artifact_type="plan"
        )
    assert refusal.value.expected == stale_version
    assert refusal.value.current == stale_version + 1
    sections, comments, version = _landed(repository)
    assert sections == [], sections
    assert comments == [str(result["comment_id"])], comments
    assert version == stale_version + 1
