"""A measurement run builds and gates exactly as an implement run does, and lands not.

The paired replay report measures each arm on the same already-landed sections,
so a replay run must build and gate like any implementation run and then be
picked up by nothing: the review reflex composes no review for it, the lander
never takes it, and ``crew complete`` writes a row whose outcome is
``measured``, leaves the primary branch's head where it found it, and frees the
worktree. A section declared ``done`` states its work is already on the branch,
so it is dispatchable only under the flag that marks this run as a measurement.

Every case synthesises its own repository, plan, ledger and workspace under a
temporary config home. The dispatch is handed a launcher that stands in for the
worker, and the delivery the worker would have produced is written here: the
manifest, the run's commit, and the token counts a harness fold would have put
on the run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _store, crew, crew_dispatch_commands
from reckon import cli as cli_module
from reckon.crew import (
    open_run_index,
    promotion_records,
    queue_landing,
    recovery_classification,
    recovery_review_dispatch,
    runs,
)
from reckon.ledger import normalize_section
from reckon.worker_git_shim import RUN_ID_ENV
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
LANDED_SECTION = "landed"
OPEN_SECTION = "open"
SESSION = "session-measurement"
NODE_ID = "measurement-fixture"
REASON = "every arm builds the same landed section so their figures are comparable"
INPUT_TOKENS = 1200
OUTPUT_TOKENS = 40

CONFIG: dict = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

PLAN_HTML = (
    "<!DOCTYPE html>\n"
    '<meta name="docs-project" content="proj">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="plan-a">\n'
    '<meta name="plan-section-declarations" content="'
    "{&quot;landed&quot;:&quot;done&quot;,&quot;open&quot;:&quot;implementable&quot;}"
    '">\n'
    '<h2 id="landed">A completed task</h2>\n'
    '<h2 id="open">An open task</h2>\n'
)


def _node(section: str, *, manifest: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        plan=PLAN,
        section=section,
        goal="build the section every arm replays",
        done_when="the gate passes at the delivered commit",
        write_paths=["delivered.txt"],
        manifest_path=str(manifest),
        spec_level="exact",
        role="implement",
        time_budget="20m",
    )


@pytest.fixture()
def measurement_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised repository carrying one landed and one open section."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(PLAN_HTML, encoding="utf-8")
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _attach_primary_remote(repository: Path, tmp_path: Path) -> str:
    """Publish the primary branch and return its remote head."""
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "--initial-branch=main", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-q", "--set-upstream", "origin", "main")
    return _git(repository, "rev-parse", "origin/main")


def _dispatch(
    repository: Path,
    manifest: Path,
    *,
    section: str = LANDED_SECTION,
    measure_only: str = REASON,
    base: str = "HEAD",
) -> dict:
    """Dispatch one node the way ``crew dispatch --measure-only`` asks for it."""
    options: dict = {}
    if measure_only:
        options["measure_only"] = measure_only
    return crew.dispatch(
        node=_node(section, manifest=manifest),
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session=SESSION,
        base=base,
        check_budget=False,
        unreviewed_plan_override=True,
        dispatch_options=options,
        launcher=lambda *_args, **_kwargs: os.getpid(),
    )


def _deliver(record: dict, manifest: Path) -> str:
    """Write the manifest and the commit the run's worker would have delivered."""
    worktree = Path(str(record["worktree"]))
    (worktree / "delivered.txt").write_text("the replayed change\n", encoding="utf-8")
    _git(worktree, "add", "--", "delivered.txt")
    _git(worktree, "commit", "-q", "-m", "test: deliver the replayed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "\n".join(
            [
                f"node: {NODE_ID}",
                "status: complete",
                "changed_paths:",
                "  - delivered.txt",
                "commits:",
                f"  - {head}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return head


def _git_succeeds(repository: Path, *arguments: str) -> bool:
    """Whether ``git <arguments>`` exits zero, without raising when it does not."""
    environment = {key: value for key, value in os.environ.items() if key != RUN_ID_ENV}
    return (
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        ).returncode
        == 0
    )


def _record_tokens(run_id: str) -> None:
    """Put the token counts a harness fold would have written on the run."""

    def mutation(pointer: dict) -> dict:
        pointer["budget"] = {
            "tokens": {"input": INPUT_TOKENS, "output": OUTPUT_TOKENS},
            "cost_usd": None,
        }
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _end_worker(run_id: str) -> None:
    """End the stand-in worker, so its delivered report reads as an outcome.

    A live pid defers every terminal-looking manifest, which is right for a
    worker still writing and wrong for one that has stopped. The pid named here
    belongs to no process, so the run reads as ended and the landing pass is
    free to judge the delivery it reported.
    """

    def mutation(pointer: dict) -> dict:
        pointer["pid"] = 999_999
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _delivered_measurement_run(repository: Path, tmp_path: Path) -> dict:
    """One measurement run dispatched and delivered, holding live records."""
    manifest = tmp_path / "manifests" / f"{NODE_ID}.md"
    base = _git(repository, "rev-parse", "HEAD")
    record = _dispatch(repository, manifest, base=base)
    head = _deliver(record, manifest)
    _record_tokens(str(record["run_id"]))
    return {
        "run_id": str(record["run_id"]),
        "record": record,
        "head": head,
        "base": base,
        "manifest": manifest,
    }


def _complete(
    run: dict,
    repository: Path,
    tmp_path: Path,
    *,
    gate: str = "passed",
):
    """Promote the delivered run the way a coordinator does, without landing."""
    gate_log = tmp_path / "gate.log"
    exit_status = 1 if gate == "failed" else 0
    command = "false" if gate == "failed" else "true"
    gate_log.write_text(f"# command: {command}\nEXIT={exit_status}\n", encoding="utf-8")
    arguments = [
        "crew",
        "complete",
        "--wait",
        "--run",
        run["run_id"],
        "--gate",
        gate,
        "--commit",
        run["head"],
        "--checkout-path",
        str(repository),
        "--gate-command",
        command,
        "--gate-exit-status",
        str(exit_status),
        "--gate-log-path",
        str(gate_log),
    ]
    if gate == "failed":
        arguments += [
            "--failure-classification",
            "negative-result",
            "--outcome",
            "the paired suite adds a failure",
        ]
    return CliRunner().invoke(
        cli_module.main,
        arguments,
    )


def _record_added_failure_arms(run: dict) -> str:
    """Give a delivered run the same clean base and failing head suite arms."""
    failure_id = "tests/test_sample.py::test_new_regression"

    def mutation(pointer: dict) -> dict:
        pointer["suite_command"] = "true"
        return pointer

    runs._mutate_pointer(run["run_id"], mutation)
    baseline = {
        "revision": run["base"],
        "command": "true",
        "exit_status": 0,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
        "log_path": "base-suite.log",
    }
    after = {
        "revision": run["head"],
        "command": "true",
        "exit_status": 1,
        "completed": True,
        "failure_count": 1,
        "failure_ids": [failure_id],
        "log_path": "head-suite.log",
    }
    with run["manifest"].open("a", encoding="utf-8") as stream:
        stream.write(f"baseline_suite: {json.dumps(baseline)}\n")
        stream.write(f"after_suite: {json.dumps(after)}\n")
    return failure_id


def _delivered_ordinary_run(repository: Path, tmp_path: Path) -> dict:
    """An ordinary implementation run with the same synthetic suite arms."""
    manifest = tmp_path / "manifests" / f"{NODE_ID}-ordinary.md"
    base = _git(repository, "rev-parse", "HEAD")
    record = _dispatch(
        repository, manifest, section=OPEN_SECTION, measure_only="", base=base
    )
    head = _deliver(record, manifest)
    return {
        "run_id": str(record["run_id"]),
        "record": record,
        "head": head,
        "base": base,
        "manifest": manifest,
    }


def _cli_arguments(
    repository: Path, *, section: str, dry_run: bool, measure_only: str = ""
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        section,
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        NODE_ID,
        "--goal",
        "build the section every arm replays",
        "--write-path",
        "delivered.txt",
        "--time-budget",
        "20m",
        "--session",
        SESSION,
        "--repo",
        str(repository),
        "--done-when",
        "the gate passes at the delivered commit",
        "--allow-unreviewed-plan",
        "--dry-run" if dry_run else "--no-watch",
    ]
    if measure_only:
        arguments += ["--measure-only", measure_only]
    return arguments


def _invoke(arguments: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return CliRunner().invoke(cli_module.main, arguments)


# ── the run itself: dispatched, and picked up by nothing ─────────────────────


def test_a_measurement_run_is_marked_by_its_dispatch(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """The pointer states the run is a measurement, and why."""
    run = _delivered_measurement_run(measurement_repo, tmp_path)

    pointer = runs.read_pointer(run["run_id"])

    assert pointer["measure_only"]["reason"] == REASON
    assert run["record"]["measure_only"]["reason"] == REASON


def test_the_review_reflex_composes_no_review_for_a_measurement_run(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """The reflex refuses the run, and composes nothing for it."""
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    pointer = runs.read_pointer(run["run_id"])

    report = recovery_review_dispatch.dispatch_review_for_run(
        pointer, config=CONFIG, launcher=lambda *_a, **_k: os.getpid()
    )

    assert report["refused"] is True, report
    assert report.get("dispatched") is not True, report
    assert "measurement" in report["reason"], report
    reviews = [
        live
        for live in runs.list_live(project=PROJECT)
        if str(live.get("role") or "") == "review"
    ]
    assert reviews == [], "a measurement run was composed a review"


def test_the_lander_makes_no_landing_attempt_on_a_measurement_run(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """The landing pass skips the run and records no judgement against it."""
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    _end_worker(run["run_id"])

    result = queue_landing.land_once()

    skipped = [
        row for row in (result.get("skipped") or []) if row["run_id"] == run["run_id"]
    ]
    assert skipped and skipped[0]["status"] == "measurement-run", result
    pointer = runs.read_pointer(run["run_id"])
    assert not pointer.get("admission_landing"), "the lander judged a measurement run"
    assert run["run_id"] not in result["landed"], result
    assert run["run_id"] not in (result.get("requested") or []), result


def test_complete_records_a_measured_row_and_leaves_the_branch_alone(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """A row saying measured, the gate, the tokens and the commit; no merge."""
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    worktree = Path(str(run["record"]["worktree"]))

    result = _complete(run, measurement_repo, tmp_path)

    assert result.exit_code == 0, result.output
    row = open_run_index.row_for_run(PROJECT, run["run_id"], measurement_repo)
    assert row is not None, result.output
    assert row["outcome"] == "measured", row.get("outcome")
    assert row["gate"] == "passed", row.get("gate")
    assert row["commits"] == [run["head"]], row.get("commits")
    assert row["budget"]["tokens"]["input"] == INPUT_TOKENS, row.get("budget")
    assert row["budget"]["tokens"]["output"] == OUTPUT_TOKENS, row.get("budget")
    # The row is written into the repository's own ledger, so the branch does
    # move; what must never move onto it is the run's commit.
    assert not _git_succeeds(
        measurement_repo, "merge-base", "--is-ancestor", run["head"], "main"
    ), "a measurement run merged its commit into the primary branch"
    assert not _git_succeeds(
        measurement_repo, "cat-file", "-e", "main:delivered.txt"
    ), "a measurement run's change reached the primary branch"
    assert not worktree.is_dir(), "a measurement run kept its worktree"


def test_a_failed_measurement_records_added_suite_failures(
    measurement_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed gate records the added failures as the measurement result."""
    if os.environ.get("RECKON_DISABLE_MEASUREMENT_FAILURE_EXEMPTION") == "1":
        monkeypatch.setattr(
            promotion_records,
            "_failed_measurement_gate",
            lambda _record, _gate_verdict: False,
        )
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    failure_id = _record_added_failure_arms(run)

    result = _complete(run, measurement_repo, tmp_path, gate="failed")

    assert result.exit_code == 0, result.output
    row = open_run_index.row_for_run(PROJECT, run["run_id"], measurement_repo)
    assert row is not None, result.output
    assert row["outcome"] == "measured", row
    assert row["gate"] == "failed", row
    assert row["suite_delta"]["added_failure_ids"] == [failure_id], row


def test_an_ordinary_failed_run_still_refuses_added_suite_failures(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """The same suite delta remains a refusal for an implementation run."""
    run = _delivered_ordinary_run(measurement_repo, tmp_path)
    failure_id = _record_added_failure_arms(run)

    result = _complete(run, measurement_repo, tmp_path, gate="failed")

    assert result.exit_code != 0, result.output
    assert "armed promotion added suite failures" in result.output, result.output
    assert failure_id in result.output, result.output
    assert open_run_index.row_for_run(PROJECT, run["run_id"], measurement_repo) is None


def test_a_passing_measurement_still_refuses_added_suite_failures(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """A passing gate does not record added failures as a measurement result."""
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    failure_id = _record_added_failure_arms(run)

    result = _complete(run, measurement_repo, tmp_path, gate="passed")

    assert result.exit_code != 0, result.output
    assert "suite-delta-refused" in result.output, result.output
    assert failure_id in result.output, result.output
    assert open_run_index.row_for_run(PROJECT, run["run_id"], measurement_repo) is None


def test_a_measurement_complete_records_without_advancing_the_primary_branch(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """Record a completed replay even when its delivered head is off origin/main."""
    primary_head = _attach_primary_remote(measurement_repo, tmp_path)
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    assert not _git_succeeds(
        measurement_repo, "merge-base", "--is-ancestor", run["head"], "origin/main"
    )

    result = _complete(run, measurement_repo, tmp_path)

    assert result.exit_code == 0, result.output
    row = open_run_index.row_for_run(PROJECT, run["run_id"], measurement_repo)
    assert row is not None, result.output
    assert row["outcome"] == "measured", row.get("outcome")
    assert _git(measurement_repo, "rev-parse", "origin/main") == primary_head


def test_a_complete_measurement_is_promotable_without_a_review(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """Classify the same completed pointer as promotable, with no review owed."""
    _attach_primary_remote(measurement_repo, tmp_path)
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    _end_worker(run["run_id"])
    pointer = runs.read_pointer(run["run_id"])

    result = recovery_classification.classify_pointer(pointer, pointers=[pointer])

    assert result["classification"] == "promotable", result["detail"]
    assert "no review is owed" in result["detail"], result["detail"]


# ── a landed section is dispatchable only under the flag ─────────────────────


def test_a_landed_section_dispatched_without_the_flag_is_refused_naming_it(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """The launch refuses, and the refusal names the flag that clears it."""
    manifest = tmp_path / "manifests" / "refused.md"
    data, _version = _store.read_plan(PROJECT, PLAN, root=measurement_repo)
    assert data["section_declarations"][LANDED_SECTION] == "done"

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(measurement_repo, manifest, measure_only="")

    detail = str(refusal.value)
    assert "--measure-only" in detail, detail
    assert normalize_section(LANDED_SECTION) in detail, detail
    assert runs.list_live(project=PROJECT) == [], "the refused dispatch stored a run"


def test_an_open_section_dispatches_without_the_flag(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """The negative half: only a declared-done section asks for the flag."""
    manifest = tmp_path / "manifests" / "open.md"

    record = _dispatch(
        measurement_repo, manifest, section=OPEN_SECTION, measure_only=""
    )

    assert record["run_id"]
    assert "measure_only" not in runs.read_pointer(record["run_id"])


def test_the_dry_run_accepts_the_flag_on_the_same_section(
    measurement_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag the refusal names is the one the command accepts.

    The refusal above is only actionable if passing it changes the answer, so
    the same landed section is dispatched again in preview with
    ``--measure-only``: the command carries the reason into the dispatch and the
    preview goes through instead of refusing.
    """
    accepted = _invoke(
        _cli_arguments(
            measurement_repo,
            section=LANDED_SECTION,
            dry_run=True,
            measure_only=REASON,
        ),
        monkeypatch,
    )

    assert accepted.exit_code == 0, accepted.output
    payload = json.loads(accepted.output.splitlines()[0])
    assert payload.get("ok") is True, payload


def test_the_dry_run_reaches_the_same_refusal(
    measurement_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview that went green would stand in for a refused dispatch."""
    refused = _invoke(
        _cli_arguments(measurement_repo, section=LANDED_SECTION, dry_run=True),
        monkeypatch,
    )

    assert refused.exit_code == 2, refused.output
    payload = json.loads(refused.output.splitlines()[0])
    assert payload["ok"] is False, payload
    assert "--measure-only" in payload["detail"], payload


# ── the negative controls: the skips are what withhold the run ──────────────


def test_the_lander_attempts_a_landing_once_its_skip_is_removed(
    measurement_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the lander's skip makes the pass judge the measurement run.

    The delivery is the one the main case withholds: same run, same manifest,
    same ended worker. With ``_measurement_run_skip`` out of the way the pass
    reaches the run and records a landing judgement against it — the attempt
    the skip exists to prevent — so the main case's silence is a difference the
    skip produced rather than a fixture that is simply unlandable.
    """
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    _end_worker(run["run_id"])
    monkeypatch.setattr(queue_landing, "_measurement_run_skip", lambda _pointer: "")

    queue_landing.land_once()

    judged = runs.read_pointer(run["run_id"]).get("admission_landing")
    assert judged and judged.get("head"), (
        "with the lander's measurement skip removed the pass made no landing "
        f"attempt: {judged}"
    )


def test_the_reflex_composes_a_review_once_its_skip_is_removed(
    measurement_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing both review protections makes the reflex compose a review."""
    run = _delivered_measurement_run(measurement_repo, tmp_path)
    _end_worker(run["run_id"])
    monkeypatch.setattr(
        recovery_classification, "_is_measurement_run", lambda _record: False
    )
    monkeypatch.setattr(
        recovery_review_dispatch,
        "_measurement_run_refusal",
        lambda _record, *, dry_run: None,
    )

    report = recovery_review_dispatch.dispatch_review_for_run(
        runs.read_pointer(run["run_id"]),
        config=CONFIG,
        launcher=lambda *_a, **_k: os.getpid(),
    )

    assert report.get("dispatched") is True, report.get("reason")
    assert report.get("refused") is not True, report


def test_review_debt_does_not_queue_a_landed_section_without_measurement_flag(
    measurement_repo: Path, tmp_path: Path
) -> None:
    """A review hold cannot store a replay the section declaration refuses."""
    with pytest.raises(crew.CrewError, match="--measure-only"):
        crew.dispatch(
            node=_node(LANDED_SECTION, manifest=tmp_path / "manifest.md"),
            project=PROJECT,
            repo=measurement_repo,
            config={**CONFIG, "plan_review_gate": "enforce"},
            session=SESSION,
            route="deterministic",
            check_budget=False,
        )
    assert runs.list_live(project=PROJECT) == []
