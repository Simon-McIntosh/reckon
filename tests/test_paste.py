"""The clipboard paste writes an image here and, when a fleet runs, on its node."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from reckon import paste as paste_module
from reckon.paste import FleetTarget, image_extension, live_fleet, paste, replicate

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeScheduler:
    """Answers squeue from a fixed state, and runs srun's copy into a directory."""

    def __init__(
        self, state: str = "RUNNING", node: str = "fleet-node-01.site", fail_copy=False
    ):
        self.state = state
        self.node = node
        self.fail_copy = fail_copy
        self.calls: list[list[str]] = []
        self.copied: dict[str, bytes] = {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "squeue":
            out = f"{self.state} {self.node}\n" if self.state else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[0] == "srun":
            if self.fail_copy:
                return subprocess.CompletedProcess(
                    argv, 1, "", "srun: error: job not found\n"
                )
            self.copied[argv[-1]] = kwargs["stdin"].read()
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ss":
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


def _environ(tmp_path: Path, job_id: str | None = "4242") -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir()
    if job_id is not None:
        (state / "record.json").write_text(
            json.dumps({"job_id": job_id, "node": "stale-node", "runtime_dir": "/x"})
        )
    return {"FLEET_STATE_DIR": str(state), "WSL_CLIP_PORT": "2490"}


@pytest.fixture
def bridge(monkeypatch):
    """A healthy bridge serving ``bridge.payload``, recording what is copied back."""

    class Bridge:
        def __init__(self):
            self.payload = PNG
            self.healthy = True
            self.copied_back: list[str] = []

    fake = Bridge()
    monkeypatch.setattr(paste_module, "bridge_healthy", lambda port: fake.healthy)
    monkeypatch.setattr(paste_module, "fetch_clipboard", lambda port: fake.payload)
    monkeypatch.setattr(
        paste_module,
        "copy_to_clipboard",
        lambda port, text: fake.copied_back.append(text),
    )
    monkeypatch.setattr(paste_module, "_short_hostname", lambda: "login-01")
    return fake


@pytest.mark.parametrize(
    ("data", "extension"),
    [
        (PNG, "png"),
        (b"\xff\xd8\xff\xe0rest", "jpeg"),
        (b"GIF89a....", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"BM\x00\x00", "bmp"),
        (b"plain clipboard text", None),
    ],
)
def test_image_extension_reads_the_leading_bytes(data, extension):
    assert image_extension(data) == extension


def test_live_fleet_takes_the_node_from_the_scheduler_not_the_record(tmp_path):
    scheduler = FakeScheduler()
    target = live_fleet(_environ(tmp_path), scheduler)
    assert target == FleetTarget(job_id="4242", node="fleet-node-01")
    assert scheduler.calls == [["squeue", "-h", "-j", "4242", "-o", "%T %N"]]


@pytest.mark.parametrize("state", ["PENDING", "COMPLETED", ""])
def test_a_record_whose_job_is_not_running_is_no_fleet(tmp_path, state):
    assert live_fleet(_environ(tmp_path), FakeScheduler(state=state)) is None


def test_no_record_is_no_fleet_and_asks_no_scheduler(tmp_path):
    scheduler = FakeScheduler()
    assert live_fleet(_environ(tmp_path, job_id=None), scheduler) is None
    assert scheduler.calls == []


def test_replicate_runs_an_overlap_step_in_the_named_job(tmp_path):
    image = tmp_path / "paste-abc.png"
    image.write_bytes(PNG)
    scheduler = FakeScheduler()
    assert replicate(image, FleetTarget("4242", "fleet-node-01"), scheduler) is None
    argv = scheduler.calls[0]
    assert argv[:4] == ["srun", "--overlap", "--jobid=4242", "--ntasks=1"]
    assert scheduler.copied == {str(image): PNG}


def test_an_image_lands_here_and_at_the_same_path_on_the_fleet_node(tmp_path, bridge):
    scheduler = FakeScheduler()
    out, err = io.StringIO(), io.StringIO()
    local = tmp_path / "local"
    local.mkdir()

    status = paste(
        environ=_environ(tmp_path), run=scheduler, directory=local, out=out, err=err
    )

    assert status == 0
    printed = Path(out.getvalue().strip())
    assert printed.parent == local and printed.suffix == ".png"
    assert printed.read_bytes() == PNG
    assert scheduler.copied == {str(printed): PNG}
    assert "also on fleet node fleet-node-01 (job 4242)" in err.getvalue()
    assert bridge.copied_back == [str(printed)]


def test_no_fleet_leaves_the_image_on_this_host_only(tmp_path, bridge):
    scheduler = FakeScheduler()
    local = tmp_path / "local"
    local.mkdir()
    status = paste(
        fleet=False,
        environ=_environ(tmp_path),
        run=scheduler,
        directory=local,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert status == 0
    assert scheduler.calls == []


def test_on_the_fleet_node_itself_nothing_is_copied(tmp_path, bridge, monkeypatch):
    monkeypatch.setattr(paste_module, "_short_hostname", lambda: "fleet-node-01")
    scheduler = FakeScheduler()
    local = tmp_path / "local"
    local.mkdir()
    paste(
        environ=_environ(tmp_path),
        run=scheduler,
        directory=local,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert [argv[0] for argv in scheduler.calls] == ["squeue"]


def test_a_failed_copy_is_reported_and_the_paste_still_succeeds(tmp_path, bridge):
    scheduler = FakeScheduler(fail_copy=True)
    out, err = io.StringIO(), io.StringIO()
    local = tmp_path / "local"
    local.mkdir()
    status = paste(
        environ=_environ(tmp_path), run=scheduler, directory=local, out=out, err=err
    )
    assert status == 0
    assert Path(out.getvalue().strip()).exists()
    assert (
        "not copied to fleet node fleet-node-01 (job 4242): srun: error: job not found"
        in (err.getvalue())
    )


def test_text_is_printed_and_nothing_is_written(tmp_path, bridge):
    bridge.payload = b"some copied text"
    scheduler = FakeScheduler()
    out = io.StringIO()
    local = tmp_path / "local"
    local.mkdir()
    status = paste(
        environ=_environ(tmp_path),
        run=scheduler,
        directory=local,
        out=out,
        err=io.StringIO(),
    )
    assert status == 0
    assert out.getvalue() == "some copied text"
    assert list(local.iterdir()) == []
    assert scheduler.calls == []


def test_an_unreachable_bridge_says_which_side_to_fix(tmp_path, bridge):
    bridge.healthy = False
    err = io.StringIO()
    status = paste(
        environ=_environ(tmp_path),
        run=FakeScheduler(),
        directory=tmp_path,
        out=io.StringIO(),
        err=err,
    )
    assert status == 1
    text = err.getvalue()
    assert text.startswith(
        "reckon paste: clipboard bridge unreachable on login-01:2490"
    )
    assert "No reverse forward is bound on login-01" in text
