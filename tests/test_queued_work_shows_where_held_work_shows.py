"""One queued request, read where the surfaces already report held work.

A request waiting for a lane slot is not missing work: its pointer exists, it
is being waited on. These cases read one queued pointer from the same live
pointer through every surface that reports held work — the follower's waiting
count, the obligations view, the live view and the run list — and assert that a
request whose pointer is gone reads nowhere, which is the declared negative
control.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

PROJECT = "proj"
SESSION = "coordinator-a"
LANE = "clive"


def _git(repository, *arguments):
    subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)


def _mounted_project(tmp_path, monkeypatch):
    """A temporary crew home over a real git-backed mounted project."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    quote = '"'
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>",
                f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>",
                f"<meta name={quote}plan-slug{quote} content={quote}plan-a{quote}>",
                f"<h2 id={quote}dispatch{quote}>Dispatch</h2>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _queue(repository, run_id, *, session=SESSION, lane=LANE, queued_at):
    """Queue one request through the writer every dispatch uses."""
    from reckon.crew import runs
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id=f"node-{run_id}",
        goal="prove the surfaces agree",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports passed",
        write_paths=[f"{run_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = queued_at.isoformat()
    return runs.queue_dispatch(
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "repo": str(repository),
            "node": node.as_dict(),
            "backend": lane,
            "launch": "cli",
            "local": True,
            "phase": "queued",
            "queued_at": joined,
            "created_at": joined,
            "hold": {"held": True},
        }
    )


def _waiting_count():
    """The bucket the follower's q column counts, over the live pointers."""
    from reckon.crew import runs
    from reckon.crew.recovery_watch import _fleet_counts, _watch_snapshot

    pointers = list(runs._list_live_records(project=PROJECT))
    snapshots = {
        str(pointer["run_id"]): _watch_snapshot(
            pointer, moment=datetime.now(tz=UTC).timestamp(), stall_seconds=3600
        )
        for pointer in pointers
    }
    return _fleet_counts(snapshots, session=SESSION)


def _obligation_items():
    from reckon.crew import obligations

    return obligations.obligations(PROJECT, SESSION)["obligations"]


def _crew_view_rows():
    from reckon import cli

    result = CliRunner().invoke(
        cli.main,
        ["crew", "view", "live", "--project", PROJECT, "--session", SESSION],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["runs"]


def _crew_list_rows():
    from reckon import cli

    result = CliRunner().invoke(cli.main, ["crew", "list", "--project", PROJECT])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["runs"]


def _row_for(rows, run_id):
    return next(row for row in rows if str(row.get("run_id") or "") == run_id)


def test_one_queued_run_reads_as_queued_on_every_surface(tmp_path, monkeypatch):
    from reckon.crew import obligations as obligations_module
    from reckon.crew import runs, ticker

    repository = _mounted_project(tmp_path, monkeypatch)
    now = datetime.now(tz=UTC)
    _queue(repository, "r-queued-one", queued_at=now)

    counts = _waiting_count()
    assert counts.get("waiting", 0) == 1
    # The follower's q column is this bucket, read through the letter table the
    # column renders with; the counts above are the reading it prints.
    assert ticker.STAT_LETTER["waiting"] == "q"

    items = _obligation_items()
    item = _row_for(items, "r-queued-one")
    assert item["kind"] == obligations_module.QUEUED_RUN_DUTY_KIND
    assert (item["position"], item["rank"]) == (1, 1)

    row = _row_for(_crew_view_rows(), "r-queued-one")
    assert row["classification"] == "queued"
    assert (row["position"], row["rank"]) == (1, 1)

    # Declared negative control: remove the queued pointer, and every surface
    # reads no queued run.
    runs.pointer_path("r-queued-one").unlink()
    assert _waiting_count().get("waiting", 0) == 0
    assert _obligation_items() == []
    assert _crew_view_rows() == []
    assert _crew_list_rows() == []


@pytest.mark.xfail(
    reason=(
        "reckon/crew_run_commands.py, where crew list composes its rows, is "
        "claimed by another session's run cso-ledger-and-velocity-retire and "
        "cannot carry the place yet; it needs the two keys added to its row "
        "dict, read from queue_admission.queued_placements(project_records). "
        "The change is named in the manifest's follow_ons."
    ),
    strict=False,
)
def test_crew_list_names_the_queued_runs_place(tmp_path, monkeypatch):
    repository = _mounted_project(tmp_path, monkeypatch)
    _queue(repository, "r-queued-nine", queued_at=datetime.now(tz=UTC))
    row = _row_for(_crew_list_rows(), "r-queued-nine")
    assert (row.get("position"), row.get("rank")) == (1, 1)


def test_two_queued_runs_follow_the_admission_order(tmp_path, monkeypatch):
    repository = _mounted_project(tmp_path, monkeypatch)
    joined = datetime.now(tz=UTC)
    _queue(repository, "r-queued-a", queued_at=joined - timedelta(minutes=2))
    dispatched_position = _queue(
        repository, "r-queued-b", queued_at=joined - timedelta(minutes=1)
    )[1]
    # The writer that queued the second request reports the place its own
    # admission ordering gives it: the surfaces must agree with it.
    assert dispatched_position == 2

    assert {
        item["run_id"]: (item["position"], item["rank"]) for item in _obligation_items()
    } == {"r-queued-a": (1, 1), "r-queued-b": (2, 2)}

    rows = {row["run_id"]: (row["position"], row["rank"]) for row in _crew_view_rows()}
    assert rows == {"r-queued-a": (1, 1), "r-queued-b": (2, 2)}
