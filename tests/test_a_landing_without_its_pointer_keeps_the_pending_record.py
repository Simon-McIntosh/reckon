"""A missing live pointer cannot erase a landing's durable identity."""

import json

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import merge, pending_landing, runs


@pytest.mark.parametrize("wait", [True, False], ids=["synchronous", "queued"])
def test_a_missing_pointer_refuses_without_overwriting_the_record(
    tmp_path, monkeypatch, wait
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    run_id = "r-missing-pointer"
    head = "a" * 40
    pending_landing.write_pending(
        run_id, "proj", pointer={"run_id": run_id, "project": "proj"}, head=head
    )
    record = pending_landing.pending_dir() / f"{run_id}.json"
    before = record.read_bytes()
    assert json.loads(before)["head"] == head
    assert not runs.pointer_path(run_id).exists()
    files_before = sorted(runs.crew_home().rglob("*"))

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--land",
            *(["--wait"] if wait else []),
        ],
    )

    assert result.exit_code != 0, result.output
    assert record.read_bytes() == before
    assert "live pointer" in result.output
    assert str(record) in result.output
    assert sorted(runs.crew_home().rglob("*")) == files_before

    monkeypatch.setattr(
        pending_landing,
        "_promoted_row",
        lambda project, requested: (
            {"promoted_revision": head}
            if (project, requested) == ("proj", run_id)
            else None
        ),
    )

    def unavailable_landing(repository, *, head, **kwargs):
        assert head == json.loads(before)["head"]
        raise merge.MergeError("temporary landing is unavailable")

    monkeypatch.setattr(merge, "project_repository", lambda project: tmp_path)
    rows = pending_landing.finish_pending_landings(
        only_run=run_id, land=unavailable_landing
    )
    assert len(rows) == 1
    assert rows[0]["run_id"] == run_id
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "temporary landing is unavailable"


def test_an_empty_project_writes_no_record(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))

    pending_landing.write_pending("r-empty-project", "")

    assert not pending_landing.pending_dir().exists()


def test_an_empty_project_preserves_an_existing_record(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    run_id = "r-recorded-landing"
    pending_landing.write_pending(run_id, "proj", head="a" * 40)
    record = pending_landing.pending_dir() / f"{run_id}.json"
    before = record.read_bytes()

    pending_landing.write_pending(run_id, "", pointer={}, head="")

    assert record.read_bytes() == before
