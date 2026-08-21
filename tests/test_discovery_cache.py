"""Discovery cache behavior across local files, mounts, and Git history."""

import json
import logging
import os
import subprocess

import pytest

import reckon.serve as serve


@pytest.fixture(autouse=True)
def empty_discovery_caches():
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    yield
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()


@pytest.fixture()
def mounted_projects(tmp_path, monkeypatch):
    docs_by_project = {}
    mounts = {}
    for project in ("alpha", "beta"):
        docs_dir = tmp_path / project / "docs"
        (docs_dir / "plans").mkdir(parents=True)
        docs_by_project[project] = docs_dir
        mounts[project] = str(docs_dir)

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps(mounts))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    return docs_by_project


def _plan(project: str, slug: str, status: str, depends_on: str = "") -> str:
    dependency = (
        f'<meta name="plan-depends-on" content="{depends_on}">' if depends_on else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{project}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="{status}">
{dependency}
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _successful_git(monkeypatch, heads_by_repo):
    calls = []

    def run(args, **kwargs):
        calls.append(list(args))
        repo = str(kwargs["cwd"])
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, heads_by_repo[repo] + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(serve.subprocess, "run", run)
    return calls


def test_repeat_discovery_skips_git_and_metadata_for_all_mounts(
    mounted_projects, monkeypatch
):
    docs = mounted_projects
    (docs["beta"] / "plans" / "provider.html").write_text(
        _plan("beta", "provider", "shipped")
    )
    (docs["alpha"] / "plans" / "consumer.html").write_text(
        _plan("alpha", "consumer", "active", "beta:provider")
    )
    git_calls = _successful_git(
        monkeypatch,
        {
            str(docs["alpha"].parent): "alpha-head",
            str(docs["beta"].parent): "beta-head",
        },
    )
    original_parse = serve._plan_html.parse_meta
    parse_calls = []

    def counted_parse(path):
        parse_calls.append(path)
        return original_parse(path)

    monkeypatch.setattr(serve._plan_html, "parse_meta", counted_parse)
    first = {
        project: serve.discover_plans(project_docs, project, None)
        for project, project_docs in docs.items()
    }
    assert git_calls
    assert parse_calls

    git_calls.clear()
    parse_calls.clear()
    second = {
        project: serve.discover_plans(project_docs, project, None)
        for project, project_docs in docs.items()
    }

    assert second == first
    assert git_calls == []
    assert parse_calls == []


def test_external_project_change_invalidates_dependant(mounted_projects, monkeypatch):
    docs = mounted_projects
    provider = docs["beta"] / "plans" / "provider.html"
    provider.write_text(_plan("beta", "provider", "active"))
    (docs["alpha"] / "plans" / "consumer.html").write_text(
        _plan("alpha", "consumer", "active", "beta:provider")
    )
    _successful_git(
        monkeypatch,
        {
            str(docs["alpha"].parent): "alpha-head",
            str(docs["beta"].parent): "beta-head",
        },
    )

    first = serve.discover_plans(docs["alpha"], "alpha", None)
    first_consumer = next(
        item for item in first["inventory"] if item["slug"] == "consumer"
    )
    assert first_consumer["effective_status"] == "blocked"

    previous = provider.stat()
    provider.write_text(_plan("beta", "provider", "shipped"))
    os.utime(
        provider,
        ns=(
            previous.st_atime_ns,
            max(provider.stat().st_mtime_ns, previous.st_mtime_ns + 1),
        ),
    )

    second = serve.discover_plans(docs["alpha"], "alpha", None)
    second_consumer = next(
        item for item in second["inventory"] if item["slug"] == "consumer"
    )
    assert second is not first
    assert second_consumer["effective_status"] == "active"
    assert second_consumer["blocking"] == []


def test_git_creation_map_reuses_head_and_queries_only_new_commits(
    tmp_path, monkeypatch
):
    repo_dir = tmp_path / "repository"
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir(parents=True)
    heads = iter(("old-head", "old-head", "new-head"))
    commands = []

    def run(args, **kwargs):
        commands.append(list(args))
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, next(heads) + "\n", "")
        if "old-head..new-head" in args:
            output = "COMMIT 200\ndocs/plans/added.html\n"
        else:
            output = "COMMIT 100\ndocs/plans/existing.html\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(serve.subprocess, "run", run)

    assert serve._git_first_committed(repo_dir, docs_dir) == {
        "docs/plans/existing.html": 100
    }
    commands.clear()
    assert serve._git_first_committed(repo_dir, docs_dir) == {
        "docs/plans/existing.html": 100
    }
    assert commands == [["git", "rev-parse", "HEAD"]]

    commands.clear()
    assert serve._git_first_committed(repo_dir, docs_dir) == {
        "docs/plans/existing.html": 100,
        "docs/plans/added.html": 200,
    }
    assert len(commands) == 2
    assert "old-head..new-head" in commands[1]
    assert commands[1][-2:] == ["--", "docs"]


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_git_history_failure_logs_and_keeps_ctime_fallback(
    tmp_path, monkeypatch, caplog, failure
):
    repo_dir = tmp_path / "repository"
    docs_dir = repo_dir / "docs"
    plans_dir = docs_dir / "plans"
    plans_dir.mkdir(parents=True)
    plan_path = plans_dir / "sample.html"
    plan_path.write_text(_plan("sample", "sample", "active"))

    def run(args, **kwargs):
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "current-head\n", "")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 7, "", "history unavailable")

    monkeypatch.setattr(serve.subprocess, "run", run)
    with caplog.at_level(logging.WARNING, logger=serve.LOGGER.name):
        result = serve.discover_plans(docs_dir, "sample", None)

    stat = plan_path.stat()
    expected = int(getattr(stat, "st_birthtime", None) or stat.st_ctime)
    assert result["inventory"][0]["created"] == expected
    expected_log = "timed out" if failure == "timeout" else "failed with exit code 7"
    assert expected_log in caplog.text
