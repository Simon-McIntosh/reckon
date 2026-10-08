"""The shadow and dry-run pick paths carry the dispatch authority.

The production dispatch hands its resolved authority into the picker, but the
dry-run preview and the deferred shadow pick reached it only through a fallback
that swallowed a resolution failure, so a granted landing fragment was charged
on those two paths with nothing saying why. These tests drive both paths and
assert each supplies a resolved authority to the pick, and that a resolution
that fails is reported on the selection rather than swallowed.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_picker as dispatch_picker_module
from reckon import cli, crew_dispatch_commands
from reckon.crew.node import TaskNode

dispatch = importlib.import_module("reckon.crew.dispatch")

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        name: {
            "launch": "in-harness",
            "model": f"{name}-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
        for name in ("alpha", "beta")
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    scripts = root / "skills" / "reckon-build" / "scripts"
    plans.mkdir(parents=True)
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / source.name).write_text(source.read_text())
    (plans / "example.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="example"></head>'
        '<body><h2 id="dispatch">Dispatch</h2></body></html>'
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/example.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return root


def _selection(action="route", backend="beta", **extra):
    return {
        "action": action,
        "backend": backend,
        "family": "test",
        "model": "beta-model",
        "effort": "medium",
        "probabilities": {"beta": 0.9},
        "confidence": 0.9,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "latency_ms": 1.0,
        "excluded": [],
    } | extra


def _invoke(repo, *, route=True, dry_run=True):
    args = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "example",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "pick-paths-carry-authority-test",
        "--goal",
        "carry the authority",
        "--done-when",
        "pytest checks the authority reached the pick",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--repo",
        str(repo),
        "--no-watch",
    ]
    if route:
        args += ["--route", "picker"]
    if dry_run:
        args += ["--dry-run"]
    return CliRunner().invoke(cli.main, args), args


def test_the_dry_run_pick_passes_the_dispatch_authority(repo, monkeypatch):
    """The dry-run preview resolves and hands over the authority.

    Dropping the authority here (the declared negative control) leaves the spy
    without it, so the figure the preview reports falls back to the fallback's
    silent resolution and this assertion reddens.
    """
    seen = {}

    def spy(*, node, config, project, repo, **kwargs):
        seen["authority"] = kwargs.get("authority")
        seen["authority_error"] = kwargs.get("authority_error")
        return _selection("fallback", None, fallback_reason="preview")

    monkeypatch.setattr(dispatch, "dispatch_picker_selection", spy)
    result, _ = _invoke(repo, route=True, dry_run=True)
    assert seen.get("authority") is not None, result.output
    assert seen.get("authority")["plan"]["project"] == "proj"
    assert seen.get("authority_error") is None


def test_the_shadow_pick_passes_the_dispatch_authority(repo, monkeypatch):
    """The deferred shadow pick resolves and hands over the authority."""
    seen = {}

    def spy(*, node, config, project, repo, **kwargs):
        seen["authority"] = kwargs.get("authority")
        seen["authority_error"] = kwargs.get("authority_error")
        return _selection("fallback", None, fallback_reason="shadow")

    monkeypatch.setattr(dispatch_picker_module, "dispatch_picker_selection", spy)
    node = TaskNode(
        id="pick-paths-carry-authority-test",
        goal="carry the authority",
        plan="example",
        role="implement",
        spec_level="exact",
        done_when="pytest checks the authority reached the pick",
        write_paths=["result.json"],
    )
    spec_path = repo / "shadow-picker.json"
    spec_path.write_text(
        json.dumps(
            {
                "run_id": "r-shadow-test",
                "node": node.as_dict(),
                "config": CONFIG,
                "project": "proj",
                "repo": str(repo),
                "ledger_root": str(repo),
                "session": "session",
                "comment": "",
            }
        )
    )
    dispatch_picker_module._record_shadow_picker_selection(spec_path)
    assert seen.get("authority") is not None
    assert seen.get("authority")["plan"]["project"] == "proj"
    assert seen.get("authority_error") is None


def test_a_failed_authority_resolution_is_reported_not_swallowed(repo):
    """A resolution that fails rides on the selection rather than vanishing.

    The picker path resolves the authority through ``resolve_picker_authority``;
    a failure returns the reason beside a null authority, and the pick records
    it, so a reader who sees the granted fragment charged can see why.
    """
    node = TaskNode(
        id="pick-paths-carry-authority-test",
        goal="carry the authority",
        plan="example",
        role="implement",
        spec_level="exact",
        done_when="pytest checks the authority reached the pick",
        write_paths=["result.json"],
    )
    selection = dispatch_picker_module.dispatch_picker_selection(
        node=node,
        config=CONFIG,
        project="proj",
        repo=repo,
        authority=None,
        authority_error="CrewError: mounts unavailable",
    )
    assert selection.get("authority_error") == "CrewError: mounts unavailable"


def test_the_resolver_reports_its_failure_instead_of_raising(repo, monkeypatch):
    """The resolver never raises: a failure is a (None, reason) pair."""

    def boom(*_a, **_k):
        raise dispatch_picker_module.CrewError("mounts unavailable")

    monkeypatch.setattr(dispatch_picker_module, "resolve_dispatch_authority", boom)
    authority, error = dispatch_picker_module.resolve_picker_authority("proj", repo)
    assert authority is None
    assert error == "CrewError: mounts unavailable"
