"""A changed figure receives its own read-only, image-capable review."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery_review_dispatch as recovery
from reckon.crew import recovery_review_subject as review_subject
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "sample"
RUN_ID = "run-under-review"
FIGURE = "docs/figures/p/a.svg"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _backend(command: Path, *, images: bool = False) -> dict:
    entry = {
        "launch": "cli",
        "command": str(command),
        "model": "review-model",
        "effort": "medium",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }
    if images:
        entry["input_modalities"] = ["text", "image"]
    return entry


def _flight(tmp_path: Path, *, images: bool) -> tuple[dict, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("codex", "claude"):
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    config = {
        "default_backend": "text-lane",
        "local_backend": "text-lane",
        "backends": {"text-lane": _backend(bin_dir / "codex")},
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    if images:
        config["backends"]["image-lane"] = _backend(bin_dir / "codex", images=True)
    return config, bin_dir


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A mounted repository and its flight, entirely inside the test tree."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="overview">Review fixture</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs/plans/fixture.html"),
        ("commit", "-q", "-m", "chore: seed fixture"),
    ):
        _git(repo, *args)
    base_sha = _git(repo, "rev-parse", "HEAD")
    figure_path = repo / FIGURE
    figure_path.parent.mkdir(parents=True)
    figure_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
        '<path d="M2 38 L78 2" stroke="#245a84"/></svg>\n',
        encoding="utf-8",
    )
    _git(repo, "add", FIGURE)
    _git(repo, "commit", "-q", "-m", "feat: add figure")
    head_sha = _git(repo, "rev-parse", "HEAD")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    config, _bin = _flight(tmp_path, images=True)
    manifest = home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir()
    manifest.write_text(
        f"node: {RUN_ID}\nstatus: complete\ncommits: {head_sha}\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base_sha,
        "node": {"id": RUN_ID, "plan": "fixture", "section": "overview"},
        "backend": "text-lane",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "review-session",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), record)
    dispatch_module = __import__("reckon.crew.dispatch", fromlist=["dispatch"])

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": head_sha}

    original_dispatch = dispatch_module.dispatch

    def no_watch_dispatch(**kwargs):
        kwargs["watch_required"] = False
        return original_dispatch(**kwargs)

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    monkeypatch.setattr(dispatch_module, "dispatch", no_watch_dispatch)
    return {
        "home": home,
        "repo": repo,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "record": record,
        "config": config,
        "tmp_path": tmp_path,
    }


class _Recorder:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.argv.append([str(value) for value in plan.argv])
        return os.getpid()


def _add_source_only_change(project: dict) -> None:
    repo = project["repo"]
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-q", "-m", "feat: add source")
    project["head_sha"] = _git(repo, "rev-parse", "HEAD")
    project["record"]["base_sha"] = base_sha
    project["record"]["manifest_path"] = str(
        project["home"] / "manifests" / f"{RUN_ID}.md"
    )
    Path(project["record"]["manifest_path"]).write_text(
        f"node: {RUN_ID}\nstatus: complete\ncommits: {project['head_sha']}\n",
        encoding="utf-8",
    )
    project["record"]["worktree"] = str(repo)


def test_changed_figure_dispatches_an_image_review_with_a_png(
    project: dict,
) -> None:
    repository = project["repo"]
    mount = project["tmp_path"] / "mount"
    reviewed_tree = project["tmp_path"] / "reviewed-worktree"
    subprocess.run(
        ["git", "clone", "--quiet", str(repository), str(mount)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(mount, "config", "user.email", "worker@example.invalid")
    _git(mount, "config", "user.name", "Worker")
    _git(mount, "checkout", "--quiet", "--detach", project["base_sha"])
    _git(
        mount,
        "worktree",
        "add",
        "--detach",
        str(reviewed_tree),
        project["base_sha"],
    )
    figure_path = reviewed_tree / FIGURE
    figure_path.parent.mkdir(parents=True)
    figure_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
        '<path d="M2 38 L78 2" stroke="#245a84"/></svg>\n',
        encoding="utf-8",
    )
    _git(reviewed_tree, "add", FIGURE)
    _git(reviewed_tree, "commit", "-q", "-m", "feat: add figure")
    head_sha = _git(reviewed_tree, "rev-parse", "HEAD")
    project["head_sha"] = head_sha
    (project["home"] / "mounts.json").write_text(
        json.dumps({PROJECT: str(mount / "docs")}), encoding="utf-8"
    )
    project["record"]["repo"] = str(mount)
    project["record"]["worktree"] = str(reviewed_tree)
    project["record"]["base_sha"] = project["base_sha"]
    Path(project["record"]["manifest_path"]).write_text(
        f"node: {RUN_ID}\nstatus: complete\ncommits: {head_sha}\n",
        encoding="utf-8",
    )
    crew._write_json(crew.pointer_path(RUN_ID), project["record"])
    assert not (mount / FIGURE).exists()
    assert (reviewed_tree / FIGURE).is_file()

    recorder = _Recorder()
    report = recovery.dispatch_review_for_run(
        project["record"], config=project["config"], launcher=recorder
    )

    figure_report = report["figure_review"]
    assert figure_report["dispatched"] is True, (
        figure_report.get("reason") or figure_report
    )
    assert figure_report["backend"] == "image-lane"
    image_arguments = [
        argv[index + 1]
        for argv in recorder.argv
        for index, value in enumerate(argv[:-1])
        if value == "-i"
    ]
    assert len(image_arguments) == 1
    rendered = Path(image_arguments[0])
    assert rendered.suffix == ".png"
    assert rendered.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert str(project["repo"] / FIGURE) not in image_arguments
    figure_pointer = runs.read_pointer(figure_report["review_run_id"])
    assert figure_pointer["node"]["id"].endswith("-figure")
    assert figure_pointer["backend"] == "image-lane"


def test_source_only_changes_keep_the_text_review(project: dict) -> None:
    _add_source_only_change(project)
    recorder = _Recorder()
    report = recovery.dispatch_review_for_run(
        project["record"], config=project["config"], launcher=recorder
    )

    assert report["dispatched"] is True
    assert "figure_review" not in report
    assert all("-i" not in argv for argv in recorder.argv)


def test_figure_review_waits_when_no_lane_declares_image_input(
    project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    text_report = {"run_id": RUN_ID, "dispatched": True, "review_run_id": "text"}
    monkeypatch.setattr(
        recovery, "_dispatch_text_review_for_run", lambda *a, **k: text_report
    )
    config, _bin = _flight(project["tmp_path"], images=False)

    report = recovery.dispatch_review_for_run(project["record"], config=config)

    figure_report = report["figure_review"]
    assert figure_report["dispatched"] is False
    assert figure_report["awaiting_lane"] is True
    assert "image" in figure_report["reason"].lower()
    stored = runs.read_pointer(RUN_ID)[recovery.FIGURE_REVIEW_DISPATCH_FIELD]
    assert stored["status"] == "awaiting-lane"
    assert "image" in stored["reason"].lower()


def test_figure_prompt_and_brief_cover_every_visual_rule(project: dict) -> None:
    prompt = review_module.load_figure_review_prompt().lower()
    for item in review_module.FIGURE_REVIEW_ITEMS:
        assert item in prompt
    assert "read only" in prompt
    assert "do not edit" in prompt
    fields = review_subject._review_dispatch_fields(
        project["record"],
        delta=None,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
        images=[FIGURE],
    )
    brief = " ".join(fields["brief_text"].lower().split())
    for phrase in (
        "readable at a glance",
        "gradients or decorative",
        "contrast",
        "naturally a table",
        "clipped",
        "do not edit",
    ):
        assert phrase in brief


def test_rubric_paths_keep_text_and_figure_records_separate(project: dict) -> None:
    store = project["tmp_path"] / "reviews"
    base = project["head_sha"]
    text_record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "review_run_id": "text-review-run",
        "reviewed_base_sha": base,
        "reviewed_head_sha": base,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 10),
    }
    figure_record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "review_run_id": "figure-review-run",
        "reviewed_base_sha": base,
        "reviewed_head_sha": base,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "status": "parsed",
        "rubric_items": dict.fromkeys(review_module.FIGURE_REVIEW_ITEMS, "pass"),
        "findings": [],
    }
    text_path = review_module.store_review(text_record, base_dir=store)
    figure_path = review_module.store_review(figure_record, base_dir=store)

    assert text_path != figure_path
    assert ".figure.at-" in figure_path.name
    text_read = review_module.read_review(
        PROJECT, RUN_ID, base_dir=store, reviewed_head_sha=base
    )
    assert text_read is not None
    assert "rubric" not in text_read
    assert text_read["scores"] == text_record["scores"]
    assert (
        review_module.read_review(
            PROJECT,
            RUN_ID,
            base_dir=store,
            reviewed_head_sha=base,
            rubric=review_module.FIGURE_REVIEW_RUBRIC,
        )["rubric"]
        == review_module.FIGURE_REVIEW_RUBRIC
    )


def test_text_selection_skips_a_figure_only_record(
    project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    figure_only_id = "run-figure-only"
    pointer = dict(project["record"])
    pointer["run_id"] = figure_only_id
    pointer["manifest_path"] = str(
        project["home"] / "manifests" / f"{figure_only_id}.md"
    )
    crew._write_json(crew.pointer_path(figure_only_id), pointer)
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": figure_only_id,
            "review_run_id": "figure-only-review",
            "reviewed_base_sha": project["head_sha"],
            "reviewed_head_sha": project["head_sha"],
            "rubric": review_module.FIGURE_REVIEW_RUBRIC,
            "status": "parsed",
            "rubric_items": dict.fromkeys(review_module.FIGURE_REVIEW_ITEMS, "pass"),
            "findings": [],
        }
    )
    actual, _actual_other_head = review_subject.select_review_for_head(
        PROJECT, figure_only_id, project["head_sha"]
    )
    assert actual is None

    figure_only = {
        "reviewed_run_id": figure_only_id,
        "reviewed_head_sha": project["head_sha"],
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "status": "parsed",
    }
    monkeypatch.setattr(review_module, "read_review", lambda *a, **k: figure_only)

    selected, other_head = review_subject.select_review_for_head(
        PROJECT, figure_only_id, project["head_sha"]
    )

    assert selected is None
    assert other_head == ""


def test_in_flight_review_reads_rubric_from_its_record_path(
    project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A run review declares its record inside the reviewed run's directory, so
    # the fixture composes that directory from the record the matcher reads.
    record_directory = runs.run_dir(str(project["record"]["run_id"]))
    head = project["head_sha"]
    pointer = {
        "run_id": "review-run",
        "project": PROJECT,
        "role": "review",
        "node": {
            "id": "hand-named-figure",
            "role": "review",
            "write_paths": review_module.run_review_record_paths(
                record_directory, RUN_ID, reviewed_head_sha=head
            ),
        },
    }
    monkeypatch.setattr(recovery, "list_live", lambda **_kwargs: [pointer])

    assert recovery._review_in_flight(project["record"], rubric="figure") == ""
    assert recovery._review_in_flight(project["record"]) == "review-run"

    pointer["node"]["id"] = "hand-named-review"
    pointer["node"]["write_paths"] = review_module.run_review_record_paths(
        record_directory,
        RUN_ID,
        reviewed_head_sha=head,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )
    assert (
        recovery._review_in_flight(
            project["record"], rubric=review_module.FIGURE_REVIEW_RUBRIC
        )
        == "review-run"
    )
