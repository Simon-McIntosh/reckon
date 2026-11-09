"""An attempt's pointer records the host that issued its worker's pid.

A pid means nothing on a machine that did not issue it, and the crew home is
shared across login nodes, so the pointer's ``launcher_host`` is what lets a
reader decide whether its own process table can answer for the worker. Two
writes issue a pid and must therefore name the host doing so:

* a resume re-issues the worker's pid. Before this change the attempt was
  recorded without a host, so the pointer kept the dispatch's host, and once
  that host was gone no machine could prove the worker dead — every later
  resume was refused as *liveness unknown* and the run had to be discarded,
  losing its session;
* an admitted launch publishes the launched attempt over the queued request it
  read. The queued record carries no host, because no process exists while a
  request waits, so the launch must write its own.

Every other pointer write carries the recorded host forward: the station's
sweeps rewrite a pointer from another machine and must not claim the attempt
as their own.

The declared negative control is the base behaviour for the resume — the host
is not written, so the pointer keeps the dispatch's and the second resume is
refused — plus a writer that stamps the writing host on every pointer write,
which reddens the third-host case. Run with the control's environment variable
set, the resume and third-host cases must fail.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import queue_admission, resumption, runs
from tests import test_a_live_run_never_reads_dead as liveness
from tests.test_a_queued_launch_keeps_the_run_record import (  # noqa: F401
    _config_for,
    _launcher,
    _prime_lane,
    _queued_request,
    admit_repo,
)
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    CONFIG,
    _stopped_pointer,
    crew_home,
    operator_home,
    repo,
)

# The declared mutation, verbatim: the string a reader compares the red log
# against. Two wrong states, one switch, because the string names both.
DECLARED_MUTATION = (
    "At base the pointer keeps A and the second resume is refused; stamping "
    "the writing host on every write fails the host-C case"
)

MUTATION_ENV = "RECKON_RESUME_HOST_NEGATIVE_CONTROL"

# The host that launched the run, and a second machine that resumes it.
DISPATCH_HOST = "host-a"
RESUME_HOST = "host-b"
# A third machine that only rewrites the pointer.
THIRD_HOST = "host-c"

RUN_ID = "r-resume-records-its-host"


@pytest.fixture()
def base_resume_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base revision's resume record: no host is written.

    One of the two wrong states the declared string names. Requested only by
    the resume cases, so the stamping writer below cannot mask it: a writer
    that names every host would otherwise put the resuming host on the pointer
    before the resume record is folded, and the base state would read green.
    """
    if os.environ.get(MUTATION_ENV) != "1":
        return
    real_record = resumption.record_resumption

    def base_record(run_id: str, **kwargs: object) -> dict:
        prior = runs.read_pointer(run_id).get("launcher_host")
        recorded = real_record(run_id, **kwargs)

        def restore(record: dict) -> dict:
            record["launcher_host"] = prior
            return record

        runs._mutate_pointer(run_id, restore)
        return recorded

    monkeypatch.setattr(resumption, "record_resumption", base_record)


@pytest.fixture()
def stamp_every_write_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other wrong state: every pointer write claims the writing host."""
    if os.environ.get(MUTATION_ENV) != "1":
        return

    def stamp_every_write(path: Path, payload: dict) -> dict:
        if path.parent == runs.live_dir():
            return {**payload, "launcher_host": socket.gethostname()}
        return payload

    monkeypatch.setattr(runs, "_stamp_pointer_launch_host", stamp_every_write)


def _stop_the_attempt_here(run_id: str) -> None:
    """Leave the pointer as a stop on this host leaves it.

    The recorded worker pid is gone and nothing observed its end: no supervisor
    exit record exists, so the only thing that can license the next resume is
    this host's own process table answering for the pid it issued.
    """

    def dead(record: dict) -> dict:
        record["pid"] = liveness._absent_pid()
        record["pid_start_time"] = None
        record["process_alive"] = None
        record["phase"] = "stopped"
        return record

    runs._mutate_pointer(run_id, dead)
    exit_record = runs.run_dir(run_id) / "exit.json"
    if exit_record.exists():
        exit_record.unlink()


def _resume_here(record: dict, launched: list) -> dict:
    """Resume ``record`` on the host the socket now names."""

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched.append(str(prompt_path))
        return os.getpid()

    return resumption._resume(
        RUN_ID, record, config=CONFIG, launcher=launcher, advice="continue"
    )


@pytest.fixture()
def run_on_the_dispatch_host(tmp_path: Path, request: pytest.FixtureRequest) -> dict:
    """A run dispatched on one machine and stopped there, ready to be resumed.

    ``launcher_host`` is the dispatch host because the pointer is created while
    the socket names it, which is what the pointer writer stamps on a new file.
    The crew home and the repository are the fixtures imported from the resume
    module, asked for by name so this module does not shadow their names.
    """
    request.getfixturevalue("operator_home")
    request.getfixturevalue("crew_home")
    repo_path = request.getfixturevalue("repo")
    real_hostname = socket.gethostname
    socket.gethostname = lambda: DISPATCH_HOST
    try:
        record = _stopped_pointer(tmp_path, repo_path, RUN_ID, backend="alpha")
    finally:
        socket.gethostname = real_hostname
    record["session"] = "coordinator-a"
    record["dialect"] = "codex"
    crew._write_json(runs.pointer_path(RUN_ID), record)
    return record


def test_a_resume_records_the_host_that_launched_it(
    run_on_the_dispatch_host: dict,
    monkeypatch: pytest.MonkeyPatch,
    base_resume_control: None,
) -> None:
    """The resumed attempt's pid was issued here, so the pointer names here."""
    assert run_on_the_dispatch_host["launcher_host"] == DISPATCH_HOST
    monkeypatch.setattr(socket, "gethostname", lambda: RESUME_HOST)

    _resume_here(runs.read_pointer(RUN_ID), [])

    resumed = runs.read_pointer(RUN_ID)
    assert resumed["launcher_host"] == RESUME_HOST, (
        f"the resume left the dispatch host on the pointer: {resumed['launcher_host']!r}"
    )
    assert resumed["attempt"] > int(run_on_the_dispatch_host["attempt"])


def test_a_second_resume_after_a_stop_on_the_new_host_is_not_refused(
    run_on_the_dispatch_host: dict,
    monkeypatch: pytest.MonkeyPatch,
    base_resume_control: None,
) -> None:
    """The host that issued the resumed pid can prove it dead, so it may relaunch."""
    monkeypatch.setattr(socket, "gethostname", lambda: RESUME_HOST)
    _resume_here(runs.read_pointer(RUN_ID), [])
    _stop_the_attempt_here(RUN_ID)

    record = runs.read_pointer(RUN_ID)
    assert record["launcher_host"] == RESUME_HOST
    # The refusal the measured run met, asked directly: this host must be able
    # to answer for the pid its own resume issued.
    assert resumption._observed_end_refusal(record) is None, (
        "the second resume is refused as liveness unknown on the host that "
        "launched the attempt"
    )

    launched: list = []
    _resume_here(record, launched)
    assert launched, "the second resume launched nothing"
    assert runs.read_pointer(RUN_ID)["phase"] == "working"


def test_a_third_hosts_pointer_write_keeps_the_launch_host(
    run_on_the_dispatch_host: dict,
    monkeypatch: pytest.MonkeyPatch,
    stamp_every_write_control: None,
) -> None:
    """A sweep rewriting the pointer from elsewhere does not claim the attempt."""
    monkeypatch.setattr(socket, "gethostname", lambda: RESUME_HOST)
    _resume_here(runs.read_pointer(RUN_ID), [])

    monkeypatch.setattr(socket, "gethostname", lambda: THIRD_HOST)
    # The write a station sweep makes: a payload carrying no host of its own,
    # funnelled through the pointer writer every non-launch path uses.
    crew._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "phase": "working",
            "pid": None,
            "last_swept_at": "2026-11-09T16:00:00Z",
        },
    )

    rewritten = runs.read_pointer(RUN_ID)
    assert rewritten["launcher_host"] == RESUME_HOST, (
        f"a write from {THIRD_HOST} claimed the attempt: {rewritten['launcher_host']!r}"
    )


def test_a_queued_run_launched_here_carries_the_launching_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """The queued record has no host, so the launch publishes its own."""
    repository = request.getfixturevalue("admit_repo")
    config, resolve = _config_for()
    _prime_lane(tmp_path, config)
    request = _queued_request(repository, node_id="node-a", run_id=RUN_ID)
    # The host a held request is stored with: no process exists while it waits,
    # so ``dispatch._queued_request_record`` writes None.
    request["launcher_host"] = None
    runs.queue_dispatch(request)
    queued = runs.read_pointer(RUN_ID)
    assert queued.get("launcher_host") is None, queued.get("launcher_host")

    monkeypatch.setattr(socket, "gethostname", lambda: RESUME_HOST)
    launched: list = []
    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(launched)
    )
    assert result["launched"] == [RUN_ID], result

    pointer = runs.read_pointer(RUN_ID)
    assert pointer["phase"] != "queued"
    assert pointer["launcher_host"] == RESUME_HOST, (
        f"the admitted launch carried the queued absence: {pointer['launcher_host']!r}"
    )
