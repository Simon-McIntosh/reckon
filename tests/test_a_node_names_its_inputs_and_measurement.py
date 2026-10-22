"""A node names its inputs and what its result is checked against.

A dispatch decides whether the tree the node will run in holds what the node
says it reads, and whether a node building a model in a project that declares
where its models live has said what its result is compared against. Both checks
need the dispatched repository and the project's flight config, so they run in
admission. A declared input the repository lacks refuses the dispatch; a
model-building node that names no comparison is admitted with the gap recorded
as a warning, so the lint does not cost the fit it was meant to save.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import TaskNode
from reckon.crew import dispatch_admission as admission
from reckon.crew.prescription import judge_prescribed

CONFIG = {
    "default_backend": "primary",
    "local_backend": "primary",
    "backends": {
        "primary": {
            "launch": "cli",
            "command": "claude",
            "model": "fixture-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

MODEL_PATHS_LAYER = """
domain:
  model_paths:
    - models/**
"""

# A project declaring no model path at all, which is the control: nothing a
# node beneath it writes is ever linted.
NO_MODEL_PATHS_LAYER = """
domain:
  invariants:
    - id: dc-limit
      rule: A divider's limit behaviour is stated at DC and at high frequency.
      applies_to:
        - lib/**/*.py
"""

BRIEF_TEXT = (
    "Fit the tape model and record the measurement its result is checked\n"
    "against, so the next reader can tell the fit from a guess.\n"
)

DONE_WHEN = "the gate command pytest exits 0 and prints the failure delta"


@pytest.fixture(autouse=True)
def isolated_host_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every resolution off this workstation's real host layer."""
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


def write_project_layer(root: Path, project: str, text: str) -> Path:
    """Write a project flight layer where the default resolution finds it."""
    path = root / "docs" / "state" / project / "flight.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _node(**overrides) -> TaskNode:
    fields = {
        "id": "fit-the-model",
        "goal": "fit the tape model",
        "plan": "",
        "role": "implement",
        "spec_level": "exact",
        "done_when": DONE_WHEN,
        "write_paths": ["models/tape.py"],
        "time_budget": "20m",
    }
    fields.update(overrides)
    return TaskNode(**fields)


# ── inputs: the tree the node will read is asked for what it holds ──────────


def test_a_missing_input_is_refused_naming_the_path(tmp_path: Path) -> None:
    """A declared input the repository lacks is named in the refusal."""
    repository = tmp_path / "repo"
    (repository / "data").mkdir(parents=True)
    (repository / "data" / "held.txt").write_text("held\n", encoding="utf-8")

    finding = admission.node_inputs_finding(
        _node(inputs=["data/held.txt", "data/absent.h5"]), repository=repository
    )

    assert finding is not None
    assert finding["property"] == "inputs"
    assert "data/absent.h5" in finding["detail"]
    # The input the repository does hold is not named as missing.
    assert "data/held.txt" not in finding["detail"]


def test_the_same_node_is_admitted_once_the_input_exists(tmp_path: Path) -> None:
    """The check reads the tree, so the file's presence admits the node."""
    repository = tmp_path / "repo"
    (repository / "data").mkdir(parents=True)
    node = _node(inputs=["data/held.txt"])

    assert admission.node_inputs_finding(node, repository=repository) is not None, (
        "the check must see an absence before it is asked about, or it proves nothing"
    )

    (repository / "data" / "held.txt").write_text("held\n", encoding="utf-8")

    assert admission.node_inputs_finding(node, repository=repository) is None


def test_an_absolute_input_resolves_outside_the_repository(tmp_path: Path) -> None:
    """An absolute input is asked as written rather than under the root."""
    repository = tmp_path / "repo"
    repository.mkdir()
    shared = tmp_path / "shared" / "held.txt"
    shared.parent.mkdir()
    shared.write_text("held\n", encoding="utf-8")

    assert (
        admission.node_inputs_finding(
            _node(inputs=[str(shared)]), repository=repository
        )
        is None
    )


# ── compares_with: a model-building node names its comparison ───────────────


def test_a_model_node_naming_no_comparison_is_linted(tmp_path: Path) -> None:
    """A node writing a declared model path with no comparison is linted."""
    project_path = write_project_layer(tmp_path, "fixture", MODEL_PATHS_LAYER)

    finding = admission.node_measurement_finding(
        _node(write_paths=["models/tape.py"]),
        project="fixture",
        project_path=project_path,
    )

    assert finding is not None
    assert finding["property"] == "compares-with"
    # A lint, not a refusal: the dispatch admits this node and records the gap.
    assert finding["severity"] == "warning"
    assert "--compares-with" in finding["detail"]
    assert "models/**" in finding["detail"]


def test_a_missing_input_is_a_refusal_where_the_comparison_is_a_warning(
    tmp_path: Path,
) -> None:
    """The split is what the section asks for: one refuses, the other warns."""
    repository = tmp_path / "repo"
    repository.mkdir()
    project_path = write_project_layer(repository, "fixture", MODEL_PATHS_LAYER)

    findings = admission.admission_domain_findings(
        _node(write_paths=["models/tape.py"], inputs=["data/absent.h5"]),
        project="fixture",
        repository=repository,
        project_path=project_path,
    )
    refusals, warnings = admission.admission_refusals_and_warnings(findings)

    assert [finding["property"] for finding in refusals] == ["inputs"]
    assert len(warnings) == 1
    assert "--compares-with" in warnings[0]


@pytest.mark.parametrize(
    "declared",
    ["the 2026-08 prototype report", "none-exists: searched docs/ for a measurement"],
)
def test_a_model_node_stating_a_comparison_is_not_linted(
    tmp_path: Path, declared: str
) -> None:
    """Either stated form clears the lint."""
    project_path = write_project_layer(tmp_path, "fixture", MODEL_PATHS_LAYER)

    assert (
        admission.node_measurement_finding(
            _node(write_paths=["models/tape.py"], compares_with=declared),
            project="fixture",
            project_path=project_path,
        )
        is None
    )


def test_a_node_writing_outside_the_model_paths_is_not_linted(tmp_path: Path) -> None:
    """The match is on the node's write paths, not on the project alone."""
    project_path = write_project_layer(tmp_path, "fixture", MODEL_PATHS_LAYER)

    assert (
        admission.node_measurement_finding(
            _node(write_paths=["lib/converters/divider.py"]),
            project="fixture",
            project_path=project_path,
        )
        is None
    )


def test_a_project_declaring_no_model_paths_is_never_linted(tmp_path: Path) -> None:
    """The control: without the declaration nothing is linted."""
    project_path = write_project_layer(tmp_path, "fixture", NO_MODEL_PATHS_LAYER)

    assert (
        admission.node_measurement_finding(
            _node(write_paths=["models/tape.py"]),
            project="fixture",
            project_path=project_path,
        )
        is None
    )


def test_both_checks_answer_from_one_resolved_authority(tmp_path: Path) -> None:
    """A missing input and an unstated comparison are reported together."""
    repository = tmp_path / "repo"
    repository.mkdir()
    project_path = write_project_layer(repository, "fixture", MODEL_PATHS_LAYER)

    findings = admission.admission_domain_findings(
        _node(write_paths=["models/tape.py"], inputs=["data/absent.h5"]),
        project="fixture",
        repository=repository,
        project_path=project_path,
    )

    assert [finding["property"] for finding in findings] == [
        "inputs",
        "compares-with",
    ]


def test_the_authority_names_the_project(tmp_path: Path) -> None:
    """The project the checks read comes from the resolved authority."""
    assert (
        admission.authority_project(
            {
                "plan": {"project": "fixture"},
                "write": {"projects": ["fixture"], "repository": str(tmp_path)},
            }
        )
        == "fixture"
    )
    assert admission.authority_project({}) == ""


# ── the dispatch itself: the dry run reports both with the findings ─────────


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fixture repository registered as a project, with a model path declared."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "models").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    write_project_layer(root, "proj", MODEL_PATHS_LAYER)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "seed.txt", "skills", "docs"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: seed fixture"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    return root, brief


def _invoke(repo: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]):
    from reckon import crew_dispatch_commands

    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return CliRunner().invoke(cli_module.main, arguments)


def _dry_run_arguments(
    repo: Path,
    brief: Path,
    *,
    inputs: tuple[str, ...] = (),
    compares_with: str | None = None,
) -> list[str]:
    """The dispatch arguments for one fixture node, its checks stated or not."""
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--brief",
        str(brief),
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "fit-the-model",
        "--goal",
        "the node fits the tape model",
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "models/tape.py",
        "--time-budget",
        "20m",
        "--session",
        "session-fit-the-model",
        "--repo",
        str(repo),
    ]
    for entry in inputs:
        arguments += ["--input", entry]
    if compares_with is not None:
        arguments += ["--compares-with", compares_with]
    return [*arguments, "--dry-run"]


def test_the_dry_run_refuses_the_missing_input_and_warns_the_comparison(
    dispatch_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing input refuses; the unnamed comparison warns instead."""
    repo, brief = dispatch_repo
    result = _invoke(
        repo,
        monkeypatch,
        _dry_run_arguments(repo, brief, inputs=["data/absent.h5"]),
    )

    payload = json.loads(result.output.splitlines()[0])
    findings = payload["validation"]["findings"]
    properties = [finding["property"] for finding in findings]

    assert "inputs" in properties, result.output
    # The comparison is admitted, so it is not on the refusal channel at all.
    assert "compares-with" not in properties, result.output
    assert payload["ok"] is False
    assert any("data/absent.h5" in finding["detail"] for finding in findings)
    # The lint is still reported, on the warning channel the caller reads.
    assert any("--compares-with" in warning for warning in payload["warnings"]), (
        result.output
    )


def test_the_dry_run_admits_a_model_node_and_warns_the_comparison(
    dispatch_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unnamed comparison does not stop the dispatch; it is recorded."""
    repo, brief = dispatch_repo
    result = _invoke(repo, monkeypatch, _dry_run_arguments(repo, brief))

    payload = json.loads(result.output.splitlines()[0])
    properties = [finding["property"] for finding in payload["validation"]["findings"]]

    assert payload["ok"] is True, result.output
    assert payload["validation"]["ok"] is True, result.output
    assert "compares-with" not in properties, result.output
    assert "inputs" not in properties, result.output
    assert any("--compares-with" in warning for warning in payload["warnings"]), (
        result.output
    )


def test_a_node_rebuilt_from_its_stored_record_reports_the_same_warning(
    dispatch_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning is derived from the recorded node, not held in a cache.

    The dispatch stores the node definition it admitted, and the warning has to
    be reproducible from that record rather than from admission state a later
    reader does not have.
    """
    repo, brief = dispatch_repo
    result = _invoke(repo, monkeypatch, _dry_run_arguments(repo, brief))

    payload = json.loads(result.output.splitlines()[0])
    warning = next(w for w in payload["warnings"] if "--compares-with" in w)

    rebuilt = TaskNode(**payload["node"])
    rebuilt_findings = admission.admission_domain_findings(
        rebuilt,
        project="proj",
        repository=repo,
        project_path=repo / "docs" / "state" / "proj" / "flight.yaml",
    )
    _, rebuilt_warnings = admission.admission_refusals_and_warnings(rebuilt_findings)

    assert rebuilt_warnings == [warning], result.output


def test_the_dry_run_admits_a_node_that_answers_both_checks(
    dispatch_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node with its input present and a stated comparison is not refused."""
    repo, brief = dispatch_repo
    (repo / "data").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "held.h5").write_text("held\n", encoding="utf-8")

    result = _invoke(
        repo,
        monkeypatch,
        _dry_run_arguments(
            repo,
            brief,
            inputs=["data/held.h5"],
            compares_with="the 2026-08 prototype report",
        ),
    )

    payload = json.loads(result.output.splitlines()[0])
    properties = [finding["property"] for finding in payload["validation"]["findings"]]

    assert "inputs" not in properties, result.output
    assert "compares-with" not in properties, result.output
    assert not any("--compares-with" in warning for warning in payload["warnings"]), (
        result.output
    )


# ── the prescribed judgement is untouched ──────────────────────────────────


def test_judge_prescribed_still_judges_a_synthesised_node() -> None:
    """The judgement reads the node alone, with no repository in hand."""
    verdict = judge_prescribed(_node())

    assert set(verdict) == {"prescribed", "failures", "detail"}
    assert verdict["prescribed"] is False
    assert "one-artifact" not in verdict["failures"]
