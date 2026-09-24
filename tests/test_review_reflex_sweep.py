"""The periodic sweep dispatches the review a scoring run composed for itself.

The reflex only earns its name when it fires on the path a healthy run takes.
A run that reaches scoring has already had its review dispatch composed and
returned as a string, and leaving it composed is what left runs waiting on a
coordinator to notice and retype the command. The site is the same periodic
sweep that already resumes runs whose external wait ended, so a session
watching its own fleet is what notices a run entering scoring.

Each property is argued with its negative half, because both failure modes
here look like an idle fleet rather than an error. A sweep that dispatches
nothing is indistinguishable from a fleet with nothing to dispatch, and a
sweep that re-dispatches what it already dispatched manufactures reviews. A
refusal on one run must leave the rest dispatched, because a sweep that stops
at the first refusal makes one unavailable lane look like a quiet one. A run
awaiting review is itself an unreconciled run, so the composed command carries
the unreconciled-runs waiver — without it the fence refuses the very dispatch
that clears the backlog. A review run is never its own source run, keyed on
the role it carried and on the node id it was minted with, so no sweep
composes a review of a review.

The dispatch is asserted against a stub launcher that records its calls: the
claim is that the sweep issued the dispatch, and an issued command is visible
in the recorded call, whereas a review that ran and wrote nothing is not
visible in any output.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, resumption, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer


CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

# The same fleet with the unreconciled-runs fence armed. The grace is short and
# the fixture manifests are backdated past it, so the fence genuinely fires and
# the waiver the reflex needs is exercised rather than assumed.
GRACED_CONFIG = {
    **CONFIG,
    "fences": {
        "time_budget": "25m",
        "needs_help_after_failures": 2,
        "unreconciled_run_grace": "10m",
    },
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A sweep dispatches the composed review</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo


def _completed_pointer(
    config_home: Path,
    repo: Path,
    run_id: str,
    *,
    role: str = "",
    node_id: str = "",
    repo_path: Path | None = None,
    session: str = "session-orchestrating",
) -> dict:
    """A completed run with no stored review, carrying the role it was dispatched with."""
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\ncommits: " + run_id + "\n",
        encoding="utf-8",
    )
    node = {"id": node_id or run_id, "plan": "fixture", "section": "s2"}
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo_path or repo),
        "node": node,
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": session,
        "manifest_path": str(manifest),
    }
    if role:
        record["role"] = role
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _backdate_manifest(record: dict, seconds: int) -> None:
    """Age a run's manifest past the grace, so the fence counts it."""
    path = Path(str(record["manifest_path"]))
    when = time.time() - seconds
    os.utime(path, (when, when))


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state("sample")["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _release_watch() -> None:
    """Release the watch producer this module armed, so the next test arms its own."""
    if crew.watch_state("sample")["watcher_live"]:
        recovery.unwatch("sample")
        _wait_for_stopped_producer()


@contextmanager
def _armed_fleet():
    """Hold the follower claim the review dispatch is gated on, then release the watch."""
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            _release_watch()


def _launcher_recording(calls: list[dict]):
    """A launcher that records every call and reports success without spawning."""

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        calls.append(
            {
                "argv": list(getattr(plan, "argv", ())),
                "log_path": str(log_path),
                "prompt_path": str(prompt_path),
            }
        )
        return os.getpid()

    return launcher


def test_the_sweep_dispatches_the_review_a_scoring_run_composed(
    isolated_project: tuple[Path, Path],
) -> None:
    """No command is issued: the sweep dispatched the composed review itself."""
    config_home, repo = isolated_project
    _completed_pointer(config_home, repo, "r-work")
    calls: list[dict] = []
    with _armed_fleet():
        report = resumption.sweep(
            "sample", config=CONFIG, launcher=_launcher_recording(calls)
        )
    assert len(calls) == 1
    assert report["reviews"]["dispatched"]
    assert runs.read_pointer("r-work")["review_dispatch"]["status"] == "dispatched"


def test_a_second_sweep_over_the_same_fleet_dispatches_no_second_review(
    isolated_project: tuple[Path, Path],
) -> None:
    """Idempotence: the review in flight is found, so the reflex does not re-fire."""
    config_home, repo = isolated_project
    _completed_pointer(config_home, repo, "r-work")
    calls: list[dict] = []
    launcher = _launcher_recording(calls)
    with _armed_fleet():
        first = resumption.sweep("sample", config=CONFIG, launcher=launcher)
        second = resumption.sweep("sample", config=CONFIG, launcher=launcher)
    assert len(first["reviews"]["dispatched"]) == 1
    assert second["reviews"]["dispatched"] == []
    assert len(calls) == 1


def test_a_refused_review_leaves_the_rest_of_the_wave_dispatched(
    isolated_project: tuple[Path, Path],
) -> None:
    """A refusal on one run leaves the sweep reaching the rest of the wave.

    The refusal recorded here is the seat the first review took, which is why a
    second review cannot follow it in one coordinator session. What the sweep
    owes its reader either way is the refusal itself and the fact that it kept
    going: a sweep that stopped at the first refusal reports nothing at all
    about the run it never reached, which makes one occupied seat look like a
    quiet fleet.
    """
    config_home, repo = isolated_project
    _backdate_manifest(_completed_pointer(config_home, repo, "r-one"), 3600)
    _backdate_manifest(_completed_pointer(config_home, repo, "r-two"), 3600)
    calls: list[dict] = []
    with _armed_fleet():
        report = resumption.sweep(
            "sample", config=GRACED_CONFIG, launcher=_launcher_recording(calls)
        )
    assert len(report["reviews"]["dispatched"]) == 1
    assert [row["run_id"] for row in report["reviews"]["refused"]] == ["r-two"]
    assert len(calls) == 1
    assert runs.read_pointer("r-one")["review_dispatch"]["status"] == "dispatched"
    assert runs.read_pointer("r-two")["review_dispatch"]["status"] == "refused"


def test_the_review_dispatch_waives_the_unreconciled_fence(
    isolated_project: tuple[Path, Path],
) -> None:
    """A run awaiting review IS an unreconciled run, so its review waives the fence."""
    config_home, repo = isolated_project
    _backdate_manifest(_completed_pointer(config_home, repo, "r-work"), 3600)
    with _armed_fleet():
        report = resumption.sweep(
            "sample", config=GRACED_CONFIG, launcher=_launcher_recording([])
        )
    assert report["reviews"]["dispatched"], report["reviews"]
    review_run_id = str(runs.read_pointer("r-work")["review_dispatch"]["run_id"])
    waiver = runs.read_pointer(review_run_id)["unreconciled_override"]
    assert waiver["requested"] is True
    assert [row["run_id"] for row in waiver["waived_runs"]] == ["r-work"]


def test_the_fence_refuses_the_same_run_without_the_waiver(
    isolated_project: tuple[Path, Path],
) -> None:
    """The positive control: the fence is genuinely armed, so the waiver is doing work."""
    config_home, repo = isolated_project
    source = _completed_pointer(config_home, repo, "r-work")
    _backdate_manifest(source, 3600)
    with _armed_fleet():
        report = recovery.dispatch_review_for_run(
            runs.read_pointer("r-work"),
            config=GRACED_CONFIG,
            launcher=_launcher_recording([]),
            allow_unreconciled_runs=False,
        )
    assert report["dispatched"] is False
    assert report["refused"] is True
    assert "r-work" in report["reason"]
    assert "10m" in report["reason"]


def test_a_recorded_refusal_is_the_report_and_not_a_generator(
    isolated_project: tuple[Path, Path],
) -> None:
    """The accumulator holds the report, which is what a JSON emit path can read."""
    config_home, repo = isolated_project
    _backdate_manifest(_completed_pointer(config_home, repo, "r-one"), 3600)
    _backdate_manifest(_completed_pointer(config_home, repo, "r-two"), 3600)
    with _armed_fleet():
        report = resumption.sweep(
            "sample", config=GRACED_CONFIG, launcher=_launcher_recording([])
        )
    entry = report["reviews"]["refused"][0]
    assert isinstance(entry, dict)
    assert entry["run_id"] == "r-two"
    assert "reason" in entry
    assert json.loads(json.dumps(entry))["refused"] is True


def test_the_composed_review_command_carries_the_unreconciled_waiver(
    isolated_project: tuple[Path, Path],
) -> None:
    """The command a reader may retype must succeed on the run it was composed for."""
    config_home, repo = isolated_project
    record = _completed_pointer(config_home, repo, "r-work")
    argv = recovery._review_dispatch_argv(runs.read_pointer("r-work"))
    assert "--allow-unreconciled-runs" in argv
    assert "--role" in argv and argv[argv.index("--role") + 1] == "review"
    assert "--allow-unreconciled-runs" in recovery._review_dispatch_action(record)


def test_a_review_run_is_never_a_source_run_by_its_node_id(
    isolated_project: tuple[Path, Path],
) -> None:
    """Keyed on the minted node id too, so a reviewer without a recorded role is inert."""
    config_home, repo = isolated_project
    _completed_pointer(
        config_home, repo, "r-legacy-review", node_id="review-of-r-earlier"
    )
    assert (
        recovery.classify_pointer(runs.read_pointer("r-legacy-review"))[
            "classification"
        ]
        == "scoring"
    )
    calls: list[dict] = []
    with _armed_fleet():
        report = resumption.sweep(
            "sample", config=CONFIG, launcher=_launcher_recording(calls)
        )
    assert calls == []
    assert report["reviews"]["dispatched"] == []
    assert "review_dispatch" not in runs.read_pointer("r-legacy-review")


def test_the_sweep_records_its_review_outcome_per_writer(
    isolated_project: tuple[Path, Path],
) -> None:
    """A follower's reader asks its own writer, so the count lands on that writer's entry."""
    config_home, repo = isolated_project
    _completed_pointer(config_home, repo, "r-work")
    follower = {"kind": "follower", "key": "session-orchestrating"}
    with _armed_fleet():
        resumption.sweep(
            "sample",
            config=CONFIG,
            launcher=_launcher_recording([]),
            writer=follower,
        )
    entry = resumption.writer_sweep_status("sample", follower)
    assert entry["reviews"] == {
        "dispatched": 1,
        "refused": 0,
        "awaiting_lane": 0,
    }
