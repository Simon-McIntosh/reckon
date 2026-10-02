"""A dispatch dry run shows how each --set override resolved.

A --set changes the resolved flight configuration for one dispatch, and the dry
run used to echo nothing about it: ``backends.codex.budget_group=null`` gave no
sign that the null took effect, so the only way to confirm an override was to
spend a real dispatch. These cases drive the command end to end with a host
flight layer in a temporary RECKON_HOME, so the values asserted are the ones the
configuration resolution produced rather than a fixture's restatement of them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew

DONE_WHEN = "the command reports each --set path resolved beside its prior value"

#: A done-when keeping an unsubstituted placeholder, which the contract
#: validation refuses — the case reaches the exit-2 refusal through the real
#: resolution that runs before it rather than by faking a verdict.
PLACEHOLDER_DONE_WHEN = "pytest reports <the-measure> passing"

LANE_GATE_REASON = "the engine relaunch is in progress"


def _host_flight(*, codex_gate_document: Path | None = None) -> str:
    """The host layer: one backend, one role, two fences.

    ``codex_gate_document`` points the backend at a lane gate file, which is
    how the held-lane case withholds the dispatch; every other case declares
    no gate and the backend proceeds.
    """
    gate = (
        f"    gate_document: {codex_gate_document}\n"
        if codex_gate_document is not None
        else ""
    )
    return (
        "default_backend: codex\n"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: codex-fixture\n"
        "    model: fixture-model\n"
        "    effort: high\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "    budget_group: waves\n"
        f"{gate}"
        "roles:\n"
        "  implement:\n"
        "    backend: codex\n"
        "    execution_capable: true\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "fences:\n"
        "  time_budget: 20m\n"
        "  needs_help_after_failures: 2\n"
    )


def _write_flight(home: Path, *, codex_gate_document: Path | None = None) -> None:
    """Write the host layer into a case's temporary RECKON_HOME."""
    (home / "flight.yaml").write_text(
        _host_flight(codex_gate_document=codex_gate_document), encoding="utf-8"
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "target.py"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    _write_flight(home)
    return root


def _arguments(
    repo: Path, *overrides: str, done_when: str = DONE_WHEN, dry_run: bool = True
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "sample",
        "--plan",
        "fixture",
        "--section",
        "guard",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "candidate",
        "--goal",
        "record one dry-run override resolution",
        "--done-when",
        done_when,
        "--write-path",
        "target.py",
        "--session",
        "override-session",
        "--repo",
        str(repo),
        "--no-watch",
    ]
    if dry_run:
        arguments.append("--dry-run")
    for override in overrides:
        arguments.extend(["--set", override])
    return arguments


def test_dry_run_reports_each_set_override_resolved_beside_its_prior_value(
    home: Path, repo: Path
) -> None:
    """The done-when case: both --set paths are echoed with before and resolved."""
    result = CliRunner().invoke(
        cli_module.main,
        _arguments(
            repo,
            "backends.codex.budget_group=null",
            "fences.needs_help_after_failures=4",
        ),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["dry_run"] is True
    assert payload["overrides"] == {
        "backends.codex.budget_group": {"before": "waves", "resolved": None},
        "fences.needs_help_after_failures": {"before": 2, "resolved": 4},
    }
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_dry_run_refuses_a_set_path_the_configuration_does_not_know(
    home: Path, repo: Path
) -> None:
    """A misspelled backend name is refused, naming the path, not echoed."""
    result = CliRunner().invoke(
        cli_module.main, _arguments(repo, "backends.ghost.budget_group=null")
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["dry_run"] is True
    assert payload["error"] == "request-error"
    assert "backends.ghost.budget_group" in payload["detail"]
    assert "ghost" in payload["detail"]
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_dry_run_without_overrides_carries_no_resolution_block(
    home: Path, repo: Path
) -> None:
    """No --set means no block: the key appears only when it has something to report."""
    result = CliRunner().invoke(cli_module.main, _arguments(repo))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert "overrides" not in payload


def test_a_launch_refuses_an_undeclared_backend_name_the_preview_refuses(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview and the launch answer one phantom name with one refusal.

    An ephemeral backend defined entirely through --set is a name no layer
    defines, and both paths must refuse it rather than merge a section nothing
    routes to: the preview is not a stricter gate than the launch it previews.
    The launch is proved never to reach dispatch by the stub below — were the
    refusal absent the stub's failure would be the run's only trace — and by
    the empty live-pointer set.
    """
    overrides = (
        "backends.ephemeral.launch=cli",
        "backends.ephemeral.command=codex-fixture",
    )

    def never_dispatch(**_kwargs: object) -> object:
        raise AssertionError("an undeclared backend name must refuse before dispatch")

    monkeypatch.setattr(crew, "dispatch", never_dispatch)

    preview = CliRunner().invoke(cli_module.main, _arguments(repo, *overrides))
    assert preview.exit_code == 1, preview.output
    preview_payload = json.loads(preview.output)
    assert preview_payload["error"] == "request-error"
    assert "backends.ephemeral" in preview_payload["detail"]

    launch = CliRunner().invoke(
        cli_module.main, _arguments(repo, *overrides, dry_run=False)
    )
    assert launch.exit_code == 1, launch.output + launch.stderr
    assert "backends.ephemeral" in launch.output + launch.stderr
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_a_contract_refusal_carries_each_set_resolution(home: Path, repo: Path) -> None:
    """A refused contract still echoes how the --set overrides resolved."""
    result = CliRunner().invoke(
        cli_module.main,
        _arguments(
            repo,
            "backends.codex.budget_group=null",
            done_when=PLACEHOLDER_DONE_WHEN,
        ),
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "contract-validation"
    assert payload["overrides"] == {
        "backends.codex.budget_group": {"before": "waves", "resolved": None}
    }
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_a_held_lane_refusal_carries_each_set_resolution(
    home: Path, repo: Path
) -> None:
    """A lane held by its gate still echoes how the --set overrides resolved.

    The refusal tells a caller the dispatch waits on the lane, and the
    override echo tells it what the wait was asked to run: without the echo the
    two dry-run refusals — an override that never resolved and a resolved
    override on a held lane — are the same document.
    """
    gate = home.parent / "router-gate.json"
    gate.write_text(
        json.dumps({"paused": True, "reason": LANE_GATE_REASON}), encoding="utf-8"
    )
    _write_flight(home, codex_gate_document=gate)

    result = CliRunner().invoke(
        cli_module.main, _arguments(repo, "backends.codex.budget_group=null")
    )

    assert result.exit_code == 75, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "lane-paused"
    assert payload["overrides"] == {
        "backends.codex.budget_group": {"before": "waves", "resolved": None}
    }
    assert not list(crew.list_live(project="sample")), "nothing may be created"


@pytest.mark.parametrize(
    ("override", "path"),
    [
        ("fences.ghost_key=1", "fences.ghost_key"),
        ("backends.codex.ghost_sub=1", "backends.codex.ghost_sub"),
    ],
)
def test_a_schema_refused_override_answers_on_the_dry_run_channel(
    home: Path, repo: Path, override: str, path: str
) -> None:
    """Every dry-run request error answers as a decodable refusal.

    A leaf key the schema does not carry and a keyed-map name no layer defines
    are one request error each; both must reach a caller that reads the dry
    run's JSON rather than one arriving as plain text on stderr.
    """
    result = CliRunner().invoke(cli_module.main, _arguments(repo, override))

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["dry_run"] is True
    assert payload["error"] == "request-error"
    assert path in payload["detail"]
    assert not list(crew.list_live(project="sample")), "nothing may be created"
