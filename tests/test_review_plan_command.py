"""Plan review composition, quiet eligibility, responses and the read surface."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, flight, mcp
from reckon.crew import recovery_repair_dispatch
from reckon.crew import recovery_review_acceptance
from reckon.crew import recovery_review_delivery
from reckon.crew import recovery_review_dispatch
from reckon.crew import recovery_watch
from reckon.crew import plan_review, recovery, review, routing, runs
from reckon.crew.node import PlanReviewMissingError, TaskNode

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / "fixture.html"
    path.write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        "<p>Extend the existing mechanism.</p></body></html>",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=CONFIG))
    return home, repo, path


def _subject(**kwargs):
    return recovery.plan_review_subject("sample", "fixture", "coordinator", **kwargs)


def _stored(path):
    record = {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 3,
        "reviewed_blob_sha": "a" * 40,
        "plan_fingerprint": plan_review.plan_fingerprint(path),
        "rubric": "design",
        "findings": [
            {"id": "reuse-owner", "type": "reuse_search", "text": "Name the owner."}
        ],
        "responses": {},
    }
    plan_review.store_plan_review(record)
    return record


@pytest.mark.parametrize(
    "rubric,items",
    [
        ("design", review.PLAN_DESIGN_REVIEW_ITEMS),
        ("content", review.PLAN_REVIEW_ITEMS),
    ],
)
def test_composer_carries_rubric_snapshot_and_sidecar(project, rubric, items):
    _, repo, path = project
    subject = _subject(rubric=rubric)
    fields = recovery._review_dispatch_fields(subject)
    assert fields["node_id"] == "plan-review-of-fixture"
    brief = Path(fields["brief"]).read_text()
    assert all(item in brief for item in items)
    assert str(path) in brief and str(repo) in brief
    assert "RUBRIC" in brief and "FINDING" in brief
    directory = plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    )
    assert fields["write_paths"] == [str(directory)]
    assert (directory / "plan.html").read_bytes() == path.read_bytes()
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    blob = subprocess.check_output(
        ["git", "hash-object", str(path)], cwd=repo, text=True
    ).strip()
    assert sidecar["plan_slug"] == "fixture"
    assert sidecar["plan_version"] == 3
    assert sidecar["reviewed_blob_sha"] == blob
    assert sidecar["plan_fingerprint"] == plan_review.plan_fingerprint(path)
    assert sidecar["section_digests"] == plan_review._section_digests(path)
    assert sidecar["rubric"] == rubric
    argv = recovery._review_dispatch_argv(subject, config=CONFIG)
    assert argv[argv.index("--node") + 1] == fields["node_id"]
    assert argv[argv.index("--role") + 1] == "review"
    assert argv[argv.index("--spec-level") + 1] == "exact"
    assert argv[argv.index("--brief") + 1] == fields["brief"]
    assert "--plan" not in argv


@pytest.mark.parametrize("write", [True, False])
def test_composed_brief_carries_the_weekly_interface_counts(project, write):
    home, repo, _ = project
    package = repo / "reckon"
    package.mkdir()
    (package / "mcp_views.py").write_text(
        'import click\nVIEW_NAMES = ("summary", "detail")\n'
        '@click.command()\n@click.option("--name")\n'
        "def command(name):\n    return name\n"
        'def refuse():\n    return format_refusal("missing-input")\n'
    )
    for args in (
        ["add", "reckon/mcp_views.py"],
        ["commit", "-q", "-m", "feat: expose fixture interfaces"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    subject = _subject()
    before = sorted(home.rglob("*"))

    fields = recovery._review_dispatch_fields(subject, write=write)

    match = re.search(
        r"Interface budget this week: public_definitions (\d+), cli_options (\d+), "
        r"mcp_views (\d+), refusal_families (\d+) \(read ([^)]+)\)",
        fields["brief_text"],
    )
    assert match is not None, "the composed brief must carry measured interface counts"
    assert tuple(map(int, match.groups()[:4])) == (2, 1, 2, 1)
    assert match.group(5).endswith("Z")
    if write:
        assert Path(fields["brief"]).read_text() == fields["brief_text"]
    else:
        assert sorted(home.rglob("*")) == before


@pytest.mark.parametrize("legacy", [False, True], ids=["current", "legacy"])
@pytest.mark.parametrize("stored", [True, False], ids=["stored", "delivered"])
def test_gate_accepts_a_stored_or_delivered_review(project, legacy, stored):
    _, repo, path = project
    path.write_text(
        path.read_text().replace(
            "</head>",
            '<meta name="plan-section-declarations" '
            'content=\'{"delivery":"implementable"}\'></head>',
        )
    )
    current = plan_review.plan_fingerprint(path)
    previous = plan_review._fingerprint_forms(path)[1]
    assert current != previous
    fields = recovery._review_dispatch_fields(_subject())
    sidecar_path = Path(fields["sidecar"])
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["plan_fingerprint"] == current
    gate = {
        "node": TaskNode(
            id="delivery", goal="Build the fixture", plan="fixture", role="implement"
        ),
        "project": "sample",
        "repo": repo,
        "authority": {"plan": {"docs": str(repo / "docs"), "source": "repository"}},
        "enforce": True,
    }
    with pytest.raises(PlanReviewMissingError, match="no stored review"):
        routing.require_plan_reviewed(**gate)
    sidecar["plan_fingerprint"] = previous if legacy else current
    if stored:
        plan_review.store_plan_review({**sidecar, "findings": [], "responses": {}})
    else:
        sidecar_path.write_text(json.dumps(sidecar))
        Path(sidecar["report_path"]).write_text("RUBRIC reuse_search: pass\n")
    assert routing.require_plan_reviewed(**gate) is None
    path.write_text(path.read_text().replace("existing mechanism", "changed design"))
    with pytest.raises(PlanReviewMissingError, match="no stored review"):
        routing.require_plan_reviewed(**gate)


def test_plan_review_in_flight_is_shared_across_sessions(project, monkeypatch):
    subject = _subject()
    (monkeypatch.setattr(
        recovery_review_delivery,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": "another-coordinator",
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    ), monkeypatch.setattr(
        recovery_review_dispatch,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": "another-coordinator",
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    ), monkeypatch.setattr(
        recovery_repair_dispatch,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": "another-coordinator",
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    ), monkeypatch.setattr(
        recovery_review_acceptance,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": "another-coordinator",
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    ), monkeypatch.setattr(
        recovery_watch,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": "another-coordinator",
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    ))
    result = recovery.dispatch_review_for_run(subject, config=CONFIG)
    assert result["dispatched"] is False
    assert result["review_run_id"] == "standing-review"


def test_review_plan_dry_run_resolves_the_shared_composition(project):
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "coordinator",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["node"]["id"] == "plan-review-of-fixture"
    assert payload["node"]["role"] == "review"
    assert (
        payload["node"]["brief"]
        == payload["argv"][payload["argv"].index("--brief") + 1]
    )
    assert payload["dry_run"] is True
    assert payload["validation"]["ok"] is True
    assert runs.list_live(project="sample") == []


def _report_listing() -> list[str]:
    root = plan_review.review_report_directory("sample", "fixture", "unused").parent
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_review_plan_dry_run_leaves_the_report_root_unchanged(project):
    before = _report_listing()
    for _ in range(2):
        result = CliRunner().invoke(
            cli.main,
            [
                "crew",
                "review-plan",
                "--project",
                "sample",
                "--plan",
                "fixture",
                "--session",
                "coordinator",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert not Path(payload["node"]["brief"]).exists()
    after = _report_listing()
    assert after == before
    assert after == []


def test_review_plan_real_dispatch_writes_the_composed_artifacts(project, monkeypatch):
    _, _, _ = project
    dispatch = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(
        dispatch, "dispatch", lambda **kwargs: {"run_id": "launched-review"}
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "coordinator",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["review_run_id"] == "launched-review"
    root = plan_review.review_report_directory("sample", "fixture", "unused").parent
    run_directories = sorted(path for path in root.iterdir() if path.is_dir())
    assert len(run_directories) == 1
    directory = run_directories[0]
    brief = directory / "brief.md"
    assert brief.is_file() and "RUBRIC" in brief.read_text()
    snapshot = directory / "plan.html"
    assert snapshot.is_file() and snapshot.read_bytes() == project[2].read_bytes()
    sidecar = json.loads((directory / "plan-review.json").read_text())
    assert sidecar["plan_slug"] == "fixture"
    assert sidecar["report_path"] == str(directory / "report.md")


def test_review_plan_launch_passes_session_and_local(project, monkeypatch):
    dispatch = importlib.import_module("reckon.crew.dispatch")
    calls = []

    def launch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "launched-review"}

    monkeypatch.setattr(dispatch, "dispatch", launch)
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "named-session",
            "--local",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["review_run_id"] == "launched-review"
    assert calls[0]["session"] == "named-session"
    assert calls[0]["local"] is True


@pytest.mark.parametrize(
    "options",
    [
        ["--declined"],
        ["--declined", ""],
        ["--acted", "--declined", "reason"],
        [],
    ],
)
def test_answer_refuses_missing_reason_or_ambiguous_action(project, options):
    _stored(project[2])
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "reuse-owner",
            *options,
        ],
    )
    assert result.exit_code != 0
    assert plan_review.read_plan_review("sample", "fixture")["responses"] == {}


@pytest.mark.parametrize(
    "options,action",
    [
        (["--declined", "The existing owner already covers this."], "declined"),
        (["--acted"], "acted"),
    ],
)
def test_answer_records_response_through_store(project, options, action):
    _stored(project[2])
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "reuse-owner",
            *options,
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["unanswered"] == []
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored["responses"]["reuse-owner"]["action"] == action
    if action == "declined":
        assert stored["responses"]["reuse-owner"]["reason"] == options[1]


def test_mcp_view_returns_stored_unanswered_and_delivered(project):
    assert mcp._crew(project="sample", plan="fixture", view="plan-review") == {
        "record": None,
        "unanswered": [],
        "delivered": [],
    }
    _stored(project[2])
    fields = recovery._review_dispatch_fields(_subject())
    directory = Path(fields["sidecar"]).parent
    (directory / "report.md").write_text("RUBRIC reuse_search: pass\n")
    payload = mcp._crew(project="sample", plan="fixture", view="plan-review")
    assert payload["record"]["plan_slug"] == "fixture"
    assert payload["unanswered"] == ["reuse-owner"]
    assert payload["delivered"][0]["report_path"] == str(directory / "report.md")
    assert mcp._crew(project="sample", view="plan-review")["error"] == "missing_plan"
    assert mcp._crew(plan="fixture", view="plan-review")["error"] == "missing_project"


def test_local_review_keeps_the_selected_lane_when_picker_routing_is_enabled(project):
    config = {
        **CONFIG,
        "local_backend": "local-worker",
        "backends": {
            **CONFIG["backends"],
            "local-worker": CONFIG["backends"]["worker"],
        },
        "routing": {"picker": "route"},
    }
    payload = recovery.dispatch_review_for_run(
        _subject(local=True), config=config, dry_run=True
    )
    assert payload["backend"] == "local-worker"
    assert payload["route"] == "deterministic"
    assert payload["local"] is True
    assert "--local" in payload["argv"]


def test_launch_returns_harness_directive_and_writes_a_live_pointer(
    project, monkeypatch, tmp_path
):
    _, repo, _ = project
    dispatch = importlib.import_module("reckon.crew.dispatch")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    def prepare(*args):
        path = tmp_path / "review-worktree"
        path.mkdir()
        (path / "seed.txt").write_text("review workspace")
        return {"path": str(path), "base": "HEAD", "base_sha": head}

    monkeypatch.setattr(dispatch, "_create_worktree", prepare)
    payload = recovery.dispatch_review_for_run(_subject(), config=CONFIG)
    assert payload["dispatched"] is True, payload
    launched = payload["dispatch"]
    assert launched["launch"] == "in-harness"
    assert launched["directive"]
    pointer = runs.read_pointer(payload["review_run_id"])
    assert pointer["node"]["id"] == "plan-review-of-fixture"
    assert pointer["session"] == "coordinator"
    assert recovery._is_review_node(pointer)
    again = recovery.dispatch_review_for_run(_subject(), config=CONFIG)
    assert again["dispatched"] is False
    assert again["review_run_id"] == payload["review_run_id"]


def test_sidecar_hashes_exact_plan_bytes_and_keeps_snapshot_metadata(
    project, monkeypatch
):
    from reckon import _plan_html

    _, repo, path = project
    document = path.read_bytes().replace(b"><", b">\r\n<")
    path.write_bytes(document)
    read_state = _plan_html.read_state_file

    def read_snapshot(snapshot):
        path.write_bytes(document.replace(b'content="3"', b'content="4"'))
        return read_state(snapshot)

    monkeypatch.setattr(_plan_html, "read_state_file", read_snapshot)
    fields = recovery._review_dispatch_fields(_subject())
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    snapshot = Path(fields["sidecar"]).with_name("plan.html")
    assert snapshot.read_bytes() == document
    expected = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=document,
            cwd=repo,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    assert sidecar["reviewed_blob_sha"] == expected
    assert sidecar["plan_version"] == 3
    assert sidecar["plan_fingerprint"] == plan_review.plan_fingerprint(
        document.decode()
    )


def test_the_report_grammar_parses_through_the_shared_review_reader():
    parsed = plan_review.parse_review_report(
        "RUBRIC reuse_search: pass — the module already owns it.\n"
        "FINDING duplicate_owner reckon/crew/review.py:409 — the grammar has two "
        "owners — WOULD_CHANGE_THE_PLAN: yes — REASON: one module must parse "
        "reviewer text.\n",
        rubric="plan_design_review",
    )
    assert parsed["rubric_items"] == {
        "reuse_search": "pass — the module already owns it."
    }
    assert parsed["absent_items"] == [
        "deep_module",
        "thin_wrapper",
        "duplicate_owner",
        "interface_budget",
    ]
    assert parsed["findings"][0]["id"] == "duplicate_owner-1"
    assert parsed["findings"][0]["would_change"] is True


def test_a_plan_review_naming_no_lane_stays_off_an_excluded_default(project):
    lanes = {"launch": "cli", "model": "m", "effort": "high", "time_budget": "20m"}
    config = {
        **CONFIG,
        "default_backend": "metered",
        "local_backend": "served",
        "backends": {"metered": dict(lanes), "served": dict(lanes)},
        "review_excluded_backends": ["metered"],
        "roles": {"review": {"execution_capable": True}},
    }

    lane = recovery._composed_review_lane("sample", _subject(), config)

    assert lane == ["--local", "--backend", "served"]
    named = recovery._composed_review_lane("sample", _subject(local=True), config)
    assert named == ["--local", "--backend", "served"]
