"""Sampler handovers retain history and report missing publications truthfully."""

import csv
import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress

import pytest

from reckon.crew import fleet_node
from reckon.crew import fleet_supervisor as supervisor
from reckon.crew.admission import service

SAMPLE_PERIOD_SECONDS = 2
HANG_PERIODS = 40
HANG_SECONDS = SAMPLE_PERIOD_SECONDS * HANG_PERIODS


def _wait(read, *, timeout=HANG_SECONDS):
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


def _sampler_events(path):
    return [
        (verb, int(pid), int(slot))
        for verb, pid, slot in (line.split() for line in path.read_text().splitlines())
    ]


def _handover_counts(rows, events, first, replacement, earlier_count):
    """Measure delivery against slots emitted by the samplers' shared kernel clock."""
    before = [row for row in rows if int(row["sampler_pid"]) == first]
    after = [row for row in rows if int(row["sampler_pid"]) == replacement]
    assert before and after, "both sampler generations must publish a row"
    assert rows == before + after, "sampler generations must not interleave"
    delivery_rows = len(before) - earlier_count + 1
    assert 1 <= delivery_rows <= 2, "first replacement row exceeds two series samples"
    stop_slots = [slot for verb, pid, slot in events if (verb, pid) == ("stop", first)]
    assert len(stop_slots) == 1, "the previous sampler must emit its stop slot"
    first_slot = int(after[0]["slot"])
    elapsed_slots = first_slot - stop_slots[0]
    assert 0 <= elapsed_slots <= 2, (
        "first replacement sample exceeds two sampling slots"
    )
    occupied = {int(row["slot"]) for row in rows}
    missing = sorted(set(range(int(before[-1]["slot"]) + 1, first_slot)) - occupied)
    assert len(missing) <= 2, "handover missed more than two sampling slots"
    return {
        "rows_before": len(before),
        "rows_after": len(after),
        "delivery_rows": delivery_rows,
        "elapsed_slots": elapsed_slots,
        "missed_slots": missing,
    }


def _record_handover(directory, rows, events, first, replacement, earlier):
    record = {"rows": rows, "events": events, "earlier_rows": earlier}
    path = directory / "handover-measurement.json"
    path.write_text(json.dumps(record))
    counts = _handover_counts(rows, events, first, replacement, len(earlier))
    path.write_text(json.dumps({"counts": counts, **record}))
    return counts


def _script(path, *, added):
    columns = ["utc", "probe", "sampler_pid", "slot", *(["added"] if added else [])]
    code = (
        "from pathlib import Path; from reckon.crew import fleet_node as n; "
        "import os; "
        "slot=int(float(Path('/proc/uptime').read_text().split()[0])"
        "/int(os.environ['FLEET_SLEEP_SECONDS'])); "
        f"n._HEALTH_COLUMNS={columns!r}; "
        "values=['now','sample',os.environ['SAMPLER_PID'],str(slot)]"
        f"+{['present'] if added else []!r}; "
        "n.append_node_reading(Path(os.environ['SERIES']), '\\t'.join(values))"
    )
    path.write_text(
        "#!/bin/bash\n"
        'exec 9>"$XDG_RUNTIME_DIR/fleet-health.lock"\n'
        "flock -n 9 || exit 0\n"
        "export SAMPLER_PID=$$\n"
        "event() {\n"
        "  read -r sample_uptime _ < /proc/uptime\n"
        '  printf "%s %s %s\\n" "$1" "$$" '
        '"$(( ${sample_uptime%%.*} / FLEET_SLEEP_SECONDS ))" >> "$EVENTS"\n'
        "}\n"
        "event start\n"
        "trap 'event stop; exit 0' TERM\n"
        "while :; do\n"
        f"  {shlex.quote(sys.executable)} -c {shlex.quote(code)}\n"
        '  sleep "$FLEET_SLEEP_SECONDS" & wait $!\n'
        "done\n"
    )
    path.chmod(0o755)


@pytest.mark.parametrize("script_changes_path", [False, True])
@pytest.mark.parametrize("observer_delay", [0, 5])
def test_reexecution_replaces_the_script_and_keeps_earlier_samples(
    tmp_path, script_changes_path, observer_delay
):
    sampler = tmp_path / "sampler"
    series = tmp_path / "series.tsv"
    events = tmp_path / "events"
    result = tmp_path / "replacement.json"
    advance = tmp_path / "advance"
    period = SAMPLE_PERIOD_SECONDS
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
        "    os.environ['FLEET_HEALTH_SAMPLER'] = Path(os.environ['ADVANCE']).read_text()\n"
        "    os.execv(sys.executable, [sys.executable, __file__, 'reexecuted'])\n"
        "Path(os.environ['RESULT']).write_text(json.dumps({'pid': pid}))\n"
        f"time.sleep({2 * HANG_SECONDS})\n"
    )
    process = subprocess.Popen([sys.executable, str(driver)], env=env)
    try:
        earlier = _wait(lambda: _rows(series))
        first = _sampler_events(events)[0][1]
        replacement_script = (
            tmp_path / "replacement" if script_changes_path else sampler
        )
        _script(replacement_script, added=True)
        advance.write_text(str(replacement_script))
        _wait(result.exists)
        replacement = json.loads(result.read_text())["pid"]
        assert replacement is not None, "the runtime's sampler was not replaced"
        time.sleep(observer_delay)
        rows = _wait(
            lambda: (
                (rows if any(r.get("added") == "present" for r in rows) else [])
                if (rows := _rows(series))
                else []
            ),
        )
        assert replacement != first
        assert rows[: len(earlier)] == [dict(row, added="-") for row in earlier]
        recorded_events = _sampler_events(events)
        counts = _record_handover(
            tmp_path, rows, recorded_events, first, replacement, earlier
        )
        active = set()
        for verb, pid, _ in recorded_events:
            if verb == "start":
                active.add(pid)
                assert len(active) == 1, "two samplers ran during the handover"
            else:
                active.remove(pid)
        assert active == {replacement}
        print(
            f"handover: one sampler; retained={len(earlier)}; added=present; "
            f"counts={counts}; period={period}s; "
            f"changed_path={script_changes_path}; observer_delay={observer_delay}s"
        )
    finally:
        process.terminate()
        process.wait(timeout=HANG_SECONDS)
        # Only the session leaders recorded by this case own these groups.
        pids = {pid for _, pid, _ in _sampler_events(events)}
        for pid in pids:
            with suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGTERM)
            _wait(lambda pid=pid: not supervisor.process_cmdline(pid))


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
    monkeypatch.setattr(socket, "gethostname", lambda: "fixture-node")
    monkeypatch.setattr(supervisor, "node_disk_reserve_bytes", lambda: 0)
    monkeypatch.setenv(supervisor.NODE_DISK_ROOT_ENV, str(tmp_path))
    assert service.node_disk_admission_hold() is None
    row = supervisor.node_disk_obligation("fixture")
    assert row["kind"] == "node-disk-missing"
    assert row["service"] == "fleet-health"
    assert "fleet-health" in row["reason"]
    assert "held" not in row["reason"].lower()
    assert "launch" not in row["next_command"].lower()


def test_an_unidentified_lock_owner_refuses_replacement(tmp_path, monkeypatch, capsys):
    from reckon.crew import process_signal

    signals = []
    monkeypatch.setattr(
        process_signal, "_signal_one", lambda *a, **kw: signals.append(a)
    )
    monkeypatch.setattr(supervisor, "all_process_cmdlines", dict)
    with (tmp_path / "fleet-health.lock").open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert not supervisor._stop_health_sampler(tmp_path)
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


def test_sampling_without_interpreter_overrides_uses_the_project_environment(tmp_path):
    from tests.test_fleet_health import BASE, run_pass, sample_rows, sandbox

    box = sandbox(tmp_path, extra_env=BASE)
    (box.root / "proc" / "uptime").write_text("1000 0\n")
    box.env.pop("FLEET_DISK_PYTHON", None)
    box.env.pop("RECKON_SHIM_PYTHON", None)
    mounts = box.root / "mounts.json"
    mounts.write_text("{}")
    box.env.update(
        {
            "RECKON_MOUNTS_PATH": str(mounts),
            "FLEET_SESSION_TMP_ROOT": str(box.root / "scratch"),
            "FLEET_RUNTIME_DIR": str(box.root / "runtime"),
            "FLEET_NODE_TMP_ROOT": str(box.root),
        }
    )
    system_python = box.root / "stubbin" / "python3"
    attempted = box.root / "system-python-used"
    system_python.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> '
        + shlex.quote(str(attempted))
        + "\nexit 91\n"
    )
    system_python.chmod(0o755)
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    assert not attempted.exists(), attempted.read_text() if attempted.exists() else ""
    row = sample_rows(box)[0]
    assert row["session_scratch_bytes"] == "0", result.stderr
    assert row["writable_interpreters"] == "0", result.stderr
    assert row["test_trees"] == "0", result.stderr
    assert row["test_rss_bytes"] == "0", result.stderr
    sample = json.loads((box.state / supervisor.NODE_DISK_RECORD).read_text())
    for column, field in (
        ("node_tmp_total_bytes", "total_bytes"),
        ("node_tmp_used_bytes", "used_bytes"),
        ("node_tmp_free_bytes", "free_bytes"),
        ("node_tmp_free_inodes", "free_inodes"),
    ):
        assert int(row[column]) == sample[field]
    print(
        "unconfigured sampler: project interpreter used for all four helpers; disk fields published"
    )


def test_a_manual_symlink_sampler_in_a_shared_group_is_replaced(tmp_path):
    from reckon.crew.process_signal import _running_descendants, _signal_one

    installed = tmp_path / "installed" / "fleet-health"
    previous = tmp_path / "earlier-checkout" / "bin" / "fleet-health"
    current = tmp_path / "current-checkout" / "bin" / "fleet-health"
    for path in (installed, previous, current):
        path.parent.mkdir(parents=True, exist_ok=True)
    _script(previous, added=False)
    _script(current, added=True)
    installed.symlink_to(previous)
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(tmp_path),
        "FLEET_SLEEP_SECONDS": str(SAMPLE_PERIOD_SECONDS),
        "SERIES": str(tmp_path / "series.tsv"),
        "EVENTS": str(tmp_path / "events"),
        "CHECKOUT": str(current.parent.parent),
        "RESULT": str(tmp_path / "replacement.json"),
    }
    env.pop("FLEET_HEALTH_SAMPLER", None)
    old = subprocess.Popen([str(installed)], env=env)
    survivor = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({2 * HANG_SECONDS})"]
    )
    replacement = None
    try:
        earlier = _wait(lambda: _rows(tmp_path / "series.tsv"))
        assert os.getpgid(old.pid) == os.getpgid(0)
        code = (
            "import os; from pathlib import Path; "
            "from reckon.crew import fleet_supervisor as s; "
            "s._checkout_root=lambda: Path(os.environ['CHECKOUT']); "
            "pid=s.start_health_sampler(); "
            "Path(os.environ['RESULT']).write_text(str(pid))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, timeout=HANG_SECONDS, check=False
        )
        assert result.returncode == 0
        recorded = (tmp_path / "replacement.json").read_text()
        assert recorded != "None", "the installed sampler was not replaced"
        replacement = int(recorded)
        rows = _wait(
            lambda: [
                r for r in _rows(tmp_path / "series.tsv") if r.get("added") == "present"
            ]
        )
        assert rows
        series_rows = _rows(tmp_path / "series.tsv")
        assert series_rows[: len(earlier)] == [dict(row, added="-") for row in earlier]
        events = _sampler_events(tmp_path / "events")
        counts = _record_handover(
            tmp_path, series_rows, events, old.pid, replacement, earlier
        )
        assert old.wait(timeout=HANG_SECONDS) == 0
        assert survivor.poll() is None, (
            "replacement signalled a peer in the same process group"
        )
        assert supervisor.process_cmdline(replacement)[1] == str(current)
        assert [(verb, pid) for verb, pid, _ in events[:3]] == [
            ("start", old.pid),
            ("stop", old.pid),
            ("start", replacement),
        ]
        print(
            "manual handover: installed symlink replaced by checkout script; "
            f"shared-group peer survived; counts={counts}"
        )
    finally:
        # Enumerate only fixture descendants; the sampler shares the test's group.
        for root in (old.pid, replacement):
            if root is not None:
                for pid in _running_descendants(root):
                    _signal_one(pid, signal.SIGTERM, allow_group=False)
                _wait(lambda root=root: not supervisor.process_cmdline(root))
        old.wait(timeout=HANG_SECONDS)
        survivor.terminate()
        survivor.wait(timeout=HANG_SECONDS)


def test_an_open_descriptor_without_the_lock_is_not_sampler_ownership(tmp_path, capsys):
    script = tmp_path / "other-service"
    lock = tmp_path / "fleet-health.lock"
    ready = tmp_path / "ready"
    script.write_text(
        "#!/bin/bash\n"
        f"exec 8>{shlex.quote(str(lock))}\n"
        "flock -n 8 || exit 1\n"
        f"exec 9>{shlex.quote(str(lock))}\n"
        f"touch {shlex.quote(str(ready))}\n"
        f"sleep {2 * HANG_SECONDS} & wait $!\n"
    )
    script.chmod(0o755)
    process = subprocess.Popen([str(script)], start_new_session=True)
    try:
        _wait(ready.exists)
        assert supervisor.lock_held("fleet-health", tmp_path)
        assert not supervisor._stop_health_sampler(tmp_path)
        assert process.poll() is None
        assert "replacement refused" in capsys.readouterr().out
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=HANG_SECONDS)


def test_handover_counts_accept_two_slots_and_two_missing_samples():
    rows = [
        {"sampler_pid": "11", "slot": "6"},
        {"sampler_pid": "11", "slot": "7"},
        {"sampler_pid": "22", "slot": "10"},
    ]
    events = [("start", 11, 6), ("stop", 11, 8), ("start", 22, 9)]
    assert _handover_counts(rows, events, 11, 22, 1) == {
        "rows_before": 2,
        "rows_after": 1,
        "delivery_rows": 2,
        "elapsed_slots": 2,
        "missed_slots": [8, 9],
    }


@pytest.mark.parametrize(
    "violation", ["late_replacement", "missing_samples", "late_series"]
)
def test_handover_counts_refuse_excess_slots(violation):
    rows = [
        {"sampler_pid": "11", "slot": "6" if violation == "missing_samples" else "7"},
        {
            "sampler_pid": "22",
            "slot": "11" if violation == "late_replacement" else "10",
        },
    ]
    if violation == "late_series":
        rows[1:1] = [{"sampler_pid": "11", "slot": "8"}] * 2
    events = [("start", 11, 6), ("stop", 11, 8), ("start", 22, 9)]
    message = {
        "late_replacement": "first replacement sample exceeds two sampling slots",
        "missing_samples": "handover missed more than two sampling slots",
        "late_series": "first replacement row exceeds two series samples",
    }[violation]
    with pytest.raises(AssertionError, match=message):
        _handover_counts(rows, events, 11, 22, 1)
