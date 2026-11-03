"""The health-review remedy a dispatch prints is one that runs as printed.

A dispatch payload's ``health_review`` block names a compose command when no
stored code review covers the node's neighbourhood. Naming the plan as that
command's subject only works when the plan's implementable sections declare
write paths: the composer resolves the plan to exactly those paths and refuses
one that resolves to none. A printed remedy that refuses when run is a refusal
moved one step later, so a plan that declares no write paths is replaced by the
write paths of the dispatch that printed the command. These cases take the
printed command verbatim, run it, and hold the review it composes to those
paths.

The negative control restores the plan-slug-only command, which is what the
first case's executed remedy then refuses.
"""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_dispatch_commands, flight
from tests import test_dispatch_names_its_backend as backend_tests

# The package re-exports a ``dispatch`` function, so the module is resolved by
# name: this test stubs the launch the composed review performs.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
SESSION = "session"
PRODUCT_PATH = "alpha/mod.py"
OTHER_PRODUCT_PATH = "alpha/other.py"


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def _plan_html(*, slug: str, write_paths: str) -> str:
    """A plan with one implementable section and no other declared content.

    ``write_paths`` is the section's "Write paths" sentence, or an empty string
    for a plan that declares none — the shape whose remedy this module tests.
    """
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-section-declarations" content="{&quot;s1&quot;:&quot;implementable&quot;}">'
        "</head><body>"
        '<section data-reckon="section" data-id="s1" data-effort-hours="1.0"'
        ' data-capability-version="1.0" data-capability-class="general"'
        ' data-capability-reasoning="standard"'
        ' data-capability-verification="standard" data-capability-risk="low"'
        ' data-status="implementable"></section>'
        f'<h2 id="s1">§1 — Work</h2><p>Build the thing. {write_paths}</p>'
        "</body></html>\n"
    )


@pytest.fixture
def review_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A mounted project holding a plan, the modules it would build, and no review."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    package = root / "alpha"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "plan-a.html").write_text(
        _plan_html(slug="plan-a", write_paths=""), encoding="utf-8"
    )
    (plans / "plan-b.html").write_text(
        _plan_html(slug="plan-b", write_paths=f"Write paths: {OTHER_PRODUCT_PATH}."),
        encoding="utf-8",
    )
    fleet_scripts = root / "skills" / "reckon-build" / "scripts"
    fleet_scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (fleet_scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _dispatch_arguments(
    repo: Path, *, plan: str, write_paths: list[str], node: str
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        plan,
        "--section",
        "s1",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        node,
        "--goal",
        "print a review remedy that runs",
        "--done-when",
        "pytest reports the printed remedy composed a review of its paths",
        "--session",
        SESSION,
        "--repo",
        str(repo),
        "--backend",
        "alpha",
        "--dry-run",
    ]
    for path in write_paths:
        arguments.extend(["--write-path", path])
    return arguments


def _printed_compose_command(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: str,
    write_paths: list[str],
    node: str,
) -> tuple[str, dict]:
    """Dry-run one dispatch and return the compose command its payload names."""
    monkeypatch.setattr(
        crew_dispatch_commands,
        "_resolved_flight",
        lambda *_a, **_k: backend_tests.CONFIG,
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    backend_tests._observe_window(monkeypatch)
    result = CliRunner().invoke(
        cli_module.main,
        _dispatch_arguments(repo, plan=plan, write_paths=write_paths, node=node),
    )
    payload = json.loads(result.output.splitlines()[0])
    assert result.exit_code == 0, result.output
    block = payload["health_review"]
    assert block is not None and block["compose_command"]
    return block["compose_command"], payload


def _run_verbatim(command: str, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Run a printed compose command as printed, capturing what it dispatched.

    The dispatch itself is recorded rather than launched: what this node asserts
    is that the printed command resolves a subject and composes a review of the
    paths it names, which is the step that refused before the remedy named them.
    """
    dispatched: dict = {}

    def _record(**kwargs):
        dispatched.update(kwargs)
        return {"ok": True, "dispatched": True, "run_id": "r-remedy"}

    monkeypatch.setattr(dispatch_module, "dispatch", _record)
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *_a, **_k: SimpleNamespace(config=backend_tests.CONFIG),
    )
    argv = shlex.split(command)
    result = CliRunner().invoke(cli_module.main, argv[1:])
    return result, dispatched, argv


def test_a_plan_without_write_paths_prints_a_remedy_that_runs(
    review_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command, _payload = _printed_compose_command(
        review_repo,
        monkeypatch,
        plan="plan-a",
        write_paths=[PRODUCT_PATH],
        node="no-plan-paths",
    )

    result, dispatched, argv = _run_verbatim(command, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "--path" in argv
    assert argv[argv.index("--path") + 1] == PRODUCT_PATH
    payload = json.loads(result.output.splitlines()[0])
    assert PRODUCT_PATH in payload["brief_text"]
    assert PRODUCT_PATH in dispatched["node"].goal


def test_a_plan_with_write_paths_keeps_the_plan_as_the_subject(
    review_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command, _payload = _printed_compose_command(
        review_repo,
        monkeypatch,
        plan="plan-b",
        write_paths=[PRODUCT_PATH],
        node="plan-paths",
    )

    argv = shlex.split(command)

    assert argv[argv.index("--around") + 1] == "plan-b"
    assert "--path" not in argv
