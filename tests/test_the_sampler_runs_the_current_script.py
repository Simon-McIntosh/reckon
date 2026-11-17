"""Sampler handovers retain history and report missing publications truthfully."""

import csv
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from contextlib import suppress

import pytest

from reckon.crew import fleet_node
from reckon.crew import fleet_supervisor as supervisor


def _wait(read, *, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = read()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("sampler handover did not reach its expected state")


def _rows(path):
    if not path.exists():
        return []
    with path.open() as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def _script(path, *, added):
    columns = ["utc", "probe", *(["added"] if added else [])]
    values = ["now", "sample", *(["present"] if added else [])]
    code = (
        "from pathlib import Path; from reckon.crew import fleet_node as n; "
        f"import os; n._HEALTH_COLUMNS={columns!r}; "
        f"n.append_node_reading(Path(os.environ['SERIES']), {chr(9).join(values)!r})"
    )
    path.write_text(
        "#!/bin/bash\n"
        'exec 9>"$XDG_RUNTIME_DIR/fleet-health.lock"\n'
        "flock -n 9 || exit 0\n"
        'echo "start $$" >> "$EVENTS"\n'
        'trap \'echo "stop $$" >> "$EVENTS"; exit 0\' TERM\n'
        "while :; do\n"
        f"  {shlex.quote(sys.executable)} -c {shlex.quote(code)}\n"
        '  sleep "$FLEET_SLEEP_SECONDS" & wait $!\n'
        "done\n"
    )
    path.chmod(0o755)


def test_reexecution_replaces_the_script_and_keeps_earlier_samples(tmp_path):
    sampler = tmp_path / "sampler"
    series = tmp_path / "series.tsv"
    events = tmp_path / "events"
    result = tmp_path / "replacement.json"
    advance = tmp_path / "advance"
    period = 2
    env = {
        **os.environ,
        "FLEET_HEALTH_SAMPLER": str(sampler),
        "FLEET_SLEEP_SECONDS": str(period),
        "XDG_RUNTIME_DIR": str(tmp_path),
        "FLEET_RUNTIME_DIR": str(tmp_path),
        "SERIES": str(series),
        "EVENTS": str(events),
        "ADVANCE": str(advance),
        "RESULT": str(result),
    }
    _script(sampler, added=False)
    driver = tmp_path / "supervisor.py"
    driver.write_text(
        "import json, os, sys, time\nfrom pathlib import Path\n"
        "from reckon.crew.fleet_supervisor import start_health_sampler\n"
        "pid = start_health_sampler()\n"
        "if len(sys.argv) == 1:\n"
        "    while not Path(os.environ['ADVANCE']).exists(): time.sleep(.02)\n"
        "    os.execv(sys.executable, [sys.executable, __file__, 'reexecuted'])\n"
        "Path(os.environ['RESULT']).write_text(json.dumps({'pid': pid}))\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen([sys.executable, str(driver)], env=env)
    try:
        earlier = _wait(lambda: _rows(series))
        first = int(events.read_text().splitlines()[0].split()[1])
        _script(sampler, added=True)
        started = time.monotonic()
        advance.touch()
        _wait(result.exists)
        replacement = json.loads(result.read_text())["pid"]
        rows = _wait(
            lambda: (
                (rows if any(r.get("added") == "present" for r in rows) else [])
                if (rows := _rows(series))
                else []
            ),
            timeout=2 * period,
        )
        assert replacement != first
        assert time.monotonic() - started <= 2 * period
        assert rows[: len(earlier)] == [dict(row, added="-") for row in earlier]
        active = set()
        for event in events.read_text().splitlines():
            verb, pid = event.split()
            if verb == "start":
                active.add(pid)
                assert len(active) == 1, "two samplers ran during the handover"
            else:
                active.remove(pid)
        assert active == {str(replacement)}
        print(
            f"handover: one sampler; retained={len(earlier)}; added=present; "
            f"elapsed={time.monotonic() - started:.3f}s; period={period}s; missed<=2"
        )
    finally:
        process.terminate()
        process.wait(timeout=5)
        # Only the session leaders recorded by this case own these groups.
        pids = {int(line.split()[1]) for line in events.read_text().splitlines()}
        for pid in pids:
            with suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGTERM)


def test_schema_widening_keeps_rows_in_the_same_series(tmp_path):
    path = tmp_path / "health.tsv"
    columns = fleet_node._HEALTH_COLUMNS
    path.write_text("\t".join(columns[:16]) + "\n" + "\t".join(["kept"] * 16) + "\n")
    fleet_node.append_node_reading(path, "\t".join(["current"] * len(columns)))
    rows = _rows(path)
    assert len(rows) == 2
    assert rows[0] == {
        name: "kept" if i < 16 else "-" for i, name in enumerate(columns)
    }
    assert rows[1] == dict.fromkeys(columns, "current")
    assert list(tmp_path.glob("health.*.tsv")) == []


def test_missing_disk_sample_names_sampler_without_claiming_a_hold(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor, "fleet_for_project", lambda _: "fixture")
    (tmp_path / supervisor.RECORD_NAME).write_text(
        json.dumps({"node": "fixture-node", "job_id": "123"})
    )
    row = supervisor.node_disk_obligation("fixture")
    assert row["kind"] == "node-disk-missing"
    assert row["service"] == "fleet-health"
    assert "fleet-health" in row["reason"]
    assert "held" not in row["reason"].lower()
    assert "launch" not in row["next_command"].lower()


def test_an_unidentified_lock_owner_refuses_replacement(tmp_path, monkeypatch, capsys):
    signals = []
    monkeypatch.setattr(supervisor, "signal_worker", lambda *a, **kw: signals.append(a))
    monkeypatch.setattr(supervisor, "all_process_cmdlines", dict)
    with (tmp_path / "fleet-health.lock").open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert not supervisor._stop_health_sampler(tmp_path, "/unrelated/sampler")
    assert signals == []
    assert "replacement refused" in capsys.readouterr().out


def test_default_sampler_comes_from_the_checkout(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "fleet-health"
    script.parent.mkdir()
    script.touch()
    monkeypatch.setattr(supervisor, "_checkout_root", lambda: tmp_path)
    started = []

    def launch(argv, **kwargs):
        started.append((argv, kwargs))
        return type("Child", (), {"pid": 123})()

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    monkeypatch.setattr(
        supervisor.shutil, "which", lambda *a, **kw: "/stale/fleet-health"
    )
    assert supervisor.start_health_sampler({"XDG_RUNTIME_DIR": str(tmp_path)}) == 123
    assert started[0][0] == [str(script)]


@pytest.mark.parametrize("contents", ["unknown\nkept\n", "utc\tprobe\nshort\n"])
def test_an_unreadable_series_is_preserved_on_refusal(tmp_path, contents):
    path = tmp_path / "health.tsv"
    path.write_text(contents)
    with pytest.raises(fleet_node.FleetNodeError):
        fleet_node.append_node_reading(path)
    assert path.read_text() == contents
    assert set(tmp_path.iterdir()) == {path, path.with_name(path.name + ".lock")}
