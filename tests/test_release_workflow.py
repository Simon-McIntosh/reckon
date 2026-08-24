from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def steps_for(job: dict) -> list[dict]:
    return job["steps"]


def test_release_triggers_are_limited_to_version_tags_and_manual_dry_runs() -> None:
    workflow = load_workflow()
    trigger = workflow.get("on", workflow.get(True))

    assert set(trigger) == {"push", "workflow_dispatch"}
    assert set(trigger["push"]) == {"tags"}
    assert trigger["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]


def test_manual_trigger_defaults_to_test_index_only() -> None:
    workflow = load_workflow()
    trigger = workflow.get("on", workflow.get(True))

    assert trigger["workflow_dispatch"] == {
        "inputs": {
            "testpypi-only": {
                "description": "Stop after publishing to TestPyPI",
                "required": True,
                "type": "boolean",
                "default": True,
            }
        }
    }


def test_manual_dry_run_cannot_reach_production_publish() -> None:
    production = load_workflow()["jobs"]["publish-pypi"]

    assert production["if"] == (
        "${{ github.event_name == 'push' && inputs.testpypi-only != true }}"
    )


def test_build_has_full_version_history_and_both_distribution_formats() -> None:
    build = load_workflow()["jobs"]["build"]
    checkout = next(
        step for step in steps_for(build) if step.get("uses") == "actions/checkout@v4"
    )
    commands = [step["run"] for step in steps_for(build) if "run" in step]

    assert checkout["with"]["fetch-depth"] == 0
    assert "uv build --out-dir dist" in commands
    assert any(
        step.get("uses") == "actions/upload-artifact@v4"
        and step["with"]["path"] == "dist/"
        for step in steps_for(build)
    )


def test_publish_jobs_use_named_oidc_environments() -> None:
    jobs = load_workflow()["jobs"]

    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish-testpypi"]["environment"]["name"] == "testpypi"
    assert jobs["publish-pypi"]["environment"]["name"] == "pypi"
    assert jobs["publish-testpypi"]["permissions"] == {"id-token": "write"}
    assert jobs["publish-pypi"]["permissions"] == {"id-token": "write"}


def test_test_index_publish_completes_before_production_publish() -> None:
    jobs = load_workflow()["jobs"]
    test_job = jobs["publish-testpypi"]
    production_job = jobs["publish-pypi"]
    test_publish = next(
        step
        for step in steps_for(test_job)
        if step.get("uses") == "pypa/gh-action-pypi-publish@release/v1"
    )
    production_publish = next(
        step
        for step in steps_for(production_job)
        if step.get("uses") == "pypa/gh-action-pypi-publish@release/v1"
    )

    assert test_job["needs"] == "build"
    assert production_job["needs"] == "publish-testpypi"
    assert test_publish["with"]["repository-url"] == "https://test.pypi.org/legacy/"
    assert "with" not in production_publish


def test_trusted_publishers_receive_the_same_built_artifact() -> None:
    jobs = load_workflow()["jobs"]
    for job_name in ("publish-testpypi", "publish-pypi"):
        download = next(
            step
            for step in steps_for(jobs[job_name])
            if step.get("uses") == "actions/download-artifact@v4"
        )
        assert download["with"] == {
            "name": "python-package-distributions",
            "path": "dist/",
        }


def test_workflow_has_no_repository_token_credentials() -> None:
    text = WORKFLOW_PATH.read_text().lower()
    workflow = load_workflow()

    assert "secrets." not in text
    assert "api_token" not in text
    assert "api-token" not in text
    assert "password" not in yaml.safe_dump(workflow).lower()
