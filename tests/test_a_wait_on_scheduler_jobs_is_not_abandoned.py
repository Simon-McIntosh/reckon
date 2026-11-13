"""A wait on a batch of scheduler jobs is never read as abandoned while one lives.

On 12-13 November a run declared a wait whose probe ran
``python …/wait_jobs.py 1312389,1312390,1312391,1312392,1312393,1312394,1312395``
— seven jobs on ``betelgeuse``, one completed, one running and five pending on
priority. The wait was healthy. The obligations view read it as
``run-producer-gone`` and said no scheduler job was alive, because a job id was
read only from a probe that ran a scheduler command or passed a job flag. A
script handed a comma-separated batch names neither, so the scheduler was never
asked and the row asserted an absence it had never looked for.

Three readings are pinned here. A wait naming a batch is a wait on those jobs,
and the run is not abandoned while the scheduler lists any of them. A wait whose
jobs the scheduler does not list reads as nothing can lift it, and its row says
the scheduler was asked and listed none. A wait naming no job reads as before,
and its row says the scheduler was not consulted rather than that no job is
alive — the two are different facts and only one of them was measured.

Every case stubs the scheduler query itself rather than the reader that reports
its answer, so the ids the query carries are the ones the classifier derived.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import recovery_classification, recovery_wait
from reckon.crew.runs import runs_dir
from tests.test_a_run_parked_on_a_live_job_keeps_waiting import (
    BOUND,
    _asked_jobs,
    _row,
    _scheduler_stub,
)
from tests.test_a_run_parked_on_a_live_job_keeps_waiting import (
    _waiting_run as _parked_run,
)

# The probe the run declared, in the shape that sat idle: an interpreter handed
# a script and the batch as one comma-separated operand. Its digits are not a
# job to a reader that looks only for a scheduler command or a job flag.
BATCH_JOBS = ("1312389", "1312390", "1312391")
BATCH_PROBE = ["python", "{script}", ",".join(BATCH_JOBS)]

# A probe that names no job at all: a file wait whose reference is a path.
NO_JOB_PROBE = ["cat", "{state}"]

# The same shape as the batch, with one bare number instead of a list. Nothing
# in a lone operand says it is a job, so it stays unread.
SINGLE_JOB = ("1312389",)
SINGLE_NUMBER_PROBE = ["python", "{script}", SINGLE_JOB[0]]

# A comma list of ports behind the same script operand: the shape that made
# this reader too eager. ``nc -z a-host 80,443`` hands the probe two numbers
# the way the job batch does, and neither is a job.
PORT_LIST = ("80", "443")
# The host operand is a path that exists, so the probe resolves a reference and
# is not refused as one that cannot fail before the port rule is reached.
PORT_LIST_PROBE = ["nc", "-z", "{state}", ",".join(PORT_LIST)]
NC_PORT_PROBE = ["nc", "-z", "a-host", ",".join(PORT_LIST)]

RUN_IDS = (
    "r-batch-one-job-running",
    "r-batch-no-job-listed",
    "r-no-job-named",
    "r-single-bare-number",
    "r-batch-scheduler-unaskable",
    "r-port-list",
    "r-flag-list",
)


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in RUN_IDS]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


def _waiting_run(run_id: str, *, probe: list[str]) -> dict:
    """The shared parked run, with the files its batch probe names beside it.

    The fixtures are the parked-run module's own; what this module adds is the
    files a probe reaches. The script is a real path on this host, because a
    probe that resolves no reference at all is refused as one that cannot fail
    before any of this reading is reached.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "state.txt"
    state.write_text("RUNNING\n", encoding="utf-8")
    script = directory / "wait_jobs.py"
    script.write_text("import sys\nprint('PENDING')\n", encoding="utf-8")
    resolved = [part.format(state=state, script=script) for part in probe]
    return _parked_run(run_id, probe=resolved)


def _detail(row: dict) -> str:
    """The row's detail text, lowercased.

    The classification and obligation kind keep their names — another shipped
    plan reads both — so the word this reading is about is the one the row's
    own detail uses. A detail that names the run's own process cannot be read
    as the project's watch producer having stopped.
    """
    return str(row.get("detail") or "").lower()


def test_a_batch_wait_with_one_job_running_is_not_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One live job in the batch is what keeps this run's wait liftable.

    The probe names its jobs through a script's operand rather than through a
    scheduler command or a job flag, which is the shape that was read as naming
    nothing. The scheduler is asked, it lists one of the three, and the run is
    not called abandoned.
    """
    asked = _scheduler_stub(monkeypatch, frozenset({BATCH_JOBS[1]}))
    run = _waiting_run(RUN_IDS[0], probe=BATCH_PROBE)

    row = _row(run)

    assert _asked_jobs(asked) == list(BATCH_JOBS)
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert row["external_wait"].get("producer_gone") is None


def test_a_batch_wait_whose_jobs_all_left_the_queue_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler answered, and it lists none of the three.

    With no process, a value that has not moved past the bound and no job in
    the queue, nothing is left that could lift the wait — and the row says the
    scheduler was asked and listed none, which is a fact it measured.
    """
    _scheduler_stub(monkeypatch, frozenset())
    run = _waiting_run(RUN_IDS[1], probe=BATCH_PROBE)

    row = _row(run)

    assert row["classification"] == recovery_classification.PRODUCER_GONE_STATUS
    assert (
        row["recovery_classification"] == recovery_classification.PRODUCER_GONE_STATUS
    )
    assert row["external_wait"]["producer_gone"]["still_seconds"] >= BOUND
    assert row["external_wait"]["producer_gone"]["scheduler_jobs"] == list(BATCH_JOBS)
    assert "the scheduler listed none of the 3 jobs the wait names" in row["detail"]
    assert "the scheduler was not consulted" not in row["detail"]


def test_a_wait_naming_no_job_says_the_scheduler_was_not_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait that names no job reads as before, and says why the silence is empty.

    The run is still read as having nothing left to lift it, because a file
    wait's probe answers the same value for as long as the wait is healthy. Its
    row must not borrow the scheduler's authority for a question the scheduler
    was never asked.
    """
    asked = _scheduler_stub(monkeypatch, frozenset())
    run = _waiting_run(RUN_IDS[2], probe=NO_JOB_PROBE)

    row = _row(run)

    assert asked == []
    assert row["classification"] == recovery_classification.PRODUCER_GONE_STATUS
    assert row["external_wait"]["producer_gone"]["scheduler_jobs"] == []
    assert "the scheduler was not consulted" in row["detail"]
    assert "no scheduler job alive" not in row["detail"]


def test_a_single_bare_number_names_no_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One number handed to a script stays unread, as a pid or a port may be one.

    Nothing in a single bare operand says it is a job: ``kill -0 424242`` and
    ``nc -z a-host 8765`` hand a script one number too. Reading it as a job
    would ask the scheduler about something it was never told about, so the
    operand is left unread and the row says the scheduler was not consulted.
    """
    asked = _scheduler_stub(monkeypatch, frozenset(SINGLE_JOB))
    run = _waiting_run(RUN_IDS[3], probe=SINGLE_NUMBER_PROBE)

    row = _row(run)

    assert asked == []
    assert recovery_wait._wait_probe_job_ids(["python", "/x/y.py", SINGLE_JOB[0]]) == ()
    assert row["classification"] == recovery_classification.PRODUCER_GONE_STATUS
    assert row["external_wait"]["producer_gone"]["scheduler_jobs"] == []
    assert "the scheduler was not consulted" in row["detail"]


def test_a_port_list_is_not_read_as_a_job_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two comma-separated ports are not two jobs, whatever their digits.

    A number cannot say it is a job: the port vocabulary in this module runs to
    65535, so ``50000,50001`` is as plausible a port pair as a job pair. The
    command settles it. ``nc`` takes ports by position, so its numeric list is
    read as ports; an interpreter handed a script takes none, so the same list
    is read as jobs. The scheduler is stubbed to list both ports, so a reader
    that mistook them for jobs would keep this run waiting.
    """
    asked = _scheduler_stub(monkeypatch, frozenset(PORT_LIST))
    run = _waiting_run(RUN_IDS[5], probe=PORT_LIST_PROBE)

    row = _row(run)

    assert recovery_wait._wait_probe_job_ids(NC_PORT_PROBE) == ()
    assert (
        recovery_wait._wait_probe_job_ids(["nc", "-z", "a-host", "50000,50001"]) == ()
    )
    # Beyond the port ceiling a number cannot be a port, so the list is jobs
    # even for the command that takes ports.
    assert recovery_wait._wait_probe_job_ids(["nc", "-z", "a-host", "70000,70001"]) == (
        "70000",
        "70001",
    )
    assert recovery_wait._wait_probe_job_ids(["python", "/x/y.py", "1234,5678"]) == (
        "1234",
        "5678",
    )
    assert asked == []
    assert row["classification"] == recovery_classification.PRODUCER_GONE_STATUS
    assert row["external_wait"]["producer_gone"]["scheduler_jobs"] == []
    assert "the scheduler was not consulted" in row["detail"]


@pytest.mark.parametrize("refusal", ["absent", "failed"])
def test_a_batch_the_scheduler_could_not_answer_about_is_not_abandoned(
    monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """A query that could not be put leaves the job question unanswered.

    The wait names three jobs and the scheduler is either missing or refuses
    the query, so nothing was measured about them. The run keeps its wait, and
    the row says the scheduler could not be asked rather than that no job is
    alive — the second is a claim only a answered query could support.
    """
    asked = _scheduler_stub(monkeypatch, frozenset(BATCH_JOBS), refusal=refusal)
    run = _waiting_run(RUN_IDS[4], probe=BATCH_PROBE)

    row = _row(run)

    assert _asked_jobs(asked) == list(BATCH_JOBS)
    assert row["classification"] == "waiting"
    assert row["external_wait"].get("producer_gone") is None
    detail = _detail(row)
    assert "the scheduler could not be asked" in detail
    assert "the 3 jobs the wait names" in detail
    assert "no scheduler job alive" not in detail
    assert "producer" not in detail


def test_a_flag_list_is_read_whole_before_the_words_are_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job flag's comma list names every job in it, not just the first.

    The words a probe runs are split at a comma, so a flag carrying a batch
    reaches them as the flag with its comma value and a stray second id. Only
    the first would ever be asked about, and a wait resting on the second alone
    would read as abandoned while its job ran. The list is read from the raw
    vector, which still carries it whole, in the ``=``-joined and glued
    spellings alike.
    """
    joined = ["python", "{script}", "--jobs=" + ",".join(BATCH_JOBS)]
    glued = ["python", "{script}", "-j" + ",".join(BATCH_JOBS)]

    assert recovery_wait._wait_probe_job_ids(joined) == BATCH_JOBS
    assert recovery_wait._wait_probe_job_ids(glued) == BATCH_JOBS

    # Only the job the word split keeps is alive, which is the case that read
    # as abandoned while the rest of the list was dropped.
    asked = _scheduler_stub(monkeypatch, frozenset({BATCH_JOBS[1]}))
    row = _row(_waiting_run(RUN_IDS[6], probe=joined))

    assert _asked_jobs(asked) == list(BATCH_JOBS)
    assert row["classification"] == "waiting"
    assert row["external_wait"].get("producer_gone") is None


def test_no_detail_names_the_watch_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every reading names the run's own process, never the watch producer.

    The word the incident turned on is the one the row's detail uses for what
    is gone. Naming the run's own process keeps the reading from being acted on
    as a stopped project watch producer, which is what happened for a day.
    """
    rows = []
    _scheduler_stub(monkeypatch, frozenset({BATCH_JOBS[1]}))
    rows.append(_row(_waiting_run(RUN_IDS[0], probe=BATCH_PROBE)))
    _scheduler_stub(monkeypatch, frozenset())
    rows.append(_row(_waiting_run(RUN_IDS[1], probe=BATCH_PROBE)))
    rows.append(_row(_waiting_run(RUN_IDS[2], probe=NO_JOB_PROBE)))
    rows.append(_row(_waiting_run(RUN_IDS[3], probe=SINGLE_NUMBER_PROBE)))

    for row in rows:
        assert "producer" not in _detail(row), row["detail"]


def test_the_batch_reader_is_the_one_the_classifier_calls() -> None:
    """One reader answers which jobs a probe names, so there is no second one.

    A batch reader beside the shared one would read a narrower set of shapes
    and silently answer nothing for the rest, and nothing is the answer that
    skips the scheduler entirely.
    """
    assert recovery_classification._wait_probe_job_ids is (
        recovery_wait._wait_probe_job_ids
    )
    source = Path(recovery_classification.__file__).read_text(encoding="utf-8")
    assert "def _wait_probe_job_ids(" not in source
