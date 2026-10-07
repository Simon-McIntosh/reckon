"""The recorded fleet's tabs and conversations survive layout migration."""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import fleet_migrate

FIXTURE = Path(__file__).parent / "fixtures" / "fleet_migrate"


def _source(tmp_path: Path) -> dict:
    rows = list(csv.DictReader((FIXTURE / "resume.tsv").open(), delimiter="\t"))
    transcript_root = tmp_path / "transcripts"
    records = {}
    for row in rows:
        conversation = row["conversation"]
        cwd = row["cwd"]
        records[conversation] = {"conversation": conversation, "cwd": cwd}
        path = transcript_root / cwd.replace("/", "-") / f"{conversation}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    return {
        "layouts": {
            path.stem: path.read_text() for path in (FIXTURE / "layouts").glob("*.kdl")
        },
        "claude": records,
        "starts": [row["conversation"] for row in rows],
        "transcript_root": str(transcript_root),
    }


def _identity(session: dict) -> list[tuple[str, str | None, str]]:
    return [
        (tab["name"], pane.get("conversation"), pane["cwd"])
        for tab in session["tabs"]
        for pane in tab["panes"]
    ]


def test_recorded_layouts_keep_tab_order_and_pane_identity(tmp_path: Path) -> None:
    source = _source(tmp_path)
    census = fleet_migrate.build_census(source)
    assert len(census["sessions"]) == 5
    assert (
        sum(
            bool(pane.get("conversation"))
            for session in census["sessions"]
            for tab in session["tabs"]
            for pane in tab["panes"]
        )
        == 13
    )
    assert census["missing_start_log"] == []
    by_name = {session["name"]: session for session in census["sessions"]}
    assert [tab["name"] for tab in by_name["ambix-fleet"]["tabs"]] == [
        "ids",
        "map",
        "alembic",
    ]
    assert [tab["name"] for tab in by_name["nova-fleet"]["tabs"]] == [
        "plan",
        "S22",
        "S21",
    ]

    for name, session in by_name.items():
        rendered = fleet_migrate.render_layout(session)
        parsed = fleet_migrate.parse_layout(rendered)
        assert [tab["name"] for tab in parsed] == [
            tab["name"] for tab in session["tabs"]
        ], name
        actual = [
            (tab["name"], pane.get("args", [None, None])[1], pane["cwd"])
            for tab in parsed
            for pane in tab["panes"]
        ]
        assert actual == _identity(session), name
        assert "contents_file" not in rendered
        assert rendered.count('args "--resume"') == sum(
            bool(pane.get("conversation"))
            for tab in session["tabs"]
            for pane in tab["panes"]
        )


@pytest.mark.parametrize(
    ("name", "focused_tab"),
    [("ambix-fleet", "ids"), ("nova-fleet", "S22")],
)
def test_rendered_layout_keeps_zellij_chrome_and_focus(
    tmp_path: Path, name: str, focused_tab: str
) -> None:
    source = _source(tmp_path)
    session = next(
        item
        for item in fleet_migrate.build_census(source)["sessions"]
        if item["name"] == name
    )
    rendered = fleet_migrate.render_layout(session)
    original = source["layouts"][name]
    tab_count = len(session["tabs"])
    assert original.count('plugin location="zellij:tab-bar"') == tab_count + 1
    assert rendered.count('plugin location="zellij:tab-bar"') == original.count(
        'plugin location="zellij:tab-bar"'
    )
    assert rendered.count("pane size=1 borderless=true {") >= tab_count
    assert original[original.index("    new_tab_template {") :] in rendered
    assert f'tab name="{focused_tab}" focus=true' in rendered
    assert 'pane command="fleet-claude" focus=true' in rendered


def test_resume_table_matches_layout_panes(tmp_path: Path) -> None:
    census = fleet_migrate.build_census(_source(tmp_path))
    rows = list(csv.DictReader((FIXTURE / "resume.tsv").open(), delimiter="\t"))
    expected = {
        (row["zellij_session"], row["tab"], row["conversation"], row["cwd"])
        for row in rows
    }
    actual = {
        (session["name"], tab["name"], pane["conversation"], pane["cwd"])
        for session in census["sessions"]
        for tab in session["tabs"]
        for pane in tab["panes"]
        if pane.get("conversation")
    }
    assert actual == expected


def test_missing_start_log_is_reported_from_census(tmp_path: Path) -> None:
    source = _source(tmp_path)
    missing = source["starts"].pop()
    state = tmp_path / "state"
    result = fleet_migrate.migrate(
        state=state,
        layouts_dir=tmp_path / "layouts",
        observation=lambda: source,
        read_runs=list,
    )
    assert missing in result
    ledger = json.loads(
        next((state / "migration").glob("move-*/ledger.json")).read_text()
    )
    assert ledger["census"]["missing_start_log"] == [missing]


def test_census_refuses_missing_session_record_and_transcript(tmp_path: Path) -> None:
    source = _source(tmp_path)
    conversation = next(iter(source["claude"]))
    source["claude"].pop(conversation)
    with pytest.raises(
        fleet_migrate.MigrationError, match="no live Claude session record"
    ):
        fleet_migrate.build_census(source)
    source = _source(tmp_path)
    record = source["claude"][conversation]
    path = (
        Path(source["transcript_root"])
        / record["cwd"].replace("/", "-")
        / f"{conversation}.jsonl"
    )
    path.unlink()
    with pytest.raises(fleet_migrate.MigrationError, match="transcript missing"):
        fleet_migrate.build_census(source)


def test_dry_run_changes_nothing_and_next_invocation_resumes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    layouts = tmp_path / "zellij" / "layouts"
    source = _source(tmp_path)

    def observe():
        return source

    def live():
        return [
            {
                "run_id": "run-example",
                "resume": "reckon crew resume --run run-example --advice continue",
            }
        ]

    before = list(tmp_path.rglob("*"))
    assert fleet_migrate.migrate(
        dry_run=True,
        state=state,
        layouts_dir=layouts,
        observation=observe,
        read_runs=live,
    ).startswith("census:")
    assert list(tmp_path.rglob("*")) == before
    assert "next step: layout" in fleet_migrate.migrate(
        state=state, layouts_dir=layouts, observation=observe, read_runs=live
    )
    ledger_path = next((state / "migration").glob("move-*/ledger.json"))
    census_ledger = ledger_path.read_bytes()
    assert fleet_migrate.migrate(
        dry_run=True,
        state=state,
        layouts_dir=layouts,
        observation=observe,
        read_runs=live,
    ).startswith("layout:")
    assert ledger_path.read_bytes() == census_ledger
    assert not layouts.exists()
    ledger = json.loads(ledger_path.read_text())
    conversation = next(
        pane["conversation"]
        for item in ledger["census"]["sessions"]
        for tab in item["tabs"]
        for pane in tab["panes"]
        if pane.get("conversation")
    )
    ledger["continue_prompts"][conversation] = "Continue from the checkpoint."
    ledger_path.write_text(json.dumps(ledger))
    assert "next step: stand-up" in fleet_migrate.migrate(
        state=state, layouts_dir=layouts, observation=observe, read_runs=live
    )
    ledger = json.loads(ledger_path.read_text())
    assert ledger["completed"] == ["census", "layout"]
    assert ledger["next_step"] == "stand-up"
    assert len(list(layouts.glob("migrate-*.kdl"))) == 5
    assert any(
        "Continue from the checkpoint." in path.read_text()
        for path in layouts.glob("migrate-*.kdl")
    )
    assert fleet_migrate.migrate(
        dry_run=True, state=state, layouts_dir=layouts
    ).startswith("stand-up:")


def test_command_dry_run_uses_isolated_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = CliRunner().invoke(
        main,
        ["fleet-node", "migrate", "--dry-run"],
        env={"FLEET_STATE_DIR": str(state)},
    )
    assert result.exit_code == 0, result.output
    assert "census:" in result.output
    assert not state.exists()


def test_local_census_uses_zellij_and_live_claude_records(tmp_path: Path) -> None:
    source = _source(tmp_path)
    record = next(iter(source["claude"].values()))
    pid = 1234
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    (sessions / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": record["conversation"],
                "cwd": record["cwd"],
                "procStart": "456",
                "status": "idle",
            }
        )
    )
    stat = tmp_path / "proc" / str(pid) / "stat"
    stat.parent.mkdir(parents=True)
    stat.write_text(f"{pid} (claude) " + " ".join(["S", *(["0"] * 18), "456"]))
    state = tmp_path / "state"
    state.mkdir()
    (state / "sessions.tsv").write_text(record["conversation"] + "\n")
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        output = (
            "ambix-fleet (active)\nother-session (active)\n"
            if argv[1] == "list-sessions"
            else source["layouts"]["ambix-fleet"]
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    observed = fleet_migrate._local_observation(
        state=state,
        claude_sessions=sessions,
        transcript_root=Path(source["transcript_root"]),
        process_root=tmp_path / "proc",
        invoke=invoke,
    )
    assert list(observed["layouts"]) == ["ambix-fleet"]
    assert record["conversation"] in observed["claude"]
    assert len(calls) == 2
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before


def test_continue_prompt_follows_resume_id(tmp_path: Path) -> None:
    session = fleet_migrate.build_census(_source(tmp_path))["sessions"][0]
    pane = next(
        pane
        for tab in session["tabs"]
        for pane in tab["panes"]
        if pane.get("conversation")
    )
    conversation = pane["conversation"]
    prompt = "Continue from the recorded checkpoint."
    parsed = fleet_migrate.parse_layout(
        fleet_migrate.render_layout(session, {conversation: prompt})
    )
    args = next(
        pane["args"]
        for tab in parsed
        for pane in tab["panes"]
        if pane.get("args", [None, None])[1] == conversation
    )
    assert args == ["--resume", conversation, prompt]


def test_live_run_census_uses_resumption_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def list_live():
        calls.append("list_live")
        return [{"run_id": "run-example", "project": "example", "phase": "running"}]

    def resolve(run_id, **kwargs):
        calls.append((run_id, kwargs["project"]))
        return {
            "resolved": True,
            "session_id": "conversation-example",
            "source": "live-pointer",
        }

    monkeypatch.setattr(fleet_migrate.runs, "list_live", list_live)
    monkeypatch.setattr(fleet_migrate, "resolve_session", resolve)
    rows = fleet_migrate._read_runs()
    assert calls == ["list_live", ("run-example", "example")]
    assert rows[0]["resume"] == "reckon crew resume --run run-example --advice continue"
    assert rows[0]["session_id"] == "conversation-example"
