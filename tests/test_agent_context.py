import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon.agent_context import ContextRequest, build_context_manifest
from reckon.cli import main


def _home(tmp_path: Path, *, entrypoint: str = "link") -> Path:
    home = tmp_path / "home"
    canonical = home / ".agents" / "AGENTS.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("canonical policy\n")
    codex = home / ".codex"
    codex.mkdir()
    if entrypoint == "link":
        (codex / "AGENTS.md").symlink_to(canonical)
    elif entrypoint == "copy":
        (codex / "AGENTS.md").write_text(canonical.read_text())
    elif entrypoint == "conflict":
        (codex / "AGENTS.md").write_text("independent policy\n")
    return home


def _request(home: Path, target: Path, **kwargs) -> ContextRequest:
    return ContextRequest(target=target, user_home=home, **kwargs)


def _init_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_home_target_without_repository_has_only_user_context(tmp_path):
    home = _home(tmp_path)

    manifest = build_context_manifest(_request(home, home))

    assert manifest["ok"]
    assert manifest["repository"]["root"] is None
    assert manifest["instructions"]["project_chain"] == []
    assert manifest["entrypoint"]["relationship"] == "canonical-link"
    assert len(manifest["instructions"]["effective_chain"]) == 1


def test_empty_git_directory_in_home_ancestor_is_not_a_repository(tmp_path):
    ancestor = tmp_path / "not-a-repository"
    (ancestor / ".git").mkdir(parents=True)
    home = _home(ancestor)

    manifest = build_context_manifest(_request(home, home))

    assert manifest["ok"]
    assert manifest["repository"]["root"] is None
    assert manifest["instructions"]["project_chain"] == []


def test_repository_root_instruction_is_manifested(tmp_path):
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    _init_repository(repo)
    instruction = repo / "AGENTS.md"
    instruction.write_text("repository policy\n")

    manifest = build_context_manifest(_request(home, repo))

    assert manifest["ok"]
    assert manifest["repository"]["root"] == str(repo)
    assert [item["path"] for item in manifest["instructions"]["project_chain"]] == [
        str(instruction)
    ]
    assert manifest["budget"]["project_bytes"] == len(instruction.read_bytes())


def test_nested_target_includes_root_to_target_chain(tmp_path):
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    target = repo / "physics" / "gpu"
    target.mkdir(parents=True)
    _init_repository(repo)
    root_instruction = repo / "AGENTS.md"
    nested_instruction = repo / "physics" / "AGENTS.override.md"
    root_instruction.write_text("root policy\n")
    nested_instruction.write_text("nested policy\n")

    manifest = build_context_manifest(_request(home, target))

    assert [item["path"] for item in manifest["instructions"]["project_chain"]] == [
        str(root_instruction),
        str(nested_instruction),
    ]
    assert all(item["sha256"] for item in manifest["instructions"]["project_chain"])


def test_missing_entrypoint_is_a_hard_failure(tmp_path):
    home = _home(tmp_path, entrypoint="missing")

    manifest = build_context_manifest(_request(home, home))

    assert not manifest["ok"]
    assert {item["code"] for item in manifest["findings"]} == {
        "agent_entrypoint_missing"
    }


def test_conflicting_independent_entrypoint_is_split_brain(tmp_path):
    home = _home(tmp_path, entrypoint="conflict")

    manifest = build_context_manifest(_request(home, home))

    assert not manifest["ok"]
    assert manifest["entrypoint"]["relationship"] == "conflicting-copy"
    assert any(
        item["code"] == "entrypoint_split_brain"
        for item in manifest["findings"]
    )


def test_unreadable_entrypoint_is_a_hard_failure(tmp_path):
    home = _home(tmp_path, entrypoint="copy")
    entrypoint = home / ".codex" / "AGENTS.md"
    entrypoint.chmod(0)

    manifest = build_context_manifest(_request(home, home))

    assert not manifest["ok"]
    assert manifest["entrypoint"]["readable"] is False
    assert any(
        item["code"] == "agent_entrypoint_unreadable"
        for item in manifest["findings"]
    )


def test_independent_identical_copy_is_reported_as_staleness_risk(tmp_path):
    home = _home(tmp_path, entrypoint="copy")

    manifest = build_context_manifest(_request(home, home))

    assert manifest["ok"]
    assert manifest["entrypoint"]["relationship"] == "identical-copy"
    assert any(
        item["code"] == "entrypoint_independent_copy"
        and item["severity"] == "warning"
        for item in manifest["findings"]
    )


def test_nested_instruction_budget_overflow_is_a_hard_failure(tmp_path):
    home = _home(tmp_path)
    repo = tmp_path / "repo"
    target = repo / "nested"
    target.mkdir(parents=True)
    _init_repository(repo)
    (repo / "AGENTS.md").write_text("123456")
    (target / "AGENTS.md").write_text("789")

    manifest = build_context_manifest(
        _request(home, target, project_doc_max_bytes=8)
    )

    assert not manifest["ok"]
    assert manifest["budget"] == {
        "limit_bytes": 8,
        "source": "override",
        "project_bytes": 9,
        "remaining_bytes": 0,
        "overflow_bytes": 1,
        "truncation_risk": True,
    }
    assert any(
        item["code"] == "project_instruction_budget_exceeded"
        for item in manifest["findings"]
    )


def test_config_budget_and_fallback_instruction_name_are_used(tmp_path):
    home = _home(tmp_path)
    (home / ".codex" / "config.toml").write_text(
        'project_doc_max_bytes = 123\n'
        'project_doc_fallback_filenames = ["CONTEXT.md"]\n'
    )
    repo = tmp_path / "repo"
    _init_repository(repo)
    fallback = repo / "CONTEXT.md"
    fallback.write_text("fallback policy\n")

    manifest = build_context_manifest(_request(home, repo))

    assert manifest["budget"]["limit_bytes"] == 123
    assert manifest["budget"]["source"] == str(home / ".codex" / "config.toml")
    assert manifest["instructions"]["project_chain"][0]["path"] == str(fallback)


def test_skill_metadata_and_activated_body_are_reported_without_content(tmp_path):
    home = _home(tmp_path)
    skill = home / ".agents" / "skills" / "solver" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: solver\n"
        "description: Solve bounded problems.\n"
        "---\n"
        "SECRET-BODY-CONTENT\n"
    )

    manifest = build_context_manifest(
        _request(home, home, activated_skills=("solver",))
    )

    assert manifest["ok"]
    metadata = manifest["skills"]["discovered"]
    assert metadata[0]["name"] == "solver"
    assert metadata[0]["metadata_only"] is True
    assert manifest["skills"]["activated_bodies"] == [
        {
            "name": "solver",
            "path": str(skill),
            "resolved_path": str(skill),
            "bytes": len(skill.read_bytes()),
            "sha256": metadata[0]["sha256"],
        }
    ]
    assert "SECRET-BODY-CONTENT" not in json.dumps(manifest)


def test_json_cli_is_deterministic_and_exits_nonzero_on_hard_failure(tmp_path):
    home = _home(tmp_path, entrypoint="missing")
    runner = CliRunner()
    args = [
        "agent-context",
        "doctor",
        "--target",
        str(home),
        "--user-home",
        str(home),
        "--json",
    ]

    first = runner.invoke(main, args)
    second = runner.invoke(main, args)

    assert first.exit_code == 1
    assert first.output == second.output
    payload = json.loads(first.output)
    assert payload["ok"] is False
    assert payload["entrypoint"]["path"] == str(home / ".codex" / "AGENTS.md")


def test_human_cli_reports_context_summary(tmp_path):
    home = _home(tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "agent-context",
            "doctor",
            "--target",
            str(home),
            "--user-home",
            str(home),
        ],
    )

    assert result.exit_code == 0
    assert "agent context: PASS (codex)" in result.output
    assert "[canonical-link]" in result.output
    assert "skills:" in result.output
