"""Every writer racing a landing write either commutes or is refused.

Two kinds of writer reach one plan HTML: the landing record a promotion
appends, and the edits an agent makes through the tool surface. The plan
measured a lost update here — a comment appended inside a promotion's window
was discarded while the version counter still advanced, so nothing raised a
conflict and nothing could notice.

The store holds a per-file lock across the version check and the rendition,
and a comment append whose base revision moved under it is merged by section
and id rather than refused. This module races each of the five writers that
can reach a plan — the landing record, a followup append, a section append, a
state set and a comment append — against an in-flight landing write on the
same plan. For each it requires that the writer's change is present
afterwards, or that its loss was refused with both versions named: a writer
that reports success while its change is gone is the failure being fenced.

Both writers rendezvous at the store's write threshold — after each has read
the plan and passed its own version check, before either takes the plan's
write lock. The lock then decides which writer proceeds and which meets the
fenced path. The meeting cannot sit inside the render step, because the write
path holds the lock across the version check and the replacement: with the
meeting placed there the second writer waits on the lock rather than on the
meeting, so it can never complete (measured: the barrier timed out on every
run and the writers raced nothing).

A thread that does not reach the meeting within the window fails the case,
naming the thread that missed it, rather than having the writers run unraced.
One case rigs a deliberate miss and requires that failure.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon import _plan_html, _store
from reckon.mcp import _edit_plan

PROJECT = "race-fixture"
PLAN = "race-target"
ANCHOR = "s5"
SECTION_RECORD = {
    "id": "s-race",
    "title": "The authored heading a section append writes",
    "body": "<p>Prose the append authors.</p>",
    "effort_hours": 0.5,
    "capability": {
        "version": "1.0",
        "class": "general",
        "requirements": {
            "reasoning": "standard",
            "verification": "strict",
            "risk": "low",
        },
    },
    "links": [],
}


class RendezvousMissedError(AssertionError):
    """Raised when a writer thread does not reach the meeting in time."""


class Rendezvous:
    """The meeting both writer threads must reach before either writes.

    ``timeout`` bounds how long either thread waits for the other; a thread
    still absent when the window closes fails the case against the thread that
    missed it. ``absent`` rigs one named thread to skip the meeting, which is
    how the failure path itself is exercised: the meeting then cannot complete
    and the waiting writer must report that the rendezvous broke rather than
    judge writes made without one.
    """

    PARTNERS = ("landing", "writer")

    def __init__(self, *, timeout: float = 30.0, absent: str | None = None) -> None:
        self.timeout = timeout
        self.absent = absent
        self._arrived: set[str] = set()
        self._missed: RendezvousMissedError | None = None
        self._condition = threading.Condition(threading.Lock())

    def meet(self, name: str) -> None:
        with self._condition:
            if self._missed is not None:
                raise self._missed
            if name == self.absent:
                self._condition.notify_all()
                return
            self._arrived.add(name)
            self._condition.notify_all()
            deadline = time.monotonic() + self.timeout
            while len(self._arrived) < len(self.PARTNERS):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = " and ".join(
                        partner
                        for partner in self.PARTNERS
                        if partner not in self._arrived
                    )
                    self._missed = RendezvousMissedError(
                        f"the rendezvous broke: {missing} did not reach the "
                        f"meeting within {self.timeout:g}s, so the writers "
                        "never raced"
                    )
                    self._condition.notify_all()
                    raise self._missed
                self._condition.wait(remaining)


def _seed(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title></head>"
        '<body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Race target",
        "status": "active",
        "version": 0,
        "comments": {},
        # An implementable section gives an appended followup a dispatchable
        # pointer, so the op is valid for reasons unrelated to the race being
        # measured.
        "section_declarations": {"s1": "implementable"},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _seed(root)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    # Every environment-resolved path — mounts, lock files, state — must
    # resolve inside the temp home before any writer runs.
    assert _store._config_home() == config_home.resolve()
    return root


def _comment(ident: str, who: str) -> dict:
    return {
        "id": ident,
        "who": who,
        "when": "2026-10-02T00:00:00Z",
        "body": f"<p>{ident}</p>",
    }


def _landing_write(
    root: Path, ident: str, *, retries: int = 3, who: str = "promote"
) -> dict:
    """Append one landing record the way a promotion appends it.

    The landing write reads the plan, adds its comment to the full state, and
    writes that back, re-reading and re-appending after a version conflict
    instead of discarding its record.
    """
    for _attempt in range(retries + 1):
        state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
        comments = {
            key: list(items) for key, items in (state.get("comments") or {}).items()
        }
        comments.setdefault(ANCHOR, []).append(_comment(ident, who))
        try:
            _store.write_plan(
                PROJECT,
                PLAN,
                {**state, "comments": comments},
                version,
                root,
                artifact_type="plan",
            )
        except _store.VersionConflict:
            continue
        return {"ok": True, "recorded": True, "comment_id": ident}
    raise AssertionError(f"landing record {ident!r} never landed")


def _edit(root: Path, ops: list[dict], expected_version: int) -> dict:
    """One tool-surface edit, submitted the way an agent submits it."""
    return _edit_plan(
        PROJECT,
        PLAN,
        ops,
        expected_version,
        checkout_path=str(root),
        doc_type="plan",
    )


def _refused_naming_both_versions(outcome: object) -> bool:
    """Whether an outcome is a refusal that names both versions."""
    if not isinstance(outcome, dict):
        return False
    if outcome.get("ok") is not False or outcome.get("error") != "version_conflict":
        return False
    expected = outcome.get("expected_version")
    current = outcome.get("current_version")
    message = str(outcome.get("message", ""))
    return (
        isinstance(expected, int)
        and isinstance(current, int)
        and expected != current
        and str(expected) in message
        and str(current) in message
    )


def _state(root: Path) -> dict:
    state, _version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    return state


def _comment_ids(state: dict) -> set[str]:
    return {
        str(item.get("id"))
        for items in (state.get("comments") or {}).values()
        for item in items
    }


def _race(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: Callable[[], object],
    *,
    rendezvous: Rendezvous | None = None,
) -> dict:
    """Run a landing write and ``writer`` so two writers meet on one plan.

    Both threads meet at ``_store._write_state`` — after each has read the
    plan and passed its own version check, before either takes the plan's
    write lock. Both threads can arrive there; the lock then decides which
    writer proceeds and which meets the fenced path.
    """
    meeting = rendezvous or Rendezvous()
    write_state = _store._write_state

    def meeting_first(*args, **kwargs):
        meeting.meet(threading.current_thread().name)
        return write_state(*args, **kwargs)

    monkeypatch.setattr(_store, "_write_state", meeting_first)
    outcomes: dict[str, object] = {}

    def run(name: str, call: Callable[[], object]) -> None:
        try:
            outcomes[name] = call()
        except Exception as exc:  # noqa: BLE001 — the meeting's own failure modes
            outcomes[name] = exc

    landing = threading.Thread(
        target=run,
        args=("landing", lambda: _landing_write(root, "c-landing")),
        name="landing",
    )
    other = threading.Thread(target=run, args=("writer", writer), name="writer")
    landing.start()
    other.start()
    landing.join(timeout=30)
    other.join(timeout=30)
    monkeypatch.setattr(_store, "_write_state", write_state)
    return outcomes


def _wrote_followup(state: dict) -> bool:
    return "f-race" in {str(item.get("id")) for item in (state.get("followups") or [])}


def _wrote_section(state: dict) -> bool:
    return "s-race" in {str(item.get("id")) for item in (state.get("sections") or [])}


def _wrote_state_set(state: dict) -> bool:
    return state.get("owner") == "w-race"


def _wrote_comment(state: dict) -> bool:
    return "c-edit" in _comment_ids(state)


def _landing_record_writer(root: Path, version: int) -> Callable[[], object]:
    return lambda: _landing_write(root, "c-second-landing")


def _followup_writer(root: Path, version: int) -> Callable[[], object]:
    item = {
        "id": "f-race",
        "written_by": "w-race",
        "written_at": "2026-10-02T00:00:00Z",
        "title": "A followup appended mid-landing",
        "body": "<p>Body</p>",
        "prompt": "/reckon-build race-target §1",
    }
    return lambda: _edit(
        root, [{"op": "append", "target": "followups", "item": item}], version
    )


def _section_writer(root: Path, version: int) -> Callable[[], object]:
    return lambda: _edit(
        root,
        [{"op": "append", "target": "sections", "item": dict(SECTION_RECORD)}],
        version,
    )


def _state_set_writer(root: Path, version: int) -> Callable[[], object]:
    return lambda: _edit(
        root, [{"op": "set", "path": "owner", "value": "w-race"}], version
    )


def _comment_writer(root: Path, version: int) -> Callable[[], object]:
    item = _comment("c-edit", "editor")
    return lambda: _edit(
        root,
        [{"op": "append", "target": "comments", "section": ANCHOR, "item": item}],
        version,
    )


def _initial_version(root: Path) -> int:
    _state_dict, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    return version


def _race_case(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_writer: Callable[[Path, int], Callable[[], object]],
    change_present: Callable[[dict], bool],
    *,
    rendezvous: Rendezvous | None = None,
) -> None:
    version = _initial_version(root)
    outcomes = _race(
        root, monkeypatch, build_writer(root, version), rendezvous=rendezvous
    )
    missed = [
        outcome
        for outcome in outcomes.values()
        if isinstance(outcome, RendezvousMissedError)
    ]
    if missed:
        raise AssertionError(
            "the writers never met, so this case measured no race: "
            + " | ".join(str(miss) for miss in missed)
        )
    state = _state(root)
    comment_ids = _comment_ids(state)
    landing_present = "c-landing" in comment_ids
    writer_present = change_present(state)
    outcome = outcomes.get("writer")
    refused = _refused_naming_both_versions(outcome)

    if not (landing_present and writer_present) and not (refused and landing_present):
        raise AssertionError(
            "a writer was lost on the meeting without a refusal naming both "
            f"versions: comments={sorted(comment_ids)} "
            f"landing_present={landing_present} writer_present={writer_present} "
            f"writer_outcome={outcome!r} landing_outcome={outcomes.get('landing')!r}"
        )
    if refused and writer_present:
        raise AssertionError(
            "a writer both landed its change and reported a conflict: "
            f"outcome={outcome!r} change_present={writer_present}"
        )

    # Positive control: a sequential write after the meeting still succeeds
    # and still advances the version, so a fence that refuses everything
    # cannot pass this test.
    before = int(_state(root).get("version", 0))
    sequential = _edit(
        root, [{"op": "set", "path": "owner", "value": "after-race"}], before
    )
    assert sequential.get("ok") is True, sequential
    assert sequential.get("new_version") == before + 1, sequential
    assert _state(root).get("owner") == "after-race"


@pytest.mark.parametrize(
    ("build_writer", "change_present"),
    [
        pytest.param(
            _landing_record_writer,
            lambda state: {"c-landing", "c-second-landing"} <= _comment_ids(state),
            id="landing-record",
        ),
        pytest.param(_followup_writer, _wrote_followup, id="followup-append"),
        pytest.param(_section_writer, _wrote_section, id="section-append"),
        pytest.param(_state_set_writer, _wrote_state_set, id="state-set"),
        pytest.param(_comment_writer, _wrote_comment, id="comment-append"),
    ],
)
def test_each_writer_racing_a_landing_write_commutes_or_is_refused(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_writer: Callable[[Path, int], Callable[[], object]],
    change_present: Callable[[dict], bool],
) -> None:
    _race_case(repository, monkeypatch, build_writer, change_present)


def test_a_broken_rendezvous_fails_the_case_rather_than_passing_it(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one writer rigged to miss the meeting, the case must report it.

    A rendezvous that breaks must fail the case against the thread that missed
    it. Falling through would judge writes the writers never raced, which is
    the silent success this instrument exists to rule out.
    """
    with pytest.raises(AssertionError) as failure:
        _race_case(
            repository,
            monkeypatch,
            _state_set_writer,
            _wrote_state_set,
            rendezvous=Rendezvous(timeout=0.5, absent="writer"),
        )
    message = str(failure.value)
    assert "rendezvous" in message, message
    assert "writer" in message, message


def test_a_sequential_write_still_succeeds_and_advances_the_version(
    repository: Path,
) -> None:
    _landing_write(repository, "c-sequential")
    before = _state(repository)
    outcome = _edit(
        repository,
        [{"op": "set", "path": "owner", "value": "sequential"}],
        int(before["version"]),
    )
    assert outcome.get("ok") is True, outcome
    assert outcome.get("new_version") == int(before["version"]) + 1, outcome
    after = _state(repository)
    assert after.get("owner") == "sequential"
    assert int(after["version"]) == int(before["version"]) + 1
    assert "c-sequential" in _comment_ids(after)
