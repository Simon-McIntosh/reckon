"""Duration overrides have explicit, shared bounds at their call sites."""

import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew_follow_commands, serve
from reckon.crew import dispatch_launch, runs
from reckon.env_duration import read_duration_override
from reckon.hooks import coordinator_obligations


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 7.0),
        ("  ", 7.0),
        ("invalid", 7.0),
        ("-1", 7.0),
        ("nan", 7.0),
        ("inf", 7.0),
        ("2.5", 2.5),
        ("0", 0.0),
    ],
)
def test_duration_override_uses_its_bound(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("RECKON_DURATION_PROBE", raising=False)
    else:
        monkeypatch.setenv("RECKON_DURATION_PROBE", raw)
    assert read_duration_override("RECKON_DURATION_PROBE", 7.0, 0, True) == expected


def test_exclusive_bound_rejects_zero(monkeypatch):
    monkeypatch.setenv("RECKON_DURATION_PROBE", "0")
    assert read_duration_override("RECKON_DURATION_PROBE", 7.0, 0, False) == 7.0


@pytest.mark.parametrize(
    ("name", "getter", "default"),
    [
        (
            dispatch_launch.TERMINAL_MANIFEST_GRACE_ENV,
            dispatch_launch._terminal_manifest_grace_seconds,
            dispatch_launch.TERMINAL_MANIFEST_GRACE_DEFAULT,
        ),
        (
            dispatch_launch.STOP_GRACE_ENV,
            dispatch_launch._stop_grace_seconds,
            dispatch_launch.STOP_GRACE_DEFAULT,
        ),
        (
            coordinator_obligations.STOP_DERIVATION_BUDGET_ENV,
            coordinator_obligations.stop_derivation_budget,
            coordinator_obligations.STOP_DERIVATION_BUDGET_SECONDS,
        ),
        (
            coordinator_obligations.PRODUCER_LEASE_ENV,
            coordinator_obligations.producer_lease_seconds,
            coordinator_obligations.PRODUCER_LEASE_SECONDS,
        ),
        (
            crew_follow_commands.FOLLOWER_SETTLE_GRACE_ENV,
            crew_follow_commands._settle_grace_default,
            crew_follow_commands.FOLLOWER_SETTLE_GRACE_SECONDS,
        ),
        (
            runs.PRODUCER_LEASE_ENV,
            runs.producer_lease_seconds,
            runs.DEFAULT_PRODUCER_LEASE_SECONDS,
        ),
        (
            "RECKON_CROSS_NODE_FAST_S",
            serve._cross_node_fast_s,
            serve._CROSS_NODE_FAST_S,
        ),
        (
            "RECKON_CROSS_NODE_SLOW_S",
            serve._cross_node_slow_s,
            serve._CROSS_NODE_SLOW_S,
        ),
        (
            "RECKON_CROSS_NODE_READ_WINDOW_S",
            serve._cross_node_read_window_s,
            serve._CROSS_NODE_READ_WINDOW_S,
        ),
        (
            "RECKON_DISCOVERY_REUSE_S",
            serve._served_signature_ttl,
            serve._SERVED_SIGNATURE_TTL_S,
        ),
    ],
)
def test_negative_duration_uses_each_site_default(monkeypatch, name, getter, default):
    monkeypatch.setenv(name, "-1")
    assert getter() == default


def test_zero_lease_uses_default_and_zero_stop_grace_is_accepted(monkeypatch):
    monkeypatch.setenv(runs.PRODUCER_LEASE_ENV, "0")
    assert runs.producer_lease_seconds() == runs.DEFAULT_PRODUCER_LEASE_SECONDS
    assert coordinator_obligations.producer_lease_seconds() == (
        coordinator_obligations.PRODUCER_LEASE_SECONDS
    )
    monkeypatch.setenv(dispatch_launch.STOP_GRACE_ENV, "0")
    assert dispatch_launch._stop_grace_seconds() == 0.0


def test_hook_bootstrap_precedes_duration_import(tmp_path):
    hooks = tmp_path / "checkout" / "reckon" / "hooks"
    hooks.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "reckon" / "hooks"
    for name in ("coordinator_obligations.py", "interpreter_bootstrap.py"):
        (hooks / name).write_bytes((source / name).read_bytes())

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("RECKON_HOOK_REEXEC_ATTEMPT", None)
    environment["RECKON_HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            str(hooks / "coordinator_obligations.py"),
            "--hook",
            "prompt",
        ],
        input="{}",
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=15,
    )

    assert result.returncode != 0
    assert "checkout interpreter is missing" in result.stdout + result.stderr
