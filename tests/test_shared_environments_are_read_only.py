"""Environment symlink targets stay sealed while a run keeps its write roots."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from reckon import _worker_fence as fence
from reckon.crew import fleet_supervisor


@pytest.fixture
def decoys(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    registry = home / ".config/reckon/mounts.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RECKON_HOME", str(registry.parent))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry))
    monkeypatch.delenv("UV_PYTHON_INSTALL_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    node_tmp = tmp_path / "node-tmp"
    node_tmp.mkdir()
    monkeypatch.setattr(fence, "node_tmp_seal_root", lambda: node_tmp)
    monkeypatch.setattr(fence, "create_node_tmp_runtime_paths", list)
    worktree = home / "Code/.reckon-worktrees/own"
    run = home / ".config/reckon/crew/runs/own"
    scratch = tmp_path / "scratch/own"
    for path in (worktree, run, scratch):
        path.mkdir(parents=True)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch.parent))
    if os.environ.get("RECKON_TEST_UNSEALED_INSTALL_TREE") == "1":
        original = fence._named_protected_paths
        install = home / ".local/share/uv/python"
        monkeypatch.setattr(
            fence,
            "_named_protected_paths",
            lambda home=None: [path for path in original(home) if path != install],
        )
    return home, registry, worktree, run, scratch


def _chain(decoys, *, registered):
    home, registry, worktree, _, _ = decoys
    repo = home / "elsewhere/checkout"
    environment = home / "environments/shared"
    install = home / ".local/share/uv/python"
    target = (
        home / "external/bin" if registered else install / "runtime/bin"
    ) / "python"
    repo.mkdir(parents=True)
    (repo / "docs").mkdir()
    (environment / "bin").mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\nprintf 'decoy-executes\\n'\n")
    target.chmod(0o755)
    (repo / ".venv").symlink_to(environment)
    (worktree / ".venv").symlink_to(environment)
    (environment / "bin/python").symlink_to("python3")
    (environment / "bin/python3").symlink_to(target)
    (environment / "package.py").write_text("sentinel\n")
    if registered:
        registry.write_text(json.dumps({"fixture": str(repo / "docs")}))
    return repo, environment, target


def _identity(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def _replay(decoys, destination):
    home, _, worktree, run, scratch = decoys
    wrapper = run / "gate.sh"
    wrapper.write_text(
        '#!/bin/sh\nlog="$3"\nprintf "replacement gate log\\n" > "$log"\n'
    )
    argv = fence.fence_argv(
        ["/bin/sh", str(wrapper), "gate", "command", str(destination)],
        worktree=worktree,
        manifest_path=run / "manifest.md",
        writable_directories=[scratch],
        home=home,
    )
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=20)


@pytest.mark.parametrize(
    "registered", [False, True], ids=["uv-install", "registered-external"]
)
def test_redirect_through_three_links_refuses_and_preserves_decoy(decoys, registered):
    _, _, worktree, run, scratch = decoys
    _, environment, target = _chain(decoys, registered=registered)
    before = _identity(target)
    result = _replay(decoys, worktree / ".venv/bin/python")
    print(
        f"redirect refusal ({registered=}): {result.returncode}: {result.stderr.strip()}"
    )
    assert result.returncode != 0, "redirect replaced the decoy"
    assert "Read-only file system" in result.stderr
    assert _identity(target) == before
    print(f"preserved size, mtime_ns, sha256: {before}")
    execution = subprocess.run(
        [str(target)], capture_output=True, text=True, check=True
    )
    assert execution.stdout == "decoy-executes\n"
    if registered:
        package = environment / "package.py"
        package_before = _identity(package)
        assert _replay(decoys, package).returncode != 0
        assert _identity(package) == package_before
    for destination in (
        run / "fresh.log",
        worktree / "result.log",
        scratch / "result.log",
    ):
        written = _replay(decoys, destination)
        assert written.returncode == 0, written.stderr
        assert destination.read_text() == "replacement gate log\n"


def test_install_tree_removed_from_sealed_roots_replaces_decoy(decoys, monkeypatch):
    home, _, worktree, _, _ = decoys
    _, _, target = _chain(decoys, registered=False)
    before = _identity(target)
    original = fence._named_protected_paths
    install = home / ".local/share/uv/python"
    monkeypatch.setattr(
        fence,
        "_named_protected_paths",
        lambda home=None: [path for path in original(home) if path != install],
    )
    result = _replay(decoys, worktree / ".venv/bin/python")
    assert result.returncode == 0, result.stderr
    assert _identity(target) != before
    assert target.read_text() == "replacement gate log\n"
    print(f"unsealed decoy changed: {before} -> {_identity(target)}")


def test_environment_roots_follow_the_shared_resolver(decoys, monkeypatch):
    _, _, _, _, _ = decoys
    repo, environment, target = _chain(decoys, registered=True)
    alternate = environment.parent / "alternate"
    (alternate / "bin").mkdir(parents=True)
    (alternate / "bin/python").symlink_to(target)
    calls = []

    def resolve(repository):
        calls.append(repository)
        return alternate

    monkeypatch.setattr(fleet_supervisor, "resolved_environment", resolve)
    protected = fence.declared_protected_paths(decoys[0])
    assert alternate in protected
    assert target in protected
    assert calls and set(calls) == {repo}


@pytest.mark.parametrize("setting", ["UV_PYTHON_INSTALL_DIR", "XDG_DATA_HOME"])
def test_configured_install_tree_is_sealed(decoys, monkeypatch, setting):
    home, _, _, _, _ = decoys
    configured = home / "configured-install"
    monkeypatch.setenv(setting, str(configured))
    install = (
        configured if setting == "UV_PYTHON_INSTALL_DIR" else configured / "uv/python"
    )
    target = install / "bin/python"
    target.parent.mkdir(parents=True)
    target.write_text("decoy\n")
    before = _identity(target)
    result = _replay(decoys, target)
    assert result.returncode != 0, result.stderr
    assert "Read-only file system" in result.stderr
    assert _identity(target) == before


def test_registered_environment_retargeting_is_seen_on_next_composition(decoys):
    repo, environment, _ = _chain(decoys, registered=True)
    first = fence.declared_protected_paths(decoys[0])
    replacement = environment.parent / "replacement"
    replacement.mkdir()
    (repo / ".venv").unlink()
    (repo / ".venv").symlink_to(replacement)
    second = fence.declared_protected_paths(decoys[0])
    assert environment in first
    assert replacement in second
    assert environment not in second


def test_unreadable_registry_refuses_instead_of_omitting_environments(decoys):
    from reckon.flight import FlightConfigError

    _, registry, _, _, _ = decoys
    registry.write_text("{")
    with pytest.raises(FlightConfigError, match="readable JSON"):
        fence.fence_argv(["true"], home=decoys[0])
