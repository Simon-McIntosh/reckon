"""Compatible deliveries share a proved candidate and the ordinary publisher."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
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
        row = {
            **pointer,
            "commits": [head],
            "promoted_revision": head,
            "gate": "passed",
        }
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
    request = _request(fleet)
    for _ in range(2):
        with pytest.raises(runs.CrewError, match="test_interaction"):
            commands._complete_landing_group(request)
    assert len(fleet.measured) == 1
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
    request = _request(fleet, list(reversed(fleet.members)))
    pending_landing.save_group(
        {
            "run_id": request["run_id"],
            "request": request,
            "members": {
                item["pointer"]["run_id"]: item
                for item in request["options"]["members"]
            },
        }
    )
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


def test_conflict_repair_names_the_peer_commit_and_reuses_the_repair_gate(
    fleet, monkeypatch
):
    from reckon.crew import (
        recovery_repair_dispatch,
        recovery_review_dispatch,
        resumption,
    )
    from reckon.crew import review as reviews

    first = fleet.deliver("alpha", 1)
    fleet.deliver("beta", 2)
    assert commands._complete_landing_group(_request(fleet))["ok"]
    conflicting = fleet.deliver("alpha", 7)
    primary = git(fleet.repo, "rev-parse", "HEAD")
    pointer = conflicting["pointer"]
    seen = []
    monkeypatch.setattr(
        recovery_repair_dispatch, "_reviewed_run_is_busy", lambda pointer: ""
    )
    monkeypatch.setattr(
        resumption, "_resume", lambda *args, **kwargs: seen.append(kwargs["advice"])
    )
    with pytest.raises(runs.CrewError, match="merge-conflict"):
        promotion_requests._execute_command(conflicting["options"])
    pointer = runs.read_pointer(pointer["run_id"])
    repair = pointer["merge_conflict"]
    assert len(seen) == 1
    assert "sample.py" in seen[0] and first["head"] in seen[0]
    assert first["head"] in [item["sha"] for item in repair["peer_commits"]]
    tree = Path(pointer["worktree"])
    merged = subprocess.run(
        ["git", "-C", str(tree), "merge", "--no-commit", "--no-ff", primary],
        capture_output=True,
        text=True,
        check=False,
    )
    assert merged.returncode == 1
    # Keep the primary function and the repaired delivery's additional intent.
    (tree / "sample.py").write_text(
        (fleet.repo / "sample.py").read_text() + "\nREPAIRED = True\n"
    )
    head = _commit(tree, "test: combine the conflicting intent")
    log = tree / "repair.log"
    checked = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_sample.py"],
        cwd=tree,
        env={"PATH": str(Path(sys.executable).parent), "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0
    gate_command = shlex.join(
        [sys.executable, "-m", "pytest", "-q", "tests/test_sample.py"]
    )
    log.write_text(
        f"# revision: {head}\n# command: {gate_command}\n" + checked.stdout + "EXIT=0\n"
    )
    manifest = tree / "manifest.md"
    manifest.write_text(
        f"status: complete\ncommits: [{conflicting['head']}, {head}]\ntests: {gate_command}\ntest_logs: {json.dumps([str(log)])}\n"
    )
    pointer["manifest_path"] = str(manifest)
    runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
    monkeypatch.setattr(
        queue_landing.recovery,
        "classify_pointer",
        lambda pointer: {"manifest_status": "complete", "manifest_commits": [head]},
    )
    assert (
        pending_landing._delivered_repair(
            pointer, conflicting["head"], repair, fleet.repo
        )
        == head
    )
    # Compose the review through the real dispatcher without launching a worker.
    requests = []

    def compose(pointer, fields, **kwargs):
        requests.append((pointer["run_id"], fields["head"]))
        return {
            "run_id": pointer["run_id"],
            "dispatched": True,
            "review_run_id": "review-" + pointer["run_id"],
        }

    monkeypatch.setattr(recovery_review_dispatch, "_dispatch_composed_review", compose)
    promoted_before = list(fleet.calls)
    result = pending_landing.finish_pending_landings()
    assert result[0]["status"] == "awaiting-review", result
    assert requests == [(pointer["run_id"], head)]
    assert (
        pending_landing._read(pending_landing._record_path(pointer["run_id"]))["head"]
        == head
    )
    assert git(fleet.repo, "rev-parse", "HEAD") == primary
    assert "REPAIRED = True" not in (fleet.repo / "sample.py").read_text()

    review = reviews.parse_review(
        "\n".join(f"SCORE {key}: 20" for key in reviews.REVIEW_DIMENSIONS)
    )
    review.update(
        project=PROJECT,
        reviewed_run_id=pointer["run_id"],
        review_run_id="review-" + pointer["run_id"],
        reviewed_base_sha=pointer["base_sha"],
        reviewed_head_sha=head,
        added_failure_count=0,
        added_failure_ids=[],
    )
    reviews.store_review(review)

    # A stored review releases the gated repair without another promotion.
    result = pending_landing.finish_pending_landings()
    assert result[0]["status"] == "finished", result
    assert requests == [(pointer["run_id"], head)]
    assert fleet.calls == promoted_before
    assert not runs.pointer_path(pointer["run_id"]).exists()
    assert not pending_landing._record_path(pointer["run_id"]).exists()
    assert git(fleet.repo, "ls-remote", "origin", "refs/heads/main").split()[0] == git(
        fleet.repo, "rev-parse", "HEAD"
    )
    assert "REPAIRED = True" in (fleet.repo / "sample.py").read_text()


def test_a_group_and_an_individual_landing_share_the_branch_lease(fleet):
    first = fleet.deliver("alpha", 1)
    second = fleet.deliver("beta", 2)
    third = fleet.deliver("gamma", 3)
    first["pointer"].update(session="first-session", priority_override=10)
    second["pointer"].update(session="second-session", priority_override=90)
    group = _request(fleet, [first, second])
    queued = promotion_requests.request_promotion(
        group["run_id"], kind="landing-group", options=group["options"]
    )
    assert queued["priority_override"] == 90
    promotion_requests.request_promotion(
        third["pointer"]["run_id"], kind="command", options=third["options"]
    )
    entered, release = threading.Event(), threading.Event()
    active = []

    def execute(request):
        active.append(request["run_id"])
        if request["kind"] == "landing-group":
            entered.set()
            assert release.wait(10)
        return {"ok": True}

    service = promotion_requests.PromotionService(width=2, execute=execute)
    try:
        assert service.tick() == [group["run_id"]]
        assert entered.wait(5)
        assert active == [group["run_id"]]
    finally:
        release.set()
        service.settle(15)
    assert service.tick() == [third["pointer"]["run_id"]]
    service.settle(15)
    assert active == [group["run_id"], third["pointer"]["run_id"]]
    receipt = promotion_requests.read_promotion(second["pointer"]["run_id"])
    assert receipt["session"] == "second-session"
    assert receipt["priority_override"] == 90


def test_a_failed_promotion_row_does_not_admit_a_group(fleet, monkeypatch):
    first = fleet.deliver("alpha", 1)
    fleet.deliver("beta", 2)
    monkeypatch.setattr(
        pending_landing,
        "_promoted_row",
        lambda *args: {"promoted_revision": first["head"], "gate": "failed"},
    )
    with pytest.raises(runs.CrewError, match="no passing promotion"):
        commands._complete_landing_group(_request(fleet))
    assert not fleet.measured and not fleet.calls


@pytest.mark.parametrize("conflicting_name", ["alpha", "beta"])
@pytest.mark.parametrize("after_promotion", [False, True], ids=["queued", "promoted"])
def test_a_primary_conflict_after_queueing_returns_its_member_for_repair(
    fleet, monkeypatch, conflicting_name, after_promotion
):
    from reckon.crew import recovery_repair_dispatch, resumption

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
    assert len(queue_landing.land_once()["promotion_requested"]) == 3
    primary = ""

    def move_primary():
        nonlocal primary
        (fleet.repo / "sample.py").write_text(
            SOURCE.replace(
                f"def {conflicting_name}():\n    return 0",
                f"def {conflicting_name}():\n    return 8",
            )
        )
        primary = _commit(fleet.repo, "test: move primary after queueing")

    if after_promotion:
        actual = commands._land_a_promoted_run

        def moved(*args, **kwargs):
            result = actual(*args, **kwargs)
            if len(fleet.calls) == 3 and not primary:
                move_primary()
            return result

        monkeypatch.setattr(commands, "_land_a_promoted_run", moved)
    else:
        move_primary()
    seen = []
    monkeypatch.setattr(
        recovery_repair_dispatch, "_reviewed_run_is_busy", lambda pointer: ""
    )
    monkeypatch.setattr(
        resumption,
        "_resume",
        lambda run_id, *args, **kwargs: seen.append((run_id, kwargs["advice"])),
    )
    service = promotion_requests.PromotionService(width=1)
    assert len(service.tick()) == 1
    service.settle(90)
    member = next(
        member
        for member in fleet.members
        if member["pointer"]["run_id"].startswith("r-" + conflicting_name + "-")
    )
    run_id = member["pointer"]["run_id"]
    if after_promotion:
        assert seen == []
        assert not runs.pointer_path(run_id).exists()
        receipt = json.loads((runs.run_dir(run_id) / "merge-repair.json").read_text())
        assert receipt["status"] == "refused"
        assert "promoted" in receipt["reason"]
        group_path = pending_landing.group_path(fleet.members[0]["pointer"]["run_id"])
        assert str(group_path) in receipt["reason"]
        assert group_path.exists()
        assert pending_landing.finish_landing_groups()[0]["status"] == "group-refused"
        assert seen == []
        assert git(fleet.repo, "rev-parse", "HEAD") == primary
        return
    assert [item[0] for item in seen] == [run_id]
    pointer = runs.read_pointer(run_id)
    repair = pointer["merge_conflict"]
    assert repair["main_revision"] == primary
    assert repair["head"] == member["head"]
    assert repair["paths"] == ["sample.py"]
    assert primary in seen[0][1] and "sample.py" in seen[0][1]
    assert len(fleet.calls) == (3 if after_promotion else 0)
    assert len(fleet.measured) == (1 if after_promotion else 0)
    assert git(fleet.repo, "rev-parse", "HEAD") == primary
    assert pending_landing.finish_landing_groups()[0]["status"] == "group-refused"
    assert len(seen) == 1, "a waiting repair resumed the same worker twice"
    tree = Path(pointer["worktree"])
    merged = subprocess.run(
        ["git", "-C", str(tree), "merge", "--no-commit", "--no-ff", primary],
        capture_output=True,
        text=True,
        check=False,
    )
    assert merged.returncode == 1
    (tree / "sample.py").write_text(
        SOURCE.replace(
            f"def {conflicting_name}():\n    return 0",
            f"def {conflicting_name}():\n    # REPAIRED = True\n    return 8",
        )
    )
    head = _commit(tree, "test: retain both conflicting intentions")
    command = shlex.join([sys.executable, "-m", "pytest", "-q", "tests/test_sample.py"])
    checked = subprocess.run(
        shlex.split(command),
        cwd=tree,
        env={"PATH": str(Path(sys.executable).parent), "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0
    log = tree / "repair.log"
    log.write_text(
        f"# revision: {head}\n# command: {command}\n" + checked.stdout + "EXIT=0\n"
    )
    manifest = tree / "manifest.md"
    manifest.write_text(
        f"status: complete\ncommits: [{member['head']}, {head}]\ntests: {command}\ntest_logs: {json.dumps([str(log)])}\n"
    )
    pointer["manifest_path"] = str(manifest)
    runs._write_json(runs.pointer_path(run_id), pointer)
    results = pending_landing.finish_landing_groups()
    assert len(results) == 3 and all(row["status"] == "finished" for row in results), (
        results
    )
    assert len(fleet.calls) == 3 and len(set(fleet.calls)) == 3
    assert len(fleet.measured) == (2 if after_promotion else 1)
    assert len(seen) == 1
    text = (fleet.repo / "sample.py").read_text()
    assert "REPAIRED = True" in text
    for index, name in enumerate(("alpha", "beta", "gamma"), 1):
        expected = (
            f"def {name}():\n    # REPAIRED = True\n    return 8"
            if name == conflicting_name
            else f"def {name}():\n    return {index}"
        )
        assert expected in text
    assert not pending_landing.group_path(
        fleet.members[0]["pointer"]["run_id"]
    ).exists()
