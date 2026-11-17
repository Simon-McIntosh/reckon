"""Compatible deliveries share a proved candidate and the ordinary publisher."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew_run_commands as commands
from reckon import ledger
from reckon.crew import (
    landing_measure,
    merge,
    pending_landing,
    promotion_requests,
    queue_landing,
    runs,
)
from tests.helpers.git import git

PROJECT = "group-fixture"
SOURCE = "def alpha():\n    return 0\n\n\ndef beta():\n    return 0\n\n\ndef gamma():\n    return 0\n"
GATE = "from sample import alpha, beta, gamma\n\ndef test_values():\n    assert min(alpha(), beta(), gamma()) >= 0\n\ndef test_interaction():\n    assert not (alpha() == 1 and beta() == 9)\n"


def _commit(tree, subject):
    git(tree, "add", "sample.py")
    git(
        tree,
        "commit",
        "-qm",
        subject,
        "-m",
        "Keep the synthetic delivery in its own worktree.",
    )
    return git(tree, "rev-parse", "HEAD")


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "sample.py").write_text(SOURCE)
    (repo / "tests").mkdir()
    (repo / "tests/test_sample.py").write_text(GATE)
    git(repo, "add", "tests/test_sample.py")
    base = _commit(repo, "test: seed independent functions")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--bare", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    docs = repo / "docs"
    docs.mkdir()
    home = runs.crew_home().parent
    home.mkdir(parents=True, exist_ok=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    state = SimpleNamespace(
        repo=repo, base=base, members=[], calls=[], measured=[], refused=set()
    )
    command = shlex.join(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "{node_test_files}",
        ]
    )
    state.command = command
    monkeypatch.setattr(
        queue_landing,
        "_measure_config",
        lambda pointer: {"landing": {"measure_partition": ""}},
    )
    monkeypatch.setattr(queue_landing, "_measure_timeout", lambda pointer: 90.0)
    monkeypatch.setattr(landing_measure, "POPULATION_WORKERS", 1)
    actual = queue_landing._measure_delivery

    def measure(*args, **kwargs):
        result = actual(*args, **kwargs)
        state.measured.append(result)
        return result

    monkeypatch.setattr(queue_landing, "_measure_delivery", measure)

    def dry(run_id, **options):
        return {
            "ok": run_id not in state.refused,
            "would_promote": run_id not in state.refused,
            "refusals": ["synthetic promotion refusal"]
            if run_id in state.refused
            else [],
        }

    def complete(run_id, **options):
        assert run_id not in state.refused
        pointer = runs.read_pointer(run_id)
        head = git(Path(pointer["worktree"]), "rev-parse", "HEAD")
        row = {**pointer, "commits": [head], "promoted_revision": head}
        path = ledger.run_path(PROJECT, run_id, repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row))
        state.calls.append(run_id)
        runs.pointer_path(run_id).unlink()
        return {"ok": True, "project": PROJECT, "record": row}

    monkeypatch.setattr(
        commands,
        "_crew_modules",
        lambda: (
            SimpleNamespace(
                CrewError=runs.CrewError, complete=complete, complete_dry_run=dry
            ),
            None,
        ),
    )
    from reckon.crew import promotion

    monkeypatch.setattr(promotion, "complete_dry_run", dry)
    from reckon.crew import promotion_release

    monkeypatch.setattr(
        promotion_release,
        "_release_after_promotion",
        lambda *args, **kwargs: {"worktree_released": True},
    )
    monkeypatch.setattr(
        promotion_release,
        "_record_release_on_ledger",
        lambda **kwargs: (kwargs["release"], None),
    )

    def deliver(name, value, *, original=SOURCE):
        run_id = "r-" + name + "-" + str(value)
        tree = tmp_path / run_id
        git(repo, "worktree", "add", "--detach", str(tree), base)
        (tree / "sample.py").write_text(
            original.replace(
                f"def {name}():\n    return 0", f"def {name}():\n    return {value}"
            )
        )
        head = _commit(tree, "test: deliver a function change")
        # Exercise the fixture's own gate at every delivered head as well.
        checked = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "tests/test_sample.py",
            ],
            cwd=tree,
            env={"PATH": str(Path(sys.executable).parent), "PYTHONPATH": "."},
            capture_output=True,
            text=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(tree),
            "base_sha": base,
            "role": "implement",
            "suite_command": command,
            "node": {"write_paths": ["sample.py", "tests/test_sample.py"]},
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        member = {
            "pointer": pointer,
            "head": head,
            "options": {
                "run_id": run_id,
                "gate": "passed",
                "commits": [head],
                "land": True,
                "wait": True,
            },
        }
        state.members.append(member)
        return member

    state.deliver = deliver
    return state


def _request(fleet, members=None):
    members = members or fleet.members
    leader = members[0]["pointer"]["run_id"]
    for member in members:
        member["pointer"]["landing_group"] = leader
        runs._write_json(
            runs.pointer_path(member["pointer"]["run_id"]), member["pointer"]
        )
    return {"run_id": leader, "kind": "landing-group", "options": {"members": members}}


def test_three_independent_edits_publish_one_proved_set(fleet):
    for index, name in enumerate(("alpha", "beta", "gamma"), 1):
        fleet.deliver(name, index)
    result = commands._complete_landing_group(_request(fleet))
    assert result["ok"]
    assert len(fleet.measured) == 1
    assert len(fleet.calls) == 3
    text = (fleet.repo / "sample.py").read_text()
    for index, name in enumerate(("alpha", "beta", "gamma"), 1):
        assert f"def {name}():\n    return {index}" in text
        git(
            fleet.repo,
            "merge-base",
            "--is-ancestor",
            fleet.members[index - 1]["head"],
            "HEAD",
        )
    assert (
        git(fleet.repo, "rev-parse", "HEAD")
        == git(fleet.repo, "ls-remote", "origin", "refs/heads/main").split()[0]
    )
    assert not list(pending_landing.pending_dir().glob("*.json"))


def test_a_clean_merge_with_an_added_gate_failure_publishes_nothing(fleet):
    fleet.deliver("alpha", 1)
    fleet.deliver("beta", 9)
    with pytest.raises(runs.CrewError, match="test_interaction"):
        commands._complete_landing_group(_request(fleet))
    assert not fleet.calls
    assert git(fleet.repo, "rev-parse", "HEAD") == fleet.base


def test_a_promotion_refusal_precedes_every_group_write(fleet):
    fleet.deliver("alpha", 1)
    member = fleet.deliver("beta", 2)
    fleet.refused.add(member["pointer"]["run_id"])
    with pytest.raises(runs.CrewError, match="synthetic promotion refusal"):
        commands._complete_landing_group(_request(fleet))
    assert not fleet.calls and not fleet.measured
    assert git(fleet.repo, "rev-parse", "HEAD") == fleet.base


def test_recovery_finishes_the_whole_group_after_a_promotion_interrupt(
    fleet, monkeypatch
):
    fleet.deliver("alpha", 1)
    fleet.deliver("beta", 2)
    request = _request(fleet)
    actual = commands._land_a_promoted_run

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted after ledger write")

    monkeypatch.setattr(commands, "_land_a_promoted_run", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        commands._complete_landing_group(request)
    assert len(fleet.calls) == 1
    assert git(fleet.repo, "rev-parse", "HEAD") == fleet.base
    monkeypatch.setattr(commands, "_land_a_promoted_run", actual)
    recovered = pending_landing.finish_pending_landings()
    assert len(fleet.calls) == 2
    assert len(fleet.measured) == 1
    assert {row["status"] for row in recovered} == {"finished"}
    assert len(recovered) == 2


def test_an_interrupted_publication_is_not_measured_twice(fleet, monkeypatch):
    fleet.deliver("alpha", 1)
    fleet.deliver("beta", 2)
    request = _request(fleet)
    actual = commands._land_a_promoted_run
    interrupted = False

    def interrupt(*args, **kwargs):
        nonlocal interrupted
        result = actual(*args, **kwargs)
        if (
            pending_landing.LANDING_GROUP.get()
            and not result[0].get("group_pending")
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError("interrupted after publication")
        return result

    monkeypatch.setattr(commands, "_land_a_promoted_run", interrupt)
    with pytest.raises(RuntimeError, match="after publication"):
        commands._complete_landing_group(request)
    monkeypatch.setattr(commands, "_land_a_promoted_run", actual)
    assert commands._complete_landing_group(request)["ok"]
    assert len(fleet.measured) == 1


def test_only_textually_compatible_deliveries_are_grouped(fleet, monkeypatch):
    fleet.deliver("alpha", 1)
    fleet.deliver("beta", 2)
    conflicting = fleet.deliver("alpha", 7)
    # Queueing itself uses the same completion options as an ordinary landing.
    monkeypatch.setattr(
        queue_landing,
        "_delivered_revisions",
        lambda run_id: tuple(
            member["head"]
            for member in fleet.members
            if member["pointer"]["run_id"] == run_id
        ),
    )
    eligible = [
        (
            member["pointer"],
            member["pointer"]["run_id"],
            PROJECT,
            member["head"],
            member["pointer"]["worktree"],
            "code",
            "review",
            {},
            fleet.base,
        )
        for member in fleet.members
    ]
    queued = queue_landing._queue_landing_groups(eligible)
    assert len(queued) == 2
    assert conflicting["pointer"]["run_id"] not in queued
    request = promotion_requests.read_promotion(queued[0])
    assert request["kind"] == "landing-group"
    assert promotion_requests.read_promotion(queued[1])["run_id"] == queued[0]


def test_conflicting_heads_report_the_exact_path_and_other_side(fleet):
    first = fleet.deliver("alpha", 1)
    conflict = fleet.deliver("alpha", 7)
    with pytest.raises(merge.MergeConflictError) as refused:
        merge.build_merge_group(fleet.repo, first["head"], [conflict["head"]])
    assert refused.value.paths == ["sample.py"]
    assert refused.value.base == first["head"]


@pytest.mark.parametrize("later", [1, 2], ids=["second-landing", "third-landing"])
def test_a_moved_primary_refreshes_a_clean_reviewed_candidate(fleet, later):
    for index, name in enumerate(("alpha", "beta", "gamma"), 1):
        fleet.deliver(name, index)
    receipts = []
    for member in fleet.members:
        run_id = member["pointer"]["run_id"]
        measured = queue_landing._measure_delivery(
            member["pointer"],
            run_id,
            member["head"],
            member["pointer"]["worktree"],
            promoted=True,
        )
        assert not queue_landing._measure_guard(measured)
        receipts.append(measured)
    git(fleet.repo, "merge", "--ff-only", receipts[0].population["candidate_sha"])
    member = fleet.members[later]
    conflicts = queue_landing._merge_conflicts(member["pointer"], member["head"])
    assert not conflicts
    checked = queue_landing._verified_measurement(
        member["pointer"]["run_id"], receipts[later].population["candidate_sha"]
    )
    assert checked.head == member["head"]
    assert checked.population["main_sha"] == git(fleet.repo, "rev-parse", "HEAD")
    assert not queue_landing._measure_guard(checked)


def test_the_automatic_pass_queues_one_group_and_the_drain_publishes_it(
    fleet, monkeypatch
):
    for index, name in enumerate(("alpha", "beta", "gamma"), 1):
        fleet.deliver(name, index)
    monkeypatch.setattr(
        queue_landing.recovery,
        "classify_pointer",
        lambda pointer: {
            "manifest_status": "complete",
            "manifest_commits": [git(Path(pointer["worktree"]), "rev-parse", "HEAD")],
        },
    )
    monkeypatch.setattr(queue_landing, "_owed_review_tiers", lambda pointer: [])
    monkeypatch.setattr(queue_landing, "_red_suite_guard", lambda *args: "")
    result = queue_landing.land_once()
    ids = [member["pointer"]["run_id"] for member in fleet.members]
    assert set(result["promotion_requested"]) == set(ids)
    assert not fleet.measured, "individual gates ran before the group was composed"
    service = promotion_requests.PromotionService(width=1)
    assert len(service.tick()) == 1
    service.settle(90)
    receipt = promotion_requests.read_promotion(ids[0])
    assert receipt["phase"] == promotion_requests.PROMOTED, receipt
    assert len(fleet.measured) == 1
    assert len(fleet.calls) == 3
    for run_id in ids:
        assert (
            promotion_requests.read_promotion(run_id)["phase"]
            == promotion_requests.PROMOTED
        )


def test_each_distinct_gate_keeps_its_own_candidate_log(fleet):
    fleet.deliver("alpha", 1)
    member = fleet.deliver("beta", 2)
    member["pointer"]["suite_command"] += " -x"
    request = _request(fleet)
    assert commands._complete_landing_group(request)["ok"]
    assert len(fleet.measured) == 2
    paths = [item.suite["head"]["log_path"] for item in fleet.measured]
    assert len(set(paths)) == 2
    for path in paths:
        assert Path(path).is_file()


def test_recovery_refuses_a_ledger_row_for_another_delivered_head(fleet, monkeypatch):
    fleet.deliver("alpha", 1)
    fleet.deliver("beta", 2)
    monkeypatch.setattr(
        pending_landing,
        "_promoted_row",
        lambda *args: {"promoted_revision": fleet.base},
    )
    with pytest.raises(runs.CrewError, match="different head"):
        commands._complete_landing_group(_request(fleet))
    assert not fleet.measured and not fleet.calls
