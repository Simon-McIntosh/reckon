"""Every crew list view names the section its run was dispatched for.

A run is dispatched for one section of one plan, and four read views list it:
``live``, ``runs``, ``obligations`` and ``directory``. Each builds its rows and
routes them through :func:`reckon.crew.query.project_run_rows`, so ``section``
is carried wherever the run is listed, ``fields=["section"]`` is accepted by all
four, and a field a view does not carry is refused naming the fields it does.

The repository, the ledger and the live pointers are synthesised under the
test's own ``tmp_path`` and isolated reckon home, so nothing outside them is
read.
"""

from __future__ import annotations

import json
import os
import subprocess
from html import escape
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon.crew import runs
from reckon.mcp_views import serve_crew_view
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "alpha"
BRANCH = "main"
RUN_ID = "r-fixture-run"
SESSION = "coordinator-session"
PLAN = "the-plan"
#: What a caller dispatched with. The row carries the canonical spelling of the
#: same identity, which is the one grammar's `#section` figure reduced to its
#: canonical form, so the four views cannot disagree about which section it is.
SECTION = "s4"
#: The canonical spelling, written out rather than derived: comparing the views
#: against the same normalizer the projection applies would pass whatever that
#: normalizer returned, including a spelling no reader expects.
CANONICAL_SECTION = "§4"

#: The fields a runs row declares and serves whole: the ones it answers by
#: default plus the optional ones a caller may ask for. Written out rather than
#: read from the view's own constant, because the row's shape is a contract a
#: reader relies on and a set compared against the declaration it came from
#: would pass whatever that declaration said.
RUN_ROW_FIELDS = (
    "run_id",
    "node",
    "plan",
    "section",
    "source",
    "classification",
    "process_alive",
    "session_id",
    "session_id_source",
    "worktree",
    "worktree_exists",
    "transcript_path",
    "transcript_exists",
    "resumable",
    "resumable_reason",
)
#: The fields this view answers only when asked, so a row that was not asked
#: carries none of them however easily it could fill them.
RUN_OPTIONAL_FIELDS = (
    "lane",
    "model_key",
    "member",
    "agent",
    "base_sha",
    "manifest_present",
    "commits",
    "commits_beyond_base",
    "log_age_seconds",
    "manifest_reported_status",
)

_VIEWS = ("live", "runs", "obligations", "directory")

#: The identity a worker's own git wrapper reads; dropped so the synthesised
#: repository is the only one in play.
_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _git(repo: Path, *arguments: str) -> str:
    base = {**os.environ}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _write_pointer(
    run_id: str,
    *,
    session: str,
    plan: str,
    section: str,
    worktree: Path,
    manifest_path: Path,
) -> None:
    """One live pointer, reading as a run whose worker asked for help.

    A run that merely vanished owes no duty, so the obligations view would list
    nothing and the section it carries could not be read there. The manifest's
    own help line is what makes the run an outstanding duty.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "phase": "blocked",
            "process_alive": False,
            "repo": str(worktree),
            "worktree": str(worktree),
            "manifest_path": str(manifest_path),
            "node": {
                "id": "a-node",
                "plan": plan,
                "section": section,
                "time_budget": "20m",
                "write_paths": ["docs"],
            },
        },
    )


@pytest.fixture()
def fleet(tmp_path: Path, isolated_reckon_home: Path) -> Path:
    """One mounted repository with a seeded ledger, plus one live pointer."""
    repo = tmp_path / "code" / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    ledger = {
        "data": {
            "members": [],
            "runs": [
                {
                    "run_id": RUN_ID,
                    "node": "a-node",
                    "plan": PLAN,
                    "session": SESSION,
                    "role": "implement",
                    "gate": "passed",
                    "backend": "claude",
                    "dispatched_at": "2026-09-01T00:00:00Z",
                    "completed_at": "2026-09-01T00:01:40Z",
                    "worker_seconds": 100,
                    "lineage": {},
                    "attempt": 1,
                }
            ],
            "holds": [],
        }
    }
    state_dir = repo / "docs" / "state" / PROJECT
    state_dir.mkdir(parents=True)
    (state_dir / "crew.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", f"docs/state/{PROJECT}/crew.json", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed the fixture")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    manifest = repo / "manifest.md"
    manifest.write_text(
        "NEEDS-HELP: the fixture worker cannot parse its input\n"
        "status: blocked\n\n"
        "tried: nothing\noptions: none\nleaning: none\ncost-if-wrong: none\n",
        encoding="utf-8",
    )
    _write_pointer(
        RUN_ID,
        session=SESSION,
        plan=PLAN,
        section=SECTION,
        worktree=repo,
        manifest_path=manifest,
    )
    return repo


def _run_rows(payload: dict) -> list[dict]:
    """Every run row a view answers, whatever shape the view nests them in."""
    if "rows" in payload:
        return list(payload["rows"])
    if "runs" in payload:
        return list(payload["runs"])
    rows: list[dict] = []
    for key in ("obligations", "acknowledged", "unowned_runs"):
        rows.extend(payload.get(key) or [])
    for coordinator in payload.get("coordinators") or []:
        rows.extend(coordinator.get("runs") or [])
    return rows


def _view_call(view: str, repository: Path, **kwargs) -> dict:
    """Drive one view for the fixture's single run.

    ``obligations`` is derived for one session and is read with one; ``runs``
    reads both the live pointers and the committed ledger, so it is pinned to
    the live source, where the fixture's run is the one row, rather than being
    joined with a ledger row that records no section of its own.
    """
    arguments: dict = {"project": PROJECT, "checkout_path": str(repository)}
    if view == "runs":
        arguments["source"] = "live"
    else:
        arguments["session"] = SESSION
    return serve_crew_view(view, **arguments, **kwargs)


@pytest.mark.parametrize("view", _VIEWS)
def test_every_run_view_names_the_section(view: str, fleet: Path) -> None:
    payload = _view_call(view, fleet)
    rows = _run_rows(payload)
    assert rows, f"{view} listed no run: {payload.get('error')} {payload.get('detail')}"
    assert [row.get("section") for row in rows] == [CANONICAL_SECTION] * len(rows)


@pytest.mark.parametrize("view", _VIEWS)
def test_every_run_view_accepts_the_section_field(view: str, fleet: Path) -> None:
    payload = _view_call(view, fleet, fields=["section"])
    rows = _run_rows(payload)
    assert rows, f"{view} listed no run: {payload.get('error')} {payload.get('detail')}"
    # What a request carries differs by view, and each difference is that view's
    # contract rather than this case's. A view serving a compact row carries the
    # shared run identity beside the field asked for, so a caller reading one
    # fact still sees which run, plan and section answered. The runs view serves
    # its whole declared row, so a request adds to that row instead of cutting
    # it down, and the row is exactly the declaration. Neither arm may carry an
    # optional field nobody asked for — the default-read case below holds that
    # for the runs view on its own.
    expected = (
        set(RUN_ROW_FIELDS)
        if view == "runs"
        else {"run_id", "node", "plan", "section", "mine"}
    )
    for row in rows:
        assert row.get("section") == CANONICAL_SECTION
        assert not set(row) & set(RUN_OPTIONAL_FIELDS), row
        if view == "runs":
            assert set(row) == expected, row
        else:
            assert set(row) <= expected, row


@pytest.mark.parametrize("view", _VIEWS)
def test_a_field_a_view_lacks_is_refused_naming_its_fields(
    view: str, fleet: Path
) -> None:
    payload = _view_call(view, fleet, fields=["not_a_field_here"])
    assert payload["ok"] is False
    detail = payload["detail"]
    assert "not_a_field_here" in detail
    assert "fields are" in detail


def test_a_runs_row_is_the_fields_it_declares(fleet: Path) -> None:
    """A runs row is the view's declared row, whatever this run filled in.

    The row's shape is the read model the crew tool documents, so it does not
    depend on which fields the run it describes happened to fill: the fixture's
    pointer names no transcript, and ``transcript_path`` is on the row, empty,
    because the view can answer for the field at all. A field the view answers
    only on request is absent until it is asked for, so a default read carries
    no field the caller did not ask for and the view does not serve by default —
    and ``brief_sha256`` is absent because this run's authority is a plan
    section, whose key is what distinguishes the two authorities.
    """

    rows = _run_rows(_view_call("runs", fleet))
    assert rows, "the live arm listed no run"
    live = rows[0]

    # What the run does have is stated.
    assert live["plan"] == PLAN
    assert live["section"] == CANONICAL_SECTION
    assert live["source"] == "live"

    # The declared row is served whole: a field this run left empty is present
    # with its empty value rather than left off, so a reader tells a field the
    # view cannot answer from a value this run has none of.
    assert set(live) == set(RUN_ROW_FIELDS), live
    assert live["transcript_path"] in (None, ""), live
    assert not set(live) & set(RUN_OPTIONAL_FIELDS), live
    assert "brief_sha256" not in live, live

    # An optional field the view can answer joins the same row when asked for,
    # present whether or not this run has a value for it — the fixture's pointer
    # names no member, and the key is on the row all the same.
    asked = _run_rows(_view_call("runs", fleet, fields=["member"]))[0]
    assert set(asked) == set(RUN_ROW_FIELDS) | {"member"}, asked
    assert asked["member"] in (None, ""), asked

    # The same holds on the committed arm, where the ledger records neither a
    # classification nor a worktree for the run.
    committed = _run_rows(
        serve_crew_view(
            "runs", project=PROJECT, checkout_path=str(fleet), source="ledger"
        )
    )
    assert committed, "the committed arm listed no run"
    assert set(committed[0]) == set(RUN_ROW_FIELDS), committed[0]
    assert committed[0]["classification"] in (None, "", {}), committed[0]
    assert committed[0]["worktree"] in (None, ""), committed[0]


# ── a view's fields are its own, not the ones its rows happen to hold ───────


def test_obligations_accepts_its_fields_when_nothing_is_acknowledged(
    fleet: Path,
) -> None:
    """The two duty lists are one vocabulary, however empty either of them is.

    The fixture owes one duty and has acknowledged none, which is the ordinary
    case: a view whose accepted fields collapsed to what the lists in hand carry
    would refuse ``kind`` on the strength of the empty list.
    """

    payload = serve_crew_view(
        "obligations",
        project=PROJECT,
        session=SESSION,
        checkout_path=str(fleet),
        fields=["kind", "age_seconds"],
    )
    assert payload.get("ok") is True, payload
    assert payload["acknowledged"] == []
    rows = payload["obligations"]
    assert rows, payload
    assert rows[0]["kind"] == "needs-help"
    assert rows[0]["age_seconds"] == 0
    assert set(rows[0]) <= {"run_id", "node", "plan", "section", "kind", "age_seconds"}


def test_directory_accepts_a_field_the_unowned_list_cannot_carry(fleet: Path) -> None:
    """A coordinator row's own fields are the view's, though no run is unowned.

    Every run in the fixture belongs to a session, so ``unowned_runs`` is empty;
    ``phase`` rides only the coordinator's rows, and asking for it must not be
    refused on the strength of the list that cannot carry it.
    """

    payload = serve_crew_view(
        "directory", project=PROJECT, checkout_path=str(fleet), fields=["phase"]
    )
    assert payload.get("ok") is True, payload
    assert payload["unowned_runs"] == []
    rows = payload["coordinators"][0]["runs"]
    assert rows, payload
    assert rows[0]["phase"] == "blocked"
    assert set(rows[0]) <= {"run_id", "node", "plan", "section", "phase"}


def test_a_narrowed_runs_row_still_answers_the_views_own_filters(
    fleet: Path,
) -> None:
    """A request that narrows the row does not narrow what the filters read.

    The runs view filters on ``classification`` and ``resumable`` before the row
    is projected, so a build narrowed to the request would answer every such
    filter with an absent key and list nothing. The fixture's run is blocked and
    is not resumable, so both filters must keep it — and a filter naming a
    classification the run does not carry must still drop it.
    """

    matched = _run_rows(
        _view_call("runs", fleet, fields=["section"], classification="blocked")
    )
    assert matched, "the classification filter dropped the run it names"
    assert matched[0]["section"] == CANONICAL_SECTION

    resumable = _run_rows(
        _view_call("runs", fleet, fields=["section"], resumable=False)
    )
    assert resumable, "the resumable filter dropped the run it names"

    dropped = _run_rows(
        _view_call("runs", fleet, fields=["section"], classification="promotable")
    )
    assert dropped == [], dropped


# ── a row-level dependency states the section's own condition ───────────────

_LEDGER_PROJECT = "beta"
_DONE_PLAN = "prerequisite"
_WAITING_PLAN = "consumer"
_DONE_SECTION = "s3"
_WAITING_SECTION = "s1"


def _plan_meta(name: str, value: str) -> str:
    return f'<meta name="{name}" content="{escape(value, quote=True)}">'


def _write_plan(
    docs: Path,
    slug: str,
    *,
    sections: dict[str, str],
    section_depends_on: dict[str, list[str]] | None = None,
) -> Path:
    """One in-progress plan whose sections carry the given declarations."""

    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="docs-project" content="{_LEDGER_PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        _plan_meta("plan-slug", slug),
        _plan_meta("plan-title", slug),
        '<meta name="plan-status" content="active">',
        '<meta name="plan-effort" content="M">',
        _plan_meta(
            "plan-section-declarations",
            json.dumps(sections, separators=(",", ":")),
        ),
    ]
    if section_depends_on:
        head.append(
            _plan_meta(
                "plan-section-depends-on",
                json.dumps(section_depends_on, separators=(",", ":")),
            )
        )
    head.append(f"<title>{slug}</title></head>")
    body = ['<body><main class="plan-doc">']
    for name, declaration in sorted(sections.items()):
        body.append(
            f'<h2 id="{name}">Section</h2>'
            '<section data-reckon="section"'
            f' data-id="{name}"'
            ' data-effort-hours="2"'
            ' data-capability-version="1.0" data-capability-class="general"'
            ' data-capability-reasoning="standard"'
            ' data-capability-verification="standard"'
            ' data-capability-risk="low"'
            f' data-status="{declaration}" data-links=""></section>'
        )
    body.append("</main></body></html>")
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(head) + "".join(body), encoding="utf-8")
    return path


@pytest.fixture()
def section_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A done section of a plan still in progress, waited on by another plan.

    Both plans are ``active``, so the containing plan's status cannot answer
    for the section: only the section's own declaration can.
    """

    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({_LEDGER_PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    _write_plan(
        docs,
        _DONE_PLAN,
        sections={_DONE_SECTION: "done", "s9": "implementable"},
    )
    _write_plan(
        docs,
        _WAITING_PLAN,
        sections={_WAITING_SECTION: "implementable"},
        section_depends_on={_WAITING_SECTION: [f"{_DONE_PLAN}#{_DONE_SECTION}"]},
    )
    return _LEDGER_PROJECT, docs


def test_a_section_row_states_the_sections_declaration(section_waits) -> None:
    """A done section of an in-progress plan reads done, not the plan's status."""

    project, _docs = section_waits
    result = mcp_module._roadmap(project)
    row = next(item for item in result["pending_work"] if item["slug"] == _WAITING_PLAN)

    (edge,) = row["section_depends_on"]

    assert edge["ref"] == f"{_DONE_PLAN}#{_DONE_SECTION}"
    assert edge["status"] == "done"
    assert edge["section_found"] is True
    assert edge["satisfied"] is True


def test_a_done_section_of_an_in_progress_plan_blocks_nothing(section_waits) -> None:
    """The section's own verdict is read, so a satisfied wait is not reported."""

    project, _docs = section_waits
    summary = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": _WAITING_PLAN}
    )

    assert summary["section_blocking"] == []
    assert summary["blocking"] == []
