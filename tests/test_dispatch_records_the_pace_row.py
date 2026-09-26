"""Gate: a dispatch records the pace of the wallet that paced it.

A dispatch runs on one lane, and that lane belongs to a declared budget group
whose week, five-hour window and bar decide whether the group is admitted. None
of that reached the record the dispatch wrote, so a week of decisions could be
replayed only by reopening the streams that announced them.

Every case here drives the dispatch entry point itself rather than the row
composer, because what has to hold is the wiring: the row a real dispatch
carries. The assertions check the row against the modules that own each figure —
the allowance is recomputed through ``reckon.crew.pace`` from the row's own
clocks and policy, and the bar's vocabulary is imported rather than spelled —
so a row that agreed with itself but disagreed with its sources would fail.

Two properties are the point of the row, and each has a case:

* it reports rather than derives — every figure is an output of the module that
  owns it, so the row cannot disagree with the pace the dispatch was judged
  against;
* it replays — one case recomputes every allowance from a sequence of rows
  alone, reading no stream, so a governor's own account of itself can be
  checked rather than believed.

All cases run under a temporary configuration home; the fixture asserts that no
run any case dispatched exists under the real one.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends, budget, crew, ledger
from reckon.crew import bar as bar_module
from reckon.crew import pace as pace_module
from reckon.crew import runs
from reckon.crew.dispatch import WATCH_ARMING_ENV

# This workstation's own crew home. No case may write under it: every case
# redirects the home through RECKON_HOME and the fixture proves nothing leaked.
REAL_HOME = Path.home() / ".config" / "reckon"

# The row's keys, listed rather than sampled: the point of the assertion is that
# the record carries the pace whole, so a field that quietly disappeared fails.
PACE_FIELDS = {
    "lane",
    "node",
    "score",
    "recorded_at",
    "policy",
    "hold",
    "group",
    "state",
    "source",
    "member",
    "clocks",
    "allowance",
    "bar",
    "reason",
}

# The two clocks a metered wallet is read on, longest-lived last.
CLOCK_PERIODS = ("five_hour", "seven_day")

# The two tunables that bias the derivation are declared rather than inherited,
# so the row's policy block is a value the test chose and can check. The ceiling
# less the two reserves leaves a fresh dispatch an effective ceiling of 92, which
# is what the hold case is measured against.
CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
            "budget_group": "sol",
            "fallback": "beta",
        },
        "beta": {
            "launch": "cli",
            "command": "claude",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
            "budget_group": "other",
        },
        "orphan": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
    },
    "roles": {
        "implement": {},
        "review": {},
        "verify": {},
        "investigate": {},
    },
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "coordinator_reserve_pct": 3,
        "drain_lead_hours": 12.0,
        "pace_multiple": 1.25,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp(offset_seconds: int = 0) -> str:
    """A stamp this many seconds from now, derived rather than written down.

    The dispatch reads the wall clock and takes no injectable now, so a literal
    date here would be an age the test no longer measures on some future day.
    """
    return _iso(datetime.now(UTC) + timedelta(seconds=offset_seconds))


def _epoch_in(hours: float) -> int:
    return int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())


def _known(utilisation: float, *, resets_in: int = 3600) -> dict:
    """A budget block from a lane that reported headroom and a figure."""
    block = _backends.unknown_budget("recorded by the lane's own report")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation,
            "resets_at": _stamp(resets_in),
        }
    )
    return block


def _receipt(*, five: float, week: float, observed_at: str) -> dict:
    """One lane receipt naming both metered windows by length in minutes.

    The weekly row places the group in its week and the five-hour row is the
    fill the bar is drawn against, so both are recorded whenever a case wants
    the wallet observed at all.
    """
    rows = ((300, five, 4.0), (10080, week, 100.0))
    return {
        "quota_state": "measured",
        "observed_at": observed_at,
        "quota_windows": [
            {
                "window_minutes": minutes,
                "used_percent": percent,
                "resets_at": _epoch_in(hours),
                "observed_at": observed_at,
            }
            for minutes, percent, hours in rows
        ],
    }


class _Host:
    """A temporary crew home and the mount that project identity comes from."""

    def __init__(self, config_home: Path, repo: Path) -> None:
        self.config_home = config_home
        self.repo = repo
        self.spawned: list[str] = []


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A temporary crew home and a repository that looks like a reckon mount.

    Three seams are redirected so no case reaches this workstation's own state.
    ``RECKON_HOME`` moves the crew home, the worktree helper is stood in for so
    a dispatch does not have to fork a fleet, and the client-sessions directory
    moves with the home: the rollout reader locates a session by globbing that
    directory, and a test session id would otherwise search the real one.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s10">A dispatch records the pace row</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    rollout_module_ = importlib.import_module("reckon.crew.rollout")
    sessions = tmp_path / "client-sessions"
    sessions.mkdir()
    monkeypatch.setattr(rollout_module_, "CLIENT_SESSIONS_DIR", sessions)

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        # A real worktree, because the dispatch's record names where the worker
        # runs and a fabricated path would not be a place anything could open.
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), base_sha],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    # The watch is waived for every case: the lane under measurement is the
    # record a dispatch writes, and arming a producer would add a second thing
    # to explain. Waiving is the recorded no-watch path, not a bypass.
    monkeypatch.setenv(WATCH_ARMING_ENV, "off")

    host = _Host(config_home, repo)
    yield host
    for run_id in host.spawned:
        _assert_home_untouched(host.config_home, run_id)


def _assert_home_untouched(config_home: Path, run_id: str) -> None:
    """Nothing a case wrote may exist under this workstation's own crew home.

    A case may leave its run live or promote it, so the run's own directory —
    not its pointer, which promotion deletes — is what proves the dispatch
    landed under the temporary home rather than this workstation's.
    """
    assert str(runs.live_dir()).startswith(str(config_home)), runs.live_dir()
    assert str(runs.run_dir(run_id)).startswith(str(config_home)), runs.run_dir(run_id)
    assert runs.run_dir(run_id).is_dir(), run_id
    for directory in (REAL_HOME / "crew" / "live", REAL_HOME / "crew" / "runs"):
        assert not (directory / f"{run_id}.json").exists(), f"{directory}/{run_id}"
        assert not (directory / run_id).exists(), f"{directory}/{run_id}"


def _prescribed_node(config_home: Path, name: str) -> crew.TaskNode:
    """A node meeting every prescribed property: exact, verified, one artifact."""
    return crew.TaskNode(
        id=f"node-{name}",
        goal="carry the pace row on the dispatch record",
        plan="fixture",
        section="s10",
        spec_level="exact",
        role="verify",
        done_when=(
            "pytest reports 3 passing tests against the 1 passing before, and the "
            "row lands at reckon/budget.py:2050"
        ),
        write_paths=["reckon/budget.py"],
        time_budget="20m",
        negative_control="drop the pace key from the record; the row assertion fails",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _open_node(config_home: Path, name: str) -> crew.TaskNode:
    """A node meeting most of the prescribed properties, all of the open ones.

    It carries the two things dispatch refuses a node for lacking — a
    demonstrable measure and an enumerated write scope — and none of the four
    that make it prescribed: it names several artifacts rather than one, no
    file and line, no numeric gate and no control mutation. The fence is
    declared rather than left to the configuration's default so the score is a
    property of this record alone and can be compared without reconstructing
    what a dispatch would have filled in.
    """
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record the pace every dispatch was judged against",
        plan="fixture",
        section="s10",
        spec_level="open",
        role="investigate",
        done_when=(
            "how the dispatch decided is answerable from the row alone; "
            "`uv run reckon crew runs` returns it with no stream read"
        ),
        write_paths=["reckon/crew/pace.py", "reckon/budget.py"],
        time_budget="20m",
        negative_control="",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _plant_receipt(
    host: _Host,
    run_id: str,
    *,
    backend: str,
    five: float,
    week: float,
    observed_at: str | None = None,
    **extra: object,
) -> dict:
    """Record a lane's receipt on a live pointer, as a run in flight does.

    The pace reads a group's clocks from a receipt some run recorded, so a case
    that wants a group observed plants the receipt the reading is made of. The
    pointer names a live pid because it stands for a run in flight, and the
    receipt is the only field the pace consults.
    """
    moment = observed_at or _stamp(0)
    record: dict = {
        "run_id": run_id,
        "project": "sample",
        "backend": backend,
        "phase": "running",
        "pid": os.getpid(),
        "created_at": moment,
        "observed_at": moment,
        "lane_receipt": _receipt(five=five, observed_at=moment, week=week),
    }
    record.update(extra)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    (runs.live_dir() / f"{run_id}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    return record


def _spent_lane(host: _Host, run_id: str, *, backend: str, utilisation: float) -> dict:
    """Record a lane already at that utilisation, as its own runs report it."""
    return _plant_receipt(
        host,
        run_id,
        backend=backend,
        five=utilisation,
        week=utilisation,
        budget=_known(utilisation),
    )


def _dispatch(
    host: _Host,
    name: str,
    node: crew.TaskNode,
    *,
    session: str | None = None,
    **kwargs: object,
) -> dict:
    """Drive one node through the dispatch entry point and keep its run id."""
    record = crew.dispatch(
        node=node,
        project="sample",
        repo=host.repo,
        config=CONFIG,
        session=session or f"session-{name}",
        launcher=lambda *arguments, **options: 4242,
        watch_required=False,
        **kwargs,
    )
    host.spawned.append(str(record["run_id"]))
    return record


def _endedness_score(node: crew.TaskNode) -> float:
    """The score the dispatch judges a node's declared scope by.

    Fetched at call time rather than imported at module load, so a run of this
    file against a revision where the composer does not exist yet reports the
    missing row as a failing assertion rather than as a collection error that
    hides every case in the file behind it.
    """
    return importlib.import_module("reckon.crew.dispatch").open_endedness_score(node)


def _row(record: dict) -> dict:
    """The pace row a dispatch wrote, whole.

    The first assertion a red run reaches is the row's presence: the mutation
    this node declares is dropping it, and it must be the check that fires.
    """
    assert "pace" in record, (
        f"the dispatch record carries no pace row: {sorted(record)}"
    )
    row = record["pace"]
    assert isinstance(row, dict), row
    assert set(row) == PACE_FIELDS, sorted(set(row) ^ PACE_FIELDS)
    return row


def _replay_allowance(row: dict) -> dict:
    """Recompute a row's allowance from that row alone, as a reader would.

    Nothing here opens a stream, a receipt or a configuration layer: the week's
    utilisation, both policy figures and the instant the row was judged at all
    come off the row, and the elapsed hours are recomputed from the row's own
    recorded instant and the weekly reset it carries. Recomputing rather than
    re-reading is what makes the row checkable by someone who was not there.
    """
    week = row["clocks"]["seven_day"]
    assert week["state"] == "observed", week
    recorded = datetime.fromisoformat(row["recorded_at"])
    resets_at = datetime.fromisoformat(week["resets_at"])
    elapsed = max(
        0.0, pace_module.WEEK_HOURS - (resets_at - recorded).total_seconds() / 3600.0
    )
    reading = pace_module.GroupReading(
        group=row["group"],
        utilisation=week["utilisation"],
        elapsed_hours=elapsed,
    )
    pace = pace_module.PacePolicy(
        drain_lead_hours=row["policy"]["drain_lead_hours"],
        pace_multiple=row["policy"]["pace_multiple"],
    )
    return pace_module.allowance_for_group(reading, pace=pace).as_dict()


def test_a_prescribed_dispatch_records_the_pace_of_the_wallet_that_paced_it(host):
    _plant_receipt(host, "r-seed-alpha", backend="alpha", five=22.0, week=45.0)

    record = _dispatch(
        host, "prescribed", _prescribed_node(host.config_home, "prescribed")
    )
    row = _row(record)

    assert row["lane"] == "alpha", row
    assert row["node"] == "node-prescribed", row
    # Zero is the floor of the band and it is reachable only if the score reads
    # the scope the node declares. The shared landing paths a dispatch appends
    # to every node on a plan are the dispatch's own bookkeeping, not artifacts
    # the node named, so a score taken after that grant counts them and lifts
    # every node on a plan off the floor — where a prescribed node then draws a
    # bar that holds it, which is the outcome the band exists to prevent.
    assert row["score"] == 0.0, row
    assert row["hold"] is None, row
    assert row["reason"] is None, row
    assert row["group"] == "sol", row
    assert row["member"] == "alpha", row
    assert row["state"] == budget.OBSERVED, row
    assert row["source"] == budget.WINDOW_SOURCE_RECORDED, row
    assert row["policy"] == {"drain_lead_hours": 12.0, "pace_multiple": 1.25}, row
    age = (
        datetime.now(UTC) - datetime.fromisoformat(row["recorded_at"])
    ).total_seconds()
    assert -1.0 <= age < 120.0, row["recorded_at"]

    clocks = row["clocks"]
    assert set(clocks) == set(CLOCK_PERIODS), sorted(clocks)
    for period in CLOCK_PERIODS:
        clock = clocks[period]
        assert clock["period"] == period, clock
        assert clock["state"] == budget.OBSERVED, clock
        assert clock["observed_at"], clock
        assert clock["resets_at"], clock
        assert clock["age_seconds"] is not None, clock
        assert -1.0 <= clock["age_seconds"] < 120.0, clock
    assert clocks["five_hour"]["utilisation"] == pytest.approx(0.22, abs=1e-9), clocks
    assert clocks["seven_day"]["utilisation"] == pytest.approx(0.45, abs=1e-9), clocks

    allowance = row["allowance"]
    assert allowance["group"] == "sol", allowance
    assert allowance["pace_multiple"] == pytest.approx(1.25), allowance
    assert allowance["utilisation"] == pytest.approx(0.45), allowance
    assert allowance["elapsed_hours"] == pytest.approx(68.0, abs=1.0), allowance
    assert allowance["limited_by"] == "allowance", allowance
    assert allowance["provider_ceiling"] is None, allowance

    bar = row["bar"]
    assert bar["name"] == "node-prescribed", bar
    assert bar["score"] == row["score"], bar
    assert bar["state"] == budget.OBSERVED, bar
    assert bar["verdict"] == bar_module.SEND_LOCAL, bar
    assert bar["decided_by"] == "prescription", bar
    assert bar["window_fill"] == pytest.approx(0.22, abs=1e-9), bar
    assert bar["bar"] == pytest.approx(0.22 * 0.22 * (3.0 - 2.0 * 0.22)), bar
    assert bar["margin"] == pytest.approx(row["score"] - bar["bar"]), bar


def test_a_row_recomputes_its_own_allowance(host):
    _plant_receipt(host, "r-seed-alpha", backend="alpha", five=22.0, week=45.0)

    row = _row(_dispatch(host, "replay", _prescribed_node(host.config_home, "replay")))

    # No stream, no receipt and no configuration layer is opened here: every
    # figure the derivation needs is on the row, so a reader who was not there
    # can check the governor's account of itself instead of believing it.
    assert row["allowance"] == _replay_allowance(row), (
        row["allowance"],
        _replay_allowance(row),
    )


def test_an_open_dispatch_records_the_score_its_bar_was_drawn_against(host):
    _plant_receipt(host, "r-seed-alpha", backend="alpha", five=22.0, week=45.0)
    node = _open_node(host.config_home, "open")
    # A dispatch widens a node's declared scope in place with the shared landing
    # paths, so the declaration the row is compared against is copied first.
    declared = copy.deepcopy(node)

    row = _row(_dispatch(host, "open", node))

    # The score is the prescription verdict read through the vacancy the node
    # carries, so it is checked against the module that owns the banding rather
    # than against a number written down here.
    assert row["score"] == _endedness_score(declared), row
    assert row["score"] > bar_module.PRESCRIBED_MAX, row
    assert row["bar"]["score"] == row["score"], row["bar"]

    # Nothing here is prescribed, so the window decided, and the bar it was
    # drawn against is the curve the fill produces from the row's own figure.
    assert row["bar"]["decided_by"] == "window", row["bar"]
    assert row["bar"]["verdict"] == bar_module.SEND_METERED, row["bar"]
    assert row["bar"]["window_fill"] == pytest.approx(0.22, abs=1e-9), row["bar"]
    assert row["bar"]["bar"] == pytest.approx(0.22 * 0.22 * (3.0 - 2.0 * 0.22))
    assert row["hold"] is None, row
    assert row["allowance"] == _replay_allowance(row), row


def test_a_sequence_of_rows_replays_the_week_without_a_stream(host):
    fills = (10.0, 40.0, 85.0)
    rows = []
    for index, fill in enumerate(fills):
        # The receipt is refreshed ahead of each dispatch, so each row reads the
        # week as it stood at its own decision rather than at the first one.
        _plant_receipt(
            host,
            f"r-seed-{index}",
            backend="alpha",
            five=fill,
            observed_at=_stamp(index - 3),
            week=fill,
        )
        rows.append(
            _row(
                _dispatch(
                    host,
                    f"week-{index}",
                    _prescribed_node(host.config_home, f"week-{index}"),
                )
            )
        )

    assert [row["node"] for row in rows] == [
        f"node-week-{index}" for index in range(len(fills))
    ]
    assert [row["clocks"]["seven_day"]["utilisation"] for row in rows] == [
        pytest.approx(fill / 100.0) for fill in fills
    ]

    for row in rows:
        assert row["allowance"] == _replay_allowance(row), row
    # Replayed from the rows alone, the curve falls as the week fills: the same
    # windows are left, and less headroom is left to spend across them.
    derived = [row["allowance"]["derived"] for row in rows]
    assert derived == sorted(derived, reverse=True), derived
    assert derived[0] > derived[-1], derived


def test_a_held_lane_is_recorded_with_the_evidence_that_held_it(host):
    _spent_lane(host, "r-spent-alpha", backend="alpha", utilisation=95.0)
    _plant_receipt(host, "r-seed-beta", backend="beta", five=22.0, week=45.0)

    record = _dispatch(host, "held", _prescribed_node(host.config_home, "held"))

    # The node ran on the substitute the spent lane declares...
    assert record["backend"] == "beta", record["backend"]
    row = _row(record)
    assert row["lane"] == "beta", row
    assert row["group"] == "other", row
    assert row["member"] == "beta", row
    assert row["clocks"]["seven_day"]["utilisation"] == pytest.approx(0.45, abs=1e-9)
    assert row["allowance"] == _replay_allowance(row), row

    # ...and the lane it asked for is recorded as held, with the reading and the
    # threshold that held it: a row naming a substitute without saying what
    # held the first lane is a decision a reader has to take on faith.
    hold = row["hold"]
    assert hold is not None, row
    assert hold["backend"] == "alpha", hold
    assert hold["held"] is True, hold
    assert hold["effective_ceiling_pct"] == 92.0, hold
    assert hold["state"]["utilisation_pct"] == 95.0, hold
    assert "92.0% ceiling for a dispatch" in hold["reason"], hold["reason"]
    assert "5% resume reserve" in hold["reason"], hold["reason"]


def test_a_lane_with_no_declared_wallet_records_the_absence_not_an_empty_one(host):
    node = _prescribed_node(host.config_home, "orphan")

    row = _row(_dispatch(host, "orphan", node, backend_override="orphan"))

    assert row["lane"] == "orphan", row
    assert row["group"] is None, row
    assert row["state"] == budget.UNKNOWN, row
    assert row["source"] is None, row
    assert row["member"] is None, row
    assert row["allowance"] is None, row
    assert row["bar"] is None, row
    assert row["reason"] and "no budget group" in row["reason"], row

    # An absent wallet is not an empty one: every figure nothing could read is
    # absent rather than zero, which a reader would take for an unspent window.
    for period in CLOCK_PERIODS:
        clock = row["clocks"][period]
        assert clock["state"] == budget.UNKNOWN, clock
        assert clock["utilisation"] is None, clock
        assert clock["age_seconds"] is None, clock
        assert clock["observed_at"] is None, clock
        assert clock["resets_at"] is None, clock


def _promote(host: _Host, run_id: str) -> dict:
    """Complete a dispatched run through the promotion entry point.

    Promotion is where the live pointer is deleted and the committed ledger row
    is written, so it is the one moment at which a row's durability can be
    observed at all: the run is completed here exactly as an orchestrator
    completes one.
    """
    return crew.complete(
        run_id,
        gate="passed",
        outcome="the run carried its pace row through promotion",
        root=host.repo,
    )


def _committed_row(host: _Host, run_id: str) -> dict:
    """Read one run's committed ledger row back out of the repository's store."""
    rows = [
        row
        for row in ledger.runs("sample", root=host.repo)
        if str(row.get("run_id") or "") == run_id
    ]
    assert len(rows) == 1, f"{len(rows)} committed rows for {run_id}"
    return rows[0]


def test_a_promoted_run_replays_its_allowance_from_the_committed_row(host):
    """The row must outlive the pointer, or the week it records dies with it.

    A dispatch writes the pace row to the live pointer, and promotion deletes
    that pointer in the step that appends the committed row — so the committed
    row is the only place a week of dispatch decisions can be replayed from.
    Each case here dispatches for real, promotes through the completion entry
    point, and then recomputes the allowance from the committed row alone: the
    pointer is gone and no stream, receipt or configuration layer is opened to
    interpret what the row already spells out.
    """
    fills = (22.0, 55.0)
    replayed = []
    for index, fill in enumerate(fills):
        # The receipt is refreshed ahead of each dispatch so each row reads the
        # week as it stood at its own decision rather than at the first one.
        _plant_receipt(host, f"r-seed-{index}", backend="alpha", five=fill, week=fill)
        node = _open_node(host.config_home, f"promoted-{index}")
        record = _dispatch(host, f"promoted-{index}", node)
        row = _row(record)
        run_id = str(record["run_id"])
        assert runs.pointer_path(run_id).is_file(), run_id

        promoted = _promote(host, run_id)

        # The pointer the row was written to is gone before anything is read
        # back, so every figure below comes off the committed store.
        assert promoted["pointer_removed"] is True, promoted
        assert not runs.pointer_path(run_id).exists(), run_id
        stored = _committed_row(host, run_id)
        assert stored["pace"] == row, (stored.get("pace"), row)
        assert stored["pace"]["allowance"] == _replay_allowance(stored["pace"]), (
            stored["pace"]["allowance"],
            _replay_allowance(stored["pace"]),
        )
        replayed.append(_replay_allowance(stored["pace"]))

    # Two rows of one wallet, both replayed from the committed store alone: the
    # curve falls as the week fills, and no stream was read to say so.
    derived = [entry["derived"] for entry in replayed]
    assert derived == sorted(derived, reverse=True), derived
    assert derived[0] > derived[-1], derived
