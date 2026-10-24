"""A land refuses, before it merges, a head behind the run's own worktree.

``crew complete --land`` merges the promoted revision as given. When the run's
worktree is still on disk at a later commit, that merge puts an earlier revision
on the branch and pushes it, and only the release step afterwards reports that
the worktree still carries commits no reader has. The worktree is readable
before the merge, so the land compares its head with the reviewed head and
refuses when neither the same commit nor an ancestor joins them, naming both
revisions and the ``reckon crew merge`` command for the worktree's head.

The declared mutation removes the pre-merge comparison: the third-commit case
then merges and pushes the third commit, and fails.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import real_store_home, real_store_is_not_a_fixture_target
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)
from tests.real_home_traces import traces_naming

RUN_ID = "r-20261024T150000000000-a-land-behind-its-worktree"
NEEDLE_RUN_IDS = (RUN_ID,)


@pytest.fixture(autouse=True)
def real_crew_home_untouched() -> Iterator[None]:
    """Search the operator's crew home for this run id before and after a case."""
    home = real_store_home()
    before = traces_naming(home, ("crew/live", "crew/watch"), NEEDLE_RUN_IDS)
    yield
    assert traces_naming(home, ("crew/live", "crew/watch"), NEEDLE_RUN_IDS) == before


def _carries(repository: Path, revision_and_path: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repository), "cat-file", "-e", revision_and_path],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _commit(repository: Path, name: str, text: str) -> str:
    (repository / name).write_text(text, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {text.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository whose run worktree sits at the fourth of four commits.

    The seed commit is the branch's only commit, so a land of the third commit
    would fast-forward the branch by two commits that only the worktree carries.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    real_store_is_not_a_fixture_target(config_home)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    base = _git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "--detach", str(worktree), base)
    second = _commit(worktree, "step2.txt", "second\n")
    third = _commit(worktree, "step3.txt", "third\n")
    fourth = _commit(worktree, "step4.txt", "fourth\n")
    assert _git(worktree, "rev-parse", "HEAD") == fourth
    gate_log = tmp_path / "gate.log"
    gate_log.write_text(
        f"# revision: {fourth}\n# tree: {worktree}\n# command: true\nEXIT=0\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifests" / (RUN_ID + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-10-24T15:00:00Z",
            "base_sha": base,
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": "plan-a",
                "section": "s65",
                "write_paths": ["step2.txt", "step3.txt", "step4.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "base": base,
        "second": second,
        "third": third,
        "fourth": fourth,
        "gate_log": gate_log,
    }


def _run(fixture: dict, head: str) -> dict:
    return {
        "run_id": RUN_ID,
        "repository": fixture["repository"],
        "head": head,
        "gate_log": fixture["gate_log"],
    }


def test_a_head_behind_the_worktree_is_refused_before_merging(chain: dict) -> None:
    result = _complete_land(_run(chain, chain["third"]))

    assert result.exit_code != 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is False, report
    assert report["steps"]["promote"]["ok"] is True, report
    land = report["steps"]["land"]
    assert land["ok"] is False, land
    assert chain["third"] in land["error"], land
    assert chain["fourth"] in land["error"], land
    assert "reckon crew merge" in land["error"], land
    assert land["resume"].startswith("reckon crew merge"), land
    assert chain["fourth"] in land["resume"], land
    # Nothing was pushed and the merge never reached the branch. Promotion
    # commits its own ledger row before the land is attempted, so the remote is
    # the receipt for the merge: it still names the seed commit.
    assert _git(chain["origin"], "rev-parse", "main") == chain["base"]
    assert not _carries(chain["repository"], "main:step3.txt")
    assert chain["worktree"].is_dir()


def test_a_head_equal_to_the_worktree_merges_as_today(chain: dict) -> None:
    result = _complete_land(_run(chain, chain["fourth"]))

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True, report
    assert report["steps"]["land"]["ok"] is True, report["steps"]["land"]
    assert _git(chain["origin"], "show", "main:step4.txt") == "fourth"


def test_a_released_worktree_merges_as_today(chain: dict) -> None:
    """With the tree gone there is nothing to compare, so the merge proceeds.

    The overall status stays what a land has always returned for a run whose
    worktree is no longer on disk: the release step withholds a tree it cannot
    find. That is the release's verdict, not this guard's, so the merge is
    asserted on the land step and on the remote.
    """
    _git(chain["repository"], "worktree", "remove", "--force", str(chain["worktree"]))
    assert not chain["worktree"].exists()

    result = _complete_land(_run(chain, chain["third"]))

    report = json.loads(result.output)
    land = report["steps"]["land"]
    assert land["ok"] is True, land
    assert land["pushed"] is True, land
    assert "worktree_head" not in land, land
    assert _git(chain["origin"], "show", "main:step3.txt") == "third"
    assert report["steps"]["release"]["worktree_released"] is False, report["steps"][
        "release"
    ]
    assert result.exit_code == 1, result.output
