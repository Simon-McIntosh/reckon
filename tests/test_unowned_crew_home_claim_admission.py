"""Shared delivery roots cannot fence admission; owned leaves still do."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from tests import test_each_admission_pass_records_its_verdict as admission_fixtures

admit_repo = admission_fixtures.admit_repo


def _admit(repository: Path, tmp_path: Path, holder_path: Path, write_path: Path):
    holder = admission_fixtures._queue(repository)
    holder.update(
        run_id="r-holder", phase="working", pid=os.getpid(), worktree=str(repository)
    )
    holder["node"].update(id="holder", write_paths=[str(holder_path)])
    runs._write_json(runs.pointer_path("r-holder"), holder)
    request = admission_fixtures._queue(repository)
    request["node"]["write_paths"] = [str(write_path)]
    runs.queue_dispatch(request)
    lane = tmp_path / "lane.json"
    lane.write_text(json.dumps(admission_fixtures._session_lane(2)))
    _, config_for = admission_fixtures._config_with_lane(lane)
    launched = []

    def launch(plan, *, log_path, stderr_path, prompt_path):
        launched.append(plan)
        return os.getpid()

    result = queue_admission.admit_once(config_for=config_for, launcher=launch)
    verdict = next(
        row for row in result["verdicts"] if row["run_id"] == request["run_id"]
    )
    print(json.dumps(verdict, sort_keys=True))
    return result, verdict, launched


@pytest.mark.parametrize("root", ["home", "runs", "reports", "reviews"])
def test_an_unowned_delivery_root_does_not_hold_admission(admit_repo, tmp_path, root):
    from reckon.crew.review import review_store_root

    home = runs.crew_home()
    assert not home.is_relative_to(admit_repo)
    shared = {
        "home": home,
        "runs": runs.runs_dir(),
        "reports": runs.reports_dir(),
        "reviews": review_store_root(),
    }
    result, verdict, launched = _admit(
        admit_repo,
        tmp_path,
        shared[root],
        home / "runs" / "r-target" / "receipt.json"
        if root in {"home", "runs"}
        else shared[root] / "receipt.json",
    )
    assert verdict["verdict"] == "launched", verdict
    assert result["launched"] == [admission_fixtures.RUN_ID]
    assert len(launched) == 1
    assert runs.read_pointer(admission_fixtures.RUN_ID)["phase"] != "queued"


def test_an_owned_repository_path_holds_admission(admit_repo, tmp_path):
    path = admit_repo / "docs" / "plans" / "plan-a.html"
    _assert_held(admit_repo, tmp_path, path, path)


def test_an_owned_crew_home_file_holds_admission(admit_repo, tmp_path):
    path = runs.crew_home() / "runs" / "r-holder" / "receipt.json"
    _assert_held(admit_repo, tmp_path, path, path)


def test_an_owned_crew_home_directory_holds_admission(admit_repo, tmp_path):
    path = runs.crew_home() / "runs" / "r-holder"
    _assert_held(admit_repo, tmp_path, path, path / "receipt.json")


def _assert_held(repository, tmp_path, holder_path, write_path):
    result, verdict, launched = _admit(repository, tmp_path, holder_path, write_path)
    assert verdict["verdict"] == "held-on-claim", verdict
    assert "r-holder" in verdict["detail"]
    named_path = (
        write_path.relative_to(repository)
        if write_path.is_relative_to(repository)
        else write_path
    )
    assert str(named_path) in verdict["detail"]
    assert result["held"] == [admission_fixtures.RUN_ID]
    assert launched == []
    assert runs.read_pointer(admission_fixtures.RUN_ID)["phase"] == "queued"
