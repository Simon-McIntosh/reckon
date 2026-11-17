"""Interpreter permission checks are exercised only against disposable decoys."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import fleet_supervisor as supervisor
from reckon.crew.landing_measure import _link_environment
from tests.test_fleet_health import BASE, SCRIPT, sandbox

ROOT = Path(__file__).resolve().parents[1]
PROVISIONER = ROOT / "skills/reckon-build/scripts/worktree_fleet.py"
DECOY = b"#!/bin/sh\nprintf 'decoy-alive\\n'\n"


def provisioner():
    spec = importlib.util.spec_from_file_location("provisioner", PROVISIONER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decoy(home: Path, name: str, mode: int):
    repo = home / "Code" / name
    environment = home / "environments" / name
    target = home / "installed" / name
    repo.mkdir(parents=True)
    (repo / "docs").mkdir()
    (environment / "bin").mkdir(parents=True)
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(DECOY)
    target.chmod(mode)
    (repo / ".venv").symlink_to(environment)
    (environment / "bin/python").symlink_to("python3")
    (environment / "bin/python3").symlink_to(target)
    return repo, environment, target


@pytest.fixture
def decoys(tmp_path):
    return [decoy(tmp_path, "writable", 0o777), decoy(tmp_path, "sealed", 0o555)]


def identity(target):
    info = target.stat()
    return (
        info.st_size,
        info.st_mtime_ns,
        hashlib.sha256(target.read_bytes()).hexdigest(),
    )


def redirect(alias):
    return subprocess.run(
        ["bash", "-c", 'printf replacement > "$1"', "--", str(alias)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_shell_redirects_are_refused_and_decoys_still_execute(decoys, capsys):
    before = [identity(target) for _, _, target in decoys]
    assert (
        supervisor.check_environment_interpreters([repo for repo, _, _ in decoys]) == 0
    )
    repair_log = capsys.readouterr().out
    for index, (repo, _, target) in enumerate(decoys):
        result = redirect(repo / ".venv/bin/python")
        with capsys.disabled():
            print(
                f"redirect exit={result.returncode}: before={before[index]} "
                f"after={identity(target)} stderr={result.stderr.strip()}"
            )
        assert result.returncode != 0, "the redirect replaced the writable decoy"
        assert "Permission denied" in result.stderr
        assert identity(target) == before[index]
        assert target.stat().st_mode & 0o222 == 0
        executed = subprocess.run(
            [str(repo / ".venv/bin/python")],
            capture_output=True,
            check=True,
            timeout=10,
        )
        assert executed.stdout == b"decoy-alive\n"
    assert repair_log.count("interpreter made read-only:") == 1
    assert str(decoys[0][2]) in repair_log
    assert str(decoys[1][2]) not in repair_log
    assert "T" in repair_log and "Z]" in repair_log


def test_disabled_check_replaces_the_writable_decoy(decoys, monkeypatch):
    repo, _, target = decoys[0]
    before = identity(target)
    monkeypatch.setattr(supervisor, "check_environment_interpreters", lambda *args: 0)
    supervisor.check_environment_interpreters([repo])
    assert redirect(repo / ".venv/bin/python").returncode == 0
    assert target.read_bytes() == b"replacement"
    assert identity(target) != before


def test_provisioning_repairs_before_exposing_the_environment(decoys, tmp_path, capsys):
    repo, environment, target = decoys[0]
    tree = tmp_path / "worker"
    tree.mkdir()
    record = provisioner().provision_env_links(repo, tree)
    assert (tree / ".venv").resolve() == environment
    assert record[".venv"] == f"linked -> {environment}"
    assert target.stat().st_mode & 0o222 == 0
    assert redirect(tree / ".venv/bin/python").returncode != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(target) in captured.err


def test_provisioning_and_measurement_use_the_same_resolver(
    decoys, tmp_path, monkeypatch
):
    repo, _, _ = decoys[0]
    _, selected, target = decoys[1]
    calls = []

    def resolve(repository):
        calls.append(repository)
        return selected

    monkeypatch.setattr(supervisor, "resolved_environment", resolve)
    worker, arm = tmp_path / "worker", tmp_path / "arm"
    worker.mkdir()
    arm.mkdir()
    provisioner().provision_env_links(repo, worker)
    _link_environment(repo, arm)
    assert (worker / ".venv").resolve() == (arm / ".venv").resolve() == selected
    assert supervisor.environment_interpreters(repo) == (target,)
    assert calls and all(called == repo for called in calls)


def test_aliases_are_deduplicated_and_non_interpreters_are_untouched(decoys):
    repo, environment, target = decoys[0]
    helper = environment / "bin/python-config"
    helper.write_bytes(DECOY)
    helper.chmod(0o755)
    (environment / "bin/pypy").mkdir()
    (environment / "bin/pythonw").symlink_to("absent")
    assert supervisor.environment_interpreters(repo) == (target,)
    assert supervisor.check_environment_interpreters([repo, repo]) == 0
    assert helper.stat().st_mode & 0o222


def test_failed_repair_is_counted_and_provisioning_refuses(
    decoys, tmp_path, monkeypatch
):
    repo, _, target = decoys[0]
    original = Path.chmod

    def refuse(path, mode, **kwargs):
        if path == target:
            raise PermissionError("decoy repair refused")
        return original(path, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", refuse)
    records = []
    assert supervisor.check_environment_interpreters([repo], report=records.append) == 1
    assert "repair failed" in records[0] and str(target) in records[0]
    tree = tmp_path / "worker"
    tree.mkdir()
    module = provisioner()
    with pytest.raises(module.FleetError, match="remain writable"):
        module.provision_env_links(repo, tree)
    assert not (tree / ".venv").exists()


def test_each_health_sample_repairs_a_later_install(tmp_path):
    box = sandbox(tmp_path, extra_env=BASE)
    home = Path(box.env["HOME"])
    first = decoy(home, "writable", 0o755)
    second = decoy(home, "sealed", 0o555)
    mounts = home / "mounts.json"
    mounts.write_text(
        json.dumps(
            {
                name: str(repo / "docs")
                for name, (repo, _, _) in zip(
                    ("one", "two"), (first, second), strict=True
                )
            }
        )
    )
    late = home / "installed/late"
    late_alias = first[1] / "bin/pypy"
    sleep = tmp_path / "stubbin/sleep"
    sleep.write_text(
        '#!/bin/bash\ncp "$LATE_SOURCE" "$LATE_TARGET"\n'
        'chmod 755 "$LATE_TARGET"\nln -s "$LATE_TARGET" "$LATE_ALIAS"\n'
    )
    sleep.chmod(0o755)
    env = {
        **box.env,
        "RECKON_MOUNTS_PATH": str(mounts),
        "RECKON_SHIM_PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT),
        "LATE_TARGET": str(late),
        "LATE_SOURCE": str(first[2]),
        "LATE_ALIAS": str(late_alias),
        "FLEET_SLEEP_SECONDS": "0",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "--passes", "2"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rows = box.samples()[0].read_text().splitlines()
    assert len(rows) == 3
    assert rows[0].split("\t")[-1] == "writable_interpreters"
    assert [row.split("\t")[-1] for row in rows[1:]] == ["0", "0"]
    for target in (first[2], second[2], late):
        assert target.stat().st_mode & 0o222 == 0
        assert redirect(target).returncode != 0
        assert subprocess.check_output([str(target)], timeout=10) == b"decoy-alive\n"
    assert result.stderr.count("interpreter made read-only:") == 2
    assert str(first[2]) in result.stderr and str(late) in result.stderr


def test_unreadable_registry_is_unknown_in_the_health_series(tmp_path):
    box = sandbox(tmp_path, extra_env=BASE)
    mounts = tmp_path / "mounts.json"
    mounts.write_text("{unreadable")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--once"],
        env={
            **box.env,
            "RECKON_MOUNTS_PATH": str(mounts),
            "RECKON_SHIM_PYTHON": sys.executable,
            "PYTHONPATH": str(ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    assert box.samples()[0].read_text().splitlines()[-1].split("\t")[-1] == "unknown"
    assert "interpreter permission check failed" in result.stderr
