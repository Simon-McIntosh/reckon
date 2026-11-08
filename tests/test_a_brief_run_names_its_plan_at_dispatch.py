"""A brief implementing run names the plan it belongs to when it is dispatched.

A brief implement or test run has no plan section to move, so nothing joins a
product change to a plan unless the dispatch says so. Landing already refuses to
promote such a run without one of two discharges — ``--plan-link`` naming the
plan whose product it changed, or ``--unplanned-reason`` stating why it changed
none — and meeting that refusal after the work is done costs the coordinator the
whole node. So the same two flags are asked for at the call that dispatches it,
and the value given is recorded on the run so landing needs no flag at all.

The refusal is composed in ``reckon/crew/node.py`` and called by the dispatch
command, above the pair split, so the live call, the ``--dry-run`` preview, and
both arms of a paired dispatch all reach it before any of them resolves a
request. Only that entry refuses: the production callers of ``plan_dispatch``
re-dispatch, re-resolve or re-lane a run that already exists, and have no owner
left to choose.

Every case here is hermetic. ``RECKON_HOME`` moves the transient crew directory
into a temp tree and the repository is a real but throwaway git repo, so no real
ledger or live pointer directory is touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew import queue_admission, runs
from tests.helpers.git import git as _git

PROJECT = "proj"
BRIEF_TEXT = (
    "Measure the paid lane's queue shape and record the bound you compared it\n"
    "against, so the next reader can tell the measurement from a guess.\n"
)
DONE_WHEN = "the gate command pytest exits 0 and prints the measured bound"

# A live pointer names the promoting process as the run's worker, so a promotion
# of one of these fixtures is admitted only with the live-run waiver an operator
# would give. What each case asserts is the landing discharge a coordinator
# meets, not the talking-to-itself waiver.
_PLUMBING_WAIVER = (
    "the pytest process stands in for the worker; this fixture exercises "
    "dispatch and landing plumbing"
)

CONFIG: dict = {
    "default_backend": "worker",
    "local_backend": "clive",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        },
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        },
    },
    "roles": {"implement": {}, "test": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

SESSION = "session-brief-owner"


@pytest.fixture()
def brief_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "seed.txt", "skills", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed fixture")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )

    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    return config_home, root, brief


def _lane_document(tmp_path: Path, *, worker_slots: float) -> Path:
    """A lane document whose admission block grants the session N worker slots."""
    lane = tmp_path / f"lane-{worker_slots}.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": worker_slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": worker_slots}},
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _cli_arguments(
    repo: Path,
    brief: Path,
    *,
    node_id: str,
    role: str = "implement",
    plan_link: str | None = None,
    unplanned_reason: str | None = None,
    dry_run: bool,
    local: bool = False,
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--brief",
        str(brief),
        "--role",
        role,
        "--spec-level",
        "exact",
        "--node",
        node_id,
        "--goal",
        "measure the paid lane queue shape",
        "--write-path",
        "result.json",
        "--time-budget",
        "20m",
        "--session",
        SESSION,
        "--repo",
        str(repo),
        "--done-when",
        DONE_WHEN,
    ]
    if plan_link is not None:
        arguments += ["--plan-link", plan_link]
    if unplanned_reason is not None:
        arguments += ["--unplanned-reason", unplanned_reason]
    if local:
        arguments += ["--local"]
    arguments.append("--dry-run" if dry_run else "--no-watch")
    return arguments


def _invoke(arguments: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return CliRunner().invoke(cli_module.main, arguments)


def _payload(result) -> dict:
    return json.loads(result.output.splitlines()[0])


def _brief_node(brief: Path, *, node_id: str, role: str = "implement") -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="measure the paid lane queue shape",
        plan="",
        brief=str(brief),
        spec_level="exact",
        role=role,
        done_when=DONE_WHEN,
        write_paths=["result.json"],
        time_budget="20m",
    )


# ── the refusal: an executable brief run with no owner ───────────────────────


def test_an_implement_brief_dispatch_with_neutral_arguments_is_refused(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarded thing happens: an implement brief run with neither flag."""
    _config_home, repo, brief = brief_repo

    refused = _invoke(
        _cli_arguments(repo, brief, node_id="brief-implement", dry_run=False),
        monkeypatch,
    )

    assert refused.exit_code == 2, refused.output
    payload = _payload(refused)
    assert payload["ok"] is False
    assert "--plan-link" in payload["detail"], payload
    assert "--unplanned-reason" in payload["detail"], payload
    # Nothing was stored, so the coordinator's next call is the same call.
    assert runs.list_live(project=PROJECT) == []


def test_a_test_role_brief_dispatch_with_neutral_arguments_is_refused(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test run changes a plan's product too, so the same two flags apply."""
    _config_home, repo, brief = brief_repo

    refused = _invoke(
        _cli_arguments(repo, brief, node_id="brief-test", role="test", dry_run=False),
        monkeypatch,
    )

    assert refused.exit_code == 2, refused.output
    payload = _payload(refused)
    assert "--plan-link" in payload["detail"], payload
    assert "--unplanned-reason" in payload["detail"], payload
    assert runs.list_live(project=PROJECT) == []


def test_a_dry_run_with_neutral_arguments_is_refused_the_same_way(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview is the same decision, so it reaches the same refusal."""
    _config_home, repo, brief = brief_repo

    refused = _invoke(
        _cli_arguments(repo, brief, node_id="brief-dry-refused", dry_run=True),
        monkeypatch,
    )

    assert refused.exit_code == 2, refused.output
    payload = _payload(refused)
    assert payload.get("dry_run") is True
    assert "--plan-link" in payload["detail"], payload
    assert "--unplanned-reason" in payload["detail"], payload


def test_a_dry_run_given_a_plan_link_is_not_refused(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative half at the preview: the flag the refusal names clears it."""
    _config_home, repo, brief = brief_repo

    preview = _invoke(
        _cli_arguments(
            repo, brief, node_id="brief-dry-ok", plan_link="plan-a", dry_run=True
        ),
        monkeypatch,
    )

    payload = _payload(preview)
    assert preview.exit_code == 0, preview.output
    assert payload["ok"] is True, payload


def test_a_paired_dispatch_with_neutral_arguments_is_refused(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pair split is below the refusal, so neither arm is ever composed."""
    _config_home, repo, brief = brief_repo

    refused = _invoke(
        [
            *_cli_arguments(repo, brief, node_id="brief-pair", dry_run=False),
            "--pair-backend",
            "clive",
        ],
        monkeypatch,
    )

    assert refused.exit_code == 2, refused.output
    payload = _payload(refused)
    assert "--plan-link" in payload["detail"], payload
    assert "--unplanned-reason" in payload["detail"], payload
    assert runs.list_live(project=PROJECT) == []


def test_a_plan_run_is_not_asked_for_a_link(
    brief_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief beside a plan section is briefed plan work: the plan owns it."""
    _config_home, _repo, _brief = brief_repo

    from reckon.crew.node import brief_plan_owner_finding

    node = crew.TaskNode(
        id="briefed-plan-work",
        goal="measure the paid lane queue shape",
        plan="plan-a",
        section="dispatch",
        brief="brief.md",
        spec_level="exact",
        role="implement",
        done_when=DONE_WHEN,
        write_paths=["result.json"],
        time_budget="20m",
    )

    assert brief_plan_owner_finding(node) is None


def test_a_non_executable_role_is_not_asked_for_a_link(
    brief_repo: tuple[Path, Path, Path],
) -> None:
    """An investigate run moves no plan section, so it owes no owner."""
    _config_home, _repo, brief = brief_repo

    from reckon.crew.node import brief_plan_owner_finding

    node = _brief_node(brief, node_id="probe", role="investigate")

    assert brief_plan_owner_finding(node) is None


# ── the value is recorded where landing reads it ────────────────────────────


def test_a_queued_request_stores_the_link_in_its_dispatch_options(
    brief_repo: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link the dispatch was given survives on the queued request."""
    _config_home, repo, brief = brief_repo
    CONFIG["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=0)
    )

    queued = _invoke(
        _cli_arguments(
            repo,
            brief,
            node_id="brief-queued",
            plan_link="plan-a",
            dry_run=False,
            local=True,
        ),
        monkeypatch,
    )

    payload = _payload(queued)
    assert queued.exit_code == 0, queued.output
    assert payload.get("queued") is True, payload
    pointer = runs.read_pointer(payload["run_id"])
    assert (pointer.get("dispatch_options") or {}).get("plan_link") == "plan-a"


# ── landing needs no flag when the run recorded its owner ───────────────────


def _promote(repo: Path, run_id: str, **kwargs):
    return crew.complete(run_id, root=repo, live_run_waiver=_PLUMBING_WAIVER, **kwargs)


def test_a_launched_run_lands_with_no_landing_flag(
    brief_repo: tuple[Path, Path, Path],
) -> None:
    """A dispatch that launches at once records the link on the pointer."""
    _config_home, repo, brief = brief_repo

    record = crew.dispatch(
        node=_brief_node(brief, node_id="brief-launched"),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=SESSION,
        dispatch_options={"plan_link": "plan-a", "unplanned_reason": ""},
        launcher=lambda *args, **kwargs: os.getpid(),
    )

    pointer = runs.read_pointer(record["run_id"])
    assert pointer.get("plan_link") == "plan-a"

    promoted = _promote(
        repo, record["run_id"], gate="passed", outcome="the plan's product changed"
    )

    assert promoted["run_id"] == record["run_id"]


def test_a_queued_then_launched_run_lands_with_no_landing_flag(
    brief_repo: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch composes a fresh pointer, so it must carry the link itself."""
    _config_home, repo, brief = brief_repo
    lane = _lane_document(tmp_path, worker_slots=0)
    CONFIG["backends"]["clive"]["lane_document"] = str(lane)

    queued = _invoke(
        _cli_arguments(
            repo,
            brief,
            node_id="brief-queued-launched",
            plan_link="plan-a",
            dry_run=False,
            local=True,
        ),
        monkeypatch,
    )
    payload = _payload(queued)
    assert queued.exit_code == 0, queued.output
    assert payload.get("queued") is True, payload
    run_id = payload["run_id"]

    lane.write_text(
        _lane_document(tmp_path, worker_slots=4).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    launched: list = []

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched.append(plan)
        return os.getpid()

    admitted = queue_admission.admit_once(
        config_for=lambda pointer: CONFIG, launcher=launcher
    )

    assert admitted["launched"] == [run_id], admitted
    pointer = runs.read_pointer(run_id)
    assert pointer["phase"] != "queued"
    assert pointer.get("plan_link") == "plan-a"

    promoted = _promote(
        repo, run_id, gate="passed", outcome="the plan's product changed"
    )

    assert promoted["run_id"] == run_id


# ── a pointer that recorded nothing is refused at landing, as before ────────


def test_a_pointer_with_no_recorded_link_and_no_flag_is_refused_at_landing(
    brief_repo: tuple[Path, Path, Path],
) -> None:
    """The landing guard still fires for a run dispatched before this existed."""
    _config_home, repo, brief = brief_repo

    record = crew.dispatch(
        node=_brief_node(brief, node_id="brief-unrecorded"),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=SESSION,
        launcher=lambda *args, **kwargs: os.getpid(),
    )

    assert runs.read_pointer(record["run_id"]).get("plan_link") in (None, "")

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repo, record["run_id"], gate="passed", outcome="code landed")

    message = str(refusal.value)
    assert "--plan-link" in message
    assert "--unplanned-reason" in message


def test_the_dispatch_and_landing_refusals_agree_on_every_input() -> None:
    """One fact, two sentences: the refusals must split the inputs identically.

    The dispatch refusal and the landing refusal ask the same question of the
    same run, at two moments. If each restated the predicate they could drift,
    and the drift would show up only as a run dispatched without an owner that
    lands without one too — the shape the earlier repair removed. So every
    combination of the four inputs is put to both, and the two must agree on
    which side of the split it falls.
    """
    from reckon.crew.node import brief_plan_owner_finding
    from reckon.crew.promotion_checks import _require_brief_owner

    disagreements: list[tuple[str, str, str, str, str]] = []
    for brief in ("", BRIEF_TEXT):
        for plan in ("", "some-plan:1.2"):
            for role in ("implement", "test", "review"):
                for link in ("", "some-plan"):
                    for reason in ("", "changes no plan"):
                        node = crew.TaskNode(
                            id="agreement",
                            goal="measure the paid lane queue shape",
                            plan=plan,
                            brief=brief,
                            spec_level="exact",
                            role=role,
                            done_when=DONE_WHEN,
                            write_paths=["result.json"],
                            time_budget="20m",
                            manifest_path="manifests/agreement.md",
                        )
                        refused_at_dispatch = (
                            brief_plan_owner_finding(
                                node,
                                plan_link=link,
                                unplanned_reason=reason,
                            )
                            is not None
                        )
                        record = {
                            "node": {"brief": brief, "plan": plan},
                            "role": role,
                        }
                        try:
                            _require_brief_owner(
                                "agreement",
                                record,
                                plan_link=link,
                                unplanned_reason=reason,
                            )
                        except crew.CrewError:
                            refused_at_landing = True
                        else:
                            refused_at_landing = False
                        if refused_at_dispatch != refused_at_landing:
                            disagreements.append((brief, plan, role, link, reason))

    assert disagreements == []


def test_a_recorded_discharge_is_read_at_the_same_inputs_as_a_given_one() -> None:
    """Landing's fallback to the recorded value sits inside the same split.

    The dispatch helps a run land without being asked twice, so the value it
    records must discharge the owner at landing exactly where a landing flag
    would. This keeps both readings on the one rule: recorded and given are the
    same fact by the time the split is asked.

    The accepted discharge is also what landing hands the ledger row, so the
    return value is asserted rather than ignored — a refusal that stops refusing
    while returning nothing records a brief landing with no plan owner at all.
    """
    from reckon.crew.promotion_checks import _require_brief_owner

    record = {
        "node": {"brief": BRIEF_TEXT, "plan": ""},
        "role": "implement",
        "plan_link": "some-plan",
    }

    assert _require_brief_owner(
        "recorded",
        record,
        plan_link="",
        unplanned_reason="",
    ) == {"plan_link": "some-plan", "unplanned_reason": ""}

    # A landing flag is an operator correcting the link, so it wins.
    assert _require_brief_owner(
        "recorded",
        record,
        plan_link="landing-plan",
        unplanned_reason="",
    ) == {"plan_link": "landing-plan", "unplanned_reason": ""}

    # A run the rule never asked for records nothing, so a stray flag given to
    # it cannot reach the row as an owner it never had.
    assert (
        _require_brief_owner(
            "plan-run",
            {
                "node": {"brief": BRIEF_TEXT, "plan": "some-plan:1.2"},
                "role": "implement",
            },
            plan_link="",
            unplanned_reason="",
        )
        is None
    )

    with pytest.raises(crew.CrewError):
        _require_brief_owner(
            "recorded",
            {"node": {"brief": BRIEF_TEXT, "plan": ""}, "role": "implement"},
            plan_link="",
            unplanned_reason="",
        )
