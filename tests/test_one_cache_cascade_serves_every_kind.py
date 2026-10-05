"""Cache precedence and git error domains have one owner per mechanism."""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _store, capabilities, clones, ledger, serve, velocity
from reckon.crew import routing
from reckon.crew.node import CrewError
from reckon.crew.picker import lane_context

_KINDS = (
    ("velocity", "RECKON_VELOCITY_CACHE", "velocity", False),
    ("clones", "RECKON_CLONE_CACHE", "clones", False),
    ("pick-input", "RECKON_PICK_CACHE", "", True),
    ("run-time-profile", "RECKON_RUN_TIME_PROFILE_CACHE", "run-time-profile", True),
    ("client", "RECKON_CLIENT_CACHE", "client", False),
)


@pytest.fixture
def cache_environment(tmp_path, monkeypatch):
    for name in ("RECKON_HOME", "XDG_CACHE_HOME", *(row[1] for row in _KINDS)):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    return tmp_path


def _consumer_root(kind, monkeypatch):
    if kind == "velocity":
        return velocity.velocity_cache_root()
    if kind == "pick-input":
        return capabilities.pick_input_cache_path("sample").parent
    if kind == "run-time-profile":
        return lane_context._profile_cache_root()
    if kind == "client":
        return serve._client_cache_root()
    roots = []

    def corpus_directory(root, _repo):
        roots.append(root)
        return root / "corpus"

    monkeypatch.setattr(clones, "_corpus_directory", corpus_directory)
    clones.clone_matches({}, changed_paths=[])
    return roots.pop()


@pytest.mark.parametrize("kind,variable,leaf,home_first", _KINDS)
@pytest.mark.parametrize("scenario", ["default", "reckon", "xdg", "both", "configured"])
def test_each_kind_preserves_its_directory_through_the_shared_cascade(
    cache_environment, monkeypatch, kind, variable, leaf, home_first, scenario
):
    root = cache_environment
    expected = root / "user" / ".cache" / "reckon" / leaf
    if scenario in {"reckon", "both", "configured"}:
        monkeypatch.setenv("RECKON_HOME", str(root / "isolated"))
        if kind != "client":
            expected = root / "isolated" / "cache" / leaf
    if scenario in {"xdg", "both", "configured"}:
        monkeypatch.setenv("XDG_CACHE_HOME", str(root / "xdg"))
        if scenario == "xdg" or not home_first:
            expected = root / "xdg" / "reckon" / leaf
    if scenario == "configured":
        monkeypatch.setenv(variable, str(root / "configured"))
        expected = root / "configured"

    calls = []
    resolve = _store.cache_root

    def observed(kind, override=None):
        answer = resolve(kind, override)
        calls.append((kind, override, answer))
        return answer

    monkeypatch.setattr(_store, "cache_root", observed)
    assert _consumer_root(kind, monkeypatch) == expected
    assert calls == [(kind, None, expected)]
    assert resolve(kind, root / "override") == root / "override"


@pytest.mark.parametrize("kind,variable,leaf,home_first", _KINDS)
def test_explicit_override_wins_over_every_environment_choice(
    cache_environment, monkeypatch, kind, variable, leaf, home_first
):
    root = cache_environment
    for name in ("RECKON_HOME", "XDG_CACHE_HOME", variable):
        monkeypatch.setenv(name, str(root / name))
    assert _store.cache_root(kind, "relative-override") == Path("relative-override")


@pytest.mark.parametrize("variable", ["RECKON_HOME", "XDG_CACHE_HOME"])
@pytest.mark.parametrize("override", [None, "explicit"])
def test_unknown_kind_is_refused_even_with_an_override(
    cache_environment, monkeypatch, variable, override
):
    monkeypatch.setenv(variable, str(cache_environment / "isolated"))
    with pytest.raises(ValueError, match="unknown cache kind"):
        _store.cache_root("unrecognised", override)


def test_ledger_and_explicit_picker_and_detector_roots_reach_the_owner(
    cache_environment, monkeypatch
):
    root = cache_environment / "override"
    calls = []

    def resolve(kind, override=None):
        calls.append((kind, override))
        return root

    monkeypatch.setattr(_store, "cache_root", resolve)
    assert (
        capabilities.pick_input_cache_path("sample", root=root)
        == root / "picker-sample.json"
    )
    assert ledger._run_index_path("sample", cache_environment).parent == root
    seen = []
    monkeypatch.setattr(
        clones, "_corpus_directory", lambda base, repo: seen.append(base) or base
    )
    clones.clone_matches({}, changed_paths=[], cache_root=root)
    assert seen == [root]
    assert calls == [("pick-input", root), ("pick-input", None), ("clones", root)]


def test_cache_environment_reads_have_one_owner():
    package = Path(_store.__file__).parent
    owners = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text())
        owners.extend(
            (path.name, node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(item, ast.Constant) and item.value == "XDG_CACHE_HOME"
                for item in ast.walk(node)
            )
        )
    assert owners == [("_store.py", "cache_root")]


def test_git_failure_returns_bytes_and_checked_wrappers_keep_their_error_domains(
    tmp_path,
):
    result = velocity.run_git(tmp_path, "rev-parse", "HEAD")
    assert result.returncode != 0
    assert isinstance(result.stdout, bytes)
    assert b"not a git repository" in result.stderr
    with pytest.raises(subprocess.CalledProcessError) as error:
        velocity.git(tmp_path, "rev-parse", "HEAD")
    assert error.value.returncode == result.returncode
    assert error.value.stderr == result.stderr
    unchecked = routing._git(tmp_path, "rev-parse", "HEAD", check=False)
    assert isinstance(unchecked.stdout, str)
    assert unchecked.stderr == result.stderr.decode()
    with pytest.raises(CrewError, match="not a git repository"):
        routing._git(tmp_path, "rev-parse", "HEAD")


def test_git_payload_and_timeout_reach_one_invocation(tmp_path, monkeypatch):
    calls = []
    invoke = subprocess.run

    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return invoke(*args, **kwargs)

    monkeypatch.setattr(velocity.subprocess, "run", observed)
    result = velocity.run_git(tmp_path, "hash-object", "--stdin", input=b"payload\n")
    assert result.returncode == 0
    assert len(result.stdout.strip()) == 40
    assert len(calls) == 1
    assert calls[0][1]["input"] == b"payload\n"
    assert calls[0][1]["timeout"] == 180
    assert calls[0][1]["check"] is False


def test_routing_decodes_the_shared_process_with_text_newlines(tmp_path, monkeypatch):
    calls = []

    def run(repo, *args):
        calls.append((repo, args))
        return subprocess.CompletedProcess(args, 0, b"answer\r\nnext\r", b"detail\r\n")

    monkeypatch.setattr(velocity, "run_git", run)
    result = routing._git(tmp_path, "status", check=False)
    assert calls == [(tmp_path, ("status",))]
    assert result.returncode == 0
    assert result.stdout == "answer\nnext\n"
    assert result.stderr == "detail\n"


def test_velocity_invokes_processes_only_in_the_runner():
    tree = ast.parse(inspect.getsource(velocity))
    owners = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "subprocess"
        and call.func.attr in {"run", "check_output", "check_call", "Popen"}
    }
    assert owners == {"run_git"}


def test_velocity_import_does_not_pull_in_crew():
    tree = Path(velocity.__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import reckon.velocity; assert not any(n == 'reckon.crew' or n.startswith('reckon.crew.') for n in sys.modules)",
        ],
        cwd=tree,
        env={**os.environ, "PYTHONPATH": str(tree)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
