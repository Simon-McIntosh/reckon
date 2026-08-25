"""The true Git creation map survives process-local cache loss safely."""

from __future__ import annotations

import json
import subprocess

import pytest

from reckon import serve


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "reckon-home"))
    serve._GIT_CREATION_CACHE.clear()
    yield
    serve._GIT_CREATION_CACHE.clear()


@pytest.fixture
def repository(tmp_path):
    repo_dir = tmp_path / "repository"
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    return repo_dir, docs_dir


def _git_runner(monkeypatch: pytest.MonkeyPatch, state: dict, commands: list):
    def run(args, **kwargs):
        commands.append(list(args))
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, state["head"] + "\n", "")
        range_arg = next((arg for arg in args if ".." in arg), None)
        output = state["ranges"].get(range_arg, state["full"])
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(serve.subprocess, "run", run)


def test_unchanged_head_after_restart_uses_persisted_exact_dates(
    repository, monkeypatch
):
    repo_dir, docs_dir = repository
    commands: list[list[str]] = []
    state = {
        "head": "stable-head",
        "full": (
            "COMMIT 200\ndocs/plans/recent.html\n"
            "COMMIT 100\ndocs/plans/established.html\n"
        ),
        "ranges": {},
    }
    _git_runner(monkeypatch, state, commands)

    built = serve._git_first_committed(repo_dir, docs_dir)
    assert built == {
        "docs/plans/recent.html": 200,
        "docs/plans/established.html": 100,
    }
    assert serve._git_creation_cache_path(
        (str(repo_dir.resolve()), "docs")
    ).is_file()

    serve._GIT_CREATION_CACHE.clear()
    commands.clear()
    restarted = serve._git_first_committed(repo_dir, docs_dir)

    assert restarted == built
    assert commands == [["git", "rev-parse", "HEAD"]]


def test_moved_head_after_restart_queries_only_the_new_range(repository, monkeypatch):
    repo_dir, docs_dir = repository
    commands: list[list[str]] = []
    state = {
        "head": "old-head",
        "full": "COMMIT 100\ndocs/plans/established.html\n",
        "ranges": {
            "old-head..new-head": "COMMIT 200\ndocs/plans/added.html\n"
        },
    }
    _git_runner(monkeypatch, state, commands)
    assert serve._git_first_committed(repo_dir, docs_dir) == {
        "docs/plans/established.html": 100
    }

    serve._GIT_CREATION_CACHE.clear()
    commands.clear()
    state["head"] = "new-head"
    refreshed = serve._git_first_committed(repo_dir, docs_dir)

    assert refreshed == {
        "docs/plans/established.html": 100,
        "docs/plans/added.html": 200,
    }
    history_calls = [args for args in commands if args[1] == "log"]
    assert len(history_calls) == 1
    assert "old-head..new-head" in history_calls[0]


@pytest.mark.parametrize(
    "corrupt",
    [
        "{\"schema\":",
        json.dumps({"schema": "some.other.cache", "version": 1}),
    ],
)
def test_partial_or_foreign_cache_is_rebuilt(
    repository, monkeypatch, corrupt, caplog
):
    repo_dir, docs_dir = repository
    commands: list[list[str]] = []
    state = {
        "head": "stable-head",
        "full": "COMMIT 100\ndocs/plans/established.html\n",
        "ranges": {},
    }
    _git_runner(monkeypatch, state, commands)
    expected = serve._git_first_committed(repo_dir, docs_dir)
    cache_path = serve._git_creation_cache_path((str(repo_dir.resolve()), "docs"))

    cache_path.write_text(corrupt)
    serve._GIT_CREATION_CACHE.clear()
    commands.clear()
    rebuilt = serve._git_first_committed(repo_dir, docs_dir)

    assert rebuilt == expected
    assert len([args for args in commands if args[1] == "log"]) == 1
    assert "Ignoring" in caplog.text


def test_checksum_mismatch_cannot_serve_wrong_dates(repository, monkeypatch):
    repo_dir, docs_dir = repository
    commands: list[list[str]] = []
    state = {
        "head": "stable-head",
        "full": "COMMIT 100\ndocs/plans/established.html\n",
        "ranges": {},
    }
    _git_runner(monkeypatch, state, commands)
    expected = serve._git_first_committed(repo_dir, docs_dir)
    cache_path = serve._git_creation_cache_path((str(repo_dir.resolve()), "docs"))
    payload = json.loads(cache_path.read_text())
    payload["times"]["docs/plans/established.html"] = 999
    cache_path.write_text(json.dumps(payload))

    serve._GIT_CREATION_CACHE.clear()
    commands.clear()
    assert serve._git_first_committed(repo_dir, docs_dir) == expected
    assert len([args for args in commands if args[1] == "log"]) == 1
