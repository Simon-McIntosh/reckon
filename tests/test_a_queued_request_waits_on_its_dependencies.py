"""A queued request waits on its dependencies and launches when they land.

A coordinator queues a plan's whole width in one call. Each request whose
section declares upstream refs is stored ``awaiting-upstream``; every admission
pass re-reads readiness through the roadmap's own derivation and moves the
request to ``queued`` as its last ref lands, with no coordinator turn between a
promotion and the downstream launch. A ref closed as abandoned or failed moves
the request to ``blocked-upstream``, which no pass launches, and blocking is
transitive so the tail of a chain is blocked with the middle.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from reckon.crew import (
    queue_admission,
    recovery_classification,
    recovery_vocabulary,
    runs,
)
from reckon.crew.node import CrewError, TaskNode
from tests import test_a_jev_hold_queues_for_the_local_lane as hold_tests
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

# The chain every test queues: three sections, each waiting on the one before.
CHAIN = {"s2": [f"{PLAN}#s1"], "s3": [f"{PLAN}#s2"]}


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _plan_html(declarations: dict[str, str]) -> str:
    quote = '"'
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        f"<meta name={quote}plan-section-depends-on{quote} content={quote}"
        f"{json.dumps(CHAIN).replace(quote, '&quot;')}{quote}>\n"
        f"<h2 id={quote}s1{quote}>§1</h2>\n<h2 id={quote}s2{quote}>§2</h2>\n"
        f"<h2 id={quote}s3{quote}>§3</h2>\n"
    )


def _write_plan(repository: Path, declarations: dict[str, str]) -> None:
    (repository / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html(declarations), encoding="utf-8"
    )


def _declare(repository: Path, section: str) -> None:
    """Mark one section done in the plan, as its landing does."""
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    match = re.search(r'name="plan-section-declarations" content="([^"]*)"', text)
    assert match is not None, "the fixture always writes a declarations meta"
    raw = match.group(1)
    declarations = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&"))
    declarations[section] = "done"
    path.write_text(
        text.replace(raw, json.dumps(declarations).replace('"', "&quot;"), 1),
        encoding="utf-8",
    )
    # A landing commits the plan it ticks: a dispatch refuses to run against a
    # plan that differs from the base it reads.
    _run(repository, "add", "--", str(path.relative_to(repository)))
    _run(repository, "commit", "-q", "-m", f"chore: land {section}")


@pytest.fixture()
def chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (repository / "docs" / "plans").mkdir(parents=True)
    _write_plan(
        repository,
        {"s1": "implementable", "s2": "implementable", "s3": "implementable"},
    )
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "add", "--", "skills")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _config_for():
    config = json.loads(json.dumps(CONFIG))

    def resolve(pointer):
        return config

    return resolve


def _node(node_id: str, section: str, **extra) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal=f"build {section}",
        plan=PLAN,
        section=section,
        done_when=f"pytest reports passed for {section}",
        write_paths=[f"{section}.txt"],
        time_budget="20m",
        spec_level="exact",
        **extra,
    )


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(Path(str(prompt_path)).name)
        return os.getpid()

    return launch


def _live(repository: Path) -> dict[str, dict]:
    return {
        str(pointer["run_id"]): pointer for pointer in runs.list_live(project=PROJECT)
    }


def _phase_of(repository: Path, section: str) -> str:
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        if str(node.get("section")) == section:
            return str(pointer.get("phase") or "")
    return ""


def _queue_chain(repository: Path) -> dict:
    return queue_admission.queue_requests(
        [_node("n1", "s1"), _node("n2", "s2"), _node("n3", "s3")],
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        run_ids={"n1": "r-n1", "n2": "r-n2", "n3": "r-n3"},
    )


def test_the_chain_queues_awaiting_upstream_and_launches_in_order(
    chain_repo: Path,
) -> None:
    stored = _queue_chain(chain_repo)
    assert stored["r-n1"]["phase"] == queue_admission.QUEUED_PHASE
    assert stored["r-n2"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert stored["r-n2"]["waits"] == [f"{PLAN}#s1"]
    assert stored["r-n3"]["waits"] == [f"{PLAN}#s2"]

    launched: list[str] = []
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))
    assert len(launched) == 1, "only the head of the chain has no unmet wait"
    assert _phase_of(chain_repo, "s2") == "awaiting-upstream"
    assert _phase_of(chain_repo, "s3") == "awaiting-upstream"

    _declare(chain_repo, "s1")
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))
    assert len(launched) == 2, "the middle section launches as its upstream lands"
    assert _phase_of(chain_repo, "s3") == "awaiting-upstream"

    _declare(chain_repo, "s2")
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))
    assert len(launched) == 3, "the tail launches as its upstream lands"


def test_an_abandoned_upstream_blocks_the_tail_and_never_launches(
    chain_repo: Path,
) -> None:
    stored = _queue_chain(chain_repo)
    assert stored["r-n2"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE

    # The head's run is closed as failed: nothing will ever deliver s1, so the
    # middle section is blocked rather than waiting, and the tail behind it is
    # blocked with it.
    def fail_head(pointer: dict) -> dict:
        pointer["phase"] = "failed"
        return pointer

    runs._mutate_pointer("r-n1", fail_head)

    launched: list[str] = []
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))

    assert launched == [], "a blocked chain launches nothing"
    assert _phase_of(chain_repo, "s2") == queue_admission.BLOCKED_UPSTREAM_PHASE
    assert _phase_of(chain_repo, "s3") == queue_admission.BLOCKED_UPSTREAM_PHASE


def _declare_wait_refs(repository: Path, *, section: str, refs: list[str]) -> None:
    """Declare on the plan that one section waits on exactly ``refs``."""
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    match = re.search(r'name="plan-section-depends-on" content="([^"]*)"', text)
    assert match is not None, "the fixture always writes a waits meta in the plan"
    declaration = json.dumps({section: refs}).replace('"', "&quot;")
    path.write_text(text.replace(match.group(1), declaration, 1), encoding="utf-8")
    _run(repository, "add", "--", str(path.relative_to(repository)))
    _run(repository, "commit", "-q", "-m", "chore: declare the waits")


def _gate_section(
    repository: Path, *, gate_id: str, section: str, verdict: str
) -> None:
    """Give the plan a gate on one section, as an authored plan carries one.

    The gate names the section its outcome comes from, so the roadmap resolves
    it into a blocker row on the gated section carrying the gate's id, which is
    the row a wait on the gate reads.
    """
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    block = (
        '<section data-reckon="gates">'
        f'<div class="r-gate" data-id="{gate_id}" data-section="{section}"'
        f' data-status="open" data-verdict="{verdict}"'
        f' data-gating-plan="{PLAN}#s1">'
        '<h4 class="r-gate-measure">the section lands green</h4></div></section>'
    )
    if 'data-reckon="gates"' in text:
        text = re.sub(
            r'<section data-reckon="gates">.*?</section>', "", text, flags=re.DOTALL
        )
    path.write_text(text + block, encoding="utf-8")
    _run(repository, "add", "--", str(path.relative_to(repository)))
    _run(repository, "commit", "-q", "-m", "chore: author the gate")


def _unmet(chain_repo: Path, *, section: str) -> list[str]:
    return queue_admission.unmet_waits(
        _node("n-wait", section), repo_root=chain_repo, project=PROJECT
    )


def test_a_section_ref_holds_until_that_section_lands(chain_repo: Path) -> None:
    _declare_wait_refs(chain_repo, section="s2", refs=[f"{PLAN}#s1"])
    assert _unmet(chain_repo, section="s2") == [f"{PLAN}#s1"]

    _declare(chain_repo, "s1")
    assert _unmet(chain_repo, section="s2") == [], "a landed section releases the wait"


def test_a_gate_ref_holds_and_releases_as_the_gate_verdict_moves(
    chain_repo: Path,
) -> None:
    """A gate wait reads the roadmap's own gate edge, not a copy of it.

    The gate names the section its outcome comes from, and the roadmap reports
    that edge as a blocker on the gated section while the source has not
    landed, so the wait is not satisfied. Once the source lands the roadmap's
    gate edge reads satisfied and holds no blocker, so the wait releases.
    """
    _gate_section(chain_repo, gate_id="g1", section="s2", verdict="")
    _declare_wait_refs(chain_repo, section="s2", refs=[f"gate:{PLAN}#g1"])
    assert _unmet(chain_repo, section="s2") == [f"gate:{PLAN}#g1"]

    _declare(chain_repo, "s1")
    assert _unmet(chain_repo, section="s2") == [], (
        "the gate's source lands and releases it"
    )


def test_a_wait_on_another_request_reads_that_requests_own_record(
    chain_repo: Path,
) -> None:
    """A request wait needs an existing run and a landed revision."""
    from reckon import ledger

    _declare_wait_refs(chain_repo, section="s2", refs=["run:r-n2"])
    with pytest.raises(CrewError, match="missing request r-n2"):
        queue_admission.queue_requests(
            [_node("n-wait", "s2")],
            project=PROJECT,
            session=SESSION,
            repo_root=chain_repo,
            config=CONFIG,
            run_ids={"n-wait": "r-waiter"},
        )
    assert runs.list_live(project=PROJECT) == []

    queued, _position = runs.queue_dispatch(
        {
            "run_id": "r-n2",
            "project": PROJECT,
            "phase": queue_admission.QUEUED_PHASE,
            "session": SESSION,
            "queued_at": queue_admission._utc_now(),
            "node": {"id": "n2", "plan": PLAN, "section": "s2"},
        }
    )
    assert queued["run_id"] == "r-n2"
    stored = queue_admission.queue_requests(
        [_node("n-wait", "s2")],
        project=PROJECT,
        session=SESSION,
        repo_root=chain_repo,
        config=CONFIG,
        run_ids={"n-wait": "r-waiter"},
    )
    assert stored["r-waiter"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE

    manifest = runs.run_dir("r-n2") / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("status: complete\n", encoding="utf-8")
    runs.pointer_path("r-n2").unlink()
    queue_admission.advance_waiting_requests(runs.list_live())
    assert runs.read_pointer("r-waiter")["phase"] == (
        queue_admission.AWAITING_UPSTREAM_PHASE
    )

    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=chain_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    promoted = subprocess.run(
        ["git", "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "upstream result"],
        cwd=chain_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    row = ledger.run_path(PROJECT, "r-n2")
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps({"run_id": "r-n2", "promoted_revision": promoted}),
        encoding="utf-8",
    )
    queue_admission.advance_waiting_requests(runs.list_live())
    assert runs.read_pointer("r-waiter")["phase"] == (
        queue_admission.AWAITING_UPSTREAM_PHASE
    )

    _run(chain_repo, "merge", "--ff-only", promoted)
    queue_admission.advance_waiting_requests(runs.list_live())
    assert runs.read_pointer("r-waiter")["phase"] == queue_admission.QUEUED_PHASE


def test_a_json_manifest_upstream_releases_the_wait(chain_repo: Path) -> None:
    """A JSON manifest still needs a landed revision to release its waiter."""
    from reckon import ledger

    _declare_wait_refs(chain_repo, section="s2", refs=["run:r-n3"])
    with pytest.raises(CrewError, match="missing request r-n3"):
        queue_admission.queue_requests(
            [_node("n-wait", "s2")],
            project=PROJECT,
            session=SESSION,
            repo_root=chain_repo,
            config=CONFIG,
            run_ids={"n-wait": "r-waiter"},
        )
    assert runs.list_live(project=PROJECT) == []

    runs.queue_dispatch(
        {
            "run_id": "r-n3",
            "project": PROJECT,
            "phase": queue_admission.QUEUED_PHASE,
            "session": SESSION,
            "queued_at": queue_admission._utc_now(),
            "node": {"id": "n3", "plan": PLAN, "section": "s3"},
        }
    )
    stored = queue_admission.queue_requests(
        [_node("n-wait", "s2")],
        project=PROJECT,
        session=SESSION,
        repo_root=chain_repo,
        config=CONFIG,
        run_ids={"n-wait": "r-waiter"},
    )
    assert stored["r-waiter"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE

    manifest = runs.run_dir("r-n3") / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"status": "complete", "node": "n3"}', encoding="utf-8")
    runs.pointer_path("r-n3").unlink()
    queue_admission.advance_waiting_requests(runs.list_live())
    assert runs.read_pointer("r-waiter")["phase"] == (
        queue_admission.AWAITING_UPSTREAM_PHASE
    )

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=chain_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    row = ledger.run_path(PROJECT, "r-n3")
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps({"run_id": "r-n3", "promoted_revision": revision}),
        encoding="utf-8",
    )
    queue_admission.advance_waiting_requests(runs.list_live())
    assert runs.read_pointer("r-waiter")["phase"] == queue_admission.QUEUED_PHASE


def test_a_malformed_node_is_refused_at_queue_time(chain_repo: Path) -> None:
    malformed = _node("n2", "s2", inputs=["a-file-the-repository-does-not-hold.csv"])
    with pytest.raises(CrewError) as refusal:
        queue_admission.queue_requests(
            [_node("n1", "s1"), malformed],
            project=PROJECT,
            session=SESSION,
            repo_root=chain_repo,
            config=CONFIG,
        )
    assert "refused at queue time" in str(refusal.value)
    sections = {
        str(dict(pointer.get("node") or {}).get("section"))
        for pointer in runs.list_live(project=PROJECT)
    }
    assert "s2" not in sections, "the malformed node wrote no request"


# ── the production entry point ───────────────────────────────────────────────
#
# The arms above queue through ``queue_requests`` directly, which would pass
# even if no dispatch ever wrote a hold. These arms drive the command a
# coordinator actually runs and read the pointer it leaves, so the hold is
# proved reachable rather than merely callable.


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _declare_section_wait(
    repository: Path, *, section: str, refs: list[str], sections: tuple[str, ...] = ()
) -> None:
    """Declare on the fixture's plan that one section waits on ``refs``."""
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    declaration = json.dumps({section: refs}).replace('"', "&quot;")
    anchors = "".join(f'<h2 id="{name}">§{name}</h2>' for name in sections)
    text = text.replace(
        "</head>",
        f'<meta name="plan-section-depends-on" content="{declaration}"></head>',
        1,
    )
    if anchors:
        text = text.replace("</body>", f"{anchors}</body>", 1)
    path.write_text(text, encoding="utf-8")
    _run(repository, "add", "--", str(path.relative_to(repository)))
    _run(repository, "commit", "-q", "-m", "chore: declare the section wait")


def test_a_dispatch_holds_the_request_it_queues_for_upstream(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare_section_wait(
        dispatch_repo,
        section="dispatch",
        refs=[f"{PLAN}#upstream"],
        sections=("upstream",),
    )
    hold_tests._configure(dispatch_repo, monkeypatch, session_slots=0)
    hold_tests._stub_hold(monkeypatch, offered_local=True)

    result = hold_tests._invoke(dispatch_repo, node="waiting-local")
    payload = hold_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    pointers = runs.list_live(project=hold_tests.PROJECT)
    assert len(pointers) == 1
    assert pointers[0]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert pointers[0]["waits"] == [f"{PLAN}#upstream"]
    assert payload["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE


def test_a_dispatch_holds_on_waits_though_its_lane_has_room(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waits decide the hold, not the lane: room must not walk past them."""
    _declare_section_wait(
        dispatch_repo,
        section="dispatch",
        refs=[f"{PLAN}#upstream"],
        sections=("upstream",),
    )
    hold_tests._configure(dispatch_repo, monkeypatch, session_slots=4)
    hold_tests._stub_hold(monkeypatch, offered_local=True)

    result = hold_tests._invoke(dispatch_repo, node="waiting-free-lane")
    payload = hold_tests._payload(result)

    assert result.exit_code == 0, result.output
    pointers = runs.list_live(project=hold_tests.PROJECT)
    assert len(pointers) == 1
    assert pointers[0]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert pointers[0]["waits"] == [f"{PLAN}#upstream"]
    assert payload["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    # Stored and not launched: no worker was started for this node.
    assert not payload.get("pid")
    assert not pointers[0].get("pid")


@pytest.mark.parametrize("upstream", ["missing", "dispatch"])
def test_dispatch_refuses_invalid_wait_before_storing(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, upstream: str
) -> None:
    _declare_section_wait(
        dispatch_repo, section="dispatch", refs=[f"{PLAN}#{upstream}"]
    )
    hold_tests._configure(dispatch_repo, monkeypatch, session_slots=0)
    hold_tests._stub_hold(monkeypatch, offered_local=True)

    result = hold_tests._invoke(dispatch_repo, node="invalid-wait")

    assert result.exit_code != 0
    assert not runs.list_live(project=hold_tests.PROJECT)


def test_dispatch_holds_its_own_gated_section(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = dispatch_repo / "docs" / "plans" / f"{PLAN}.html"
    text = path.read_text(encoding="utf-8")
    gate = (
        '<section data-reckon="gates"><div class="r-gate" data-id="approval" '
        'data-status="open" data-verdict="" data-gated-sections="dispatch">'
        '<h4 class="r-gate-measure">approve</h4></div></section>'
    )
    text = text.replace(
        "</head>",
        '<meta name="plan-section-declarations" '
        'content="{&quot;dispatch&quot;:&quot;implementable&quot;}"></head>',
    )
    path.write_text(text.replace("</body>", f"{gate}</body>"), encoding="utf-8")
    _run(dispatch_repo, "add", "--", str(path.relative_to(dispatch_repo)))
    _run(dispatch_repo, "commit", "-q", "-m", "chore: gate section")
    hold_tests._configure(dispatch_repo, monkeypatch, session_slots=0)
    hold_tests._stub_hold(monkeypatch, offered_local=True)

    result = hold_tests._invoke(dispatch_repo, node="gated-work")

    assert result.exit_code == 0, result.output
    pointers = runs.list_live(project=hold_tests.PROJECT)
    assert len(pointers) == 1
    assert pointers[0]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE


def test_a_dispatch_of_a_section_without_waits_launches_on_a_free_lane(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discriminator for the arm above: the same lane, with no waits."""
    hold_tests._configure(dispatch_repo, monkeypatch, session_slots=4)
    hold_tests._stub_hold(monkeypatch, offered_local=True)

    result = hold_tests._invoke(dispatch_repo, node="plain-free-lane")
    payload = hold_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    assert payload.get("phase") != queue_admission.AWAITING_UPSTREAM_PHASE
    pointers = runs.list_live(project=hold_tests.PROJECT)
    assert pointers and not any(
        pointer.get("phase") in queue_admission.WAITING_UPSTREAM_PHASES
        for pointer in pointers
    )


def test_a_dispatch_of_a_section_without_waits_queues_it_plainly(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold_tests._configure(dispatch_repo, monkeypatch, session_slots=0)
    hold_tests._stub_hold(monkeypatch, offered_local=True)

    result = hold_tests._invoke(dispatch_repo, node="plain-local")
    assert result.exit_code == 0, result.output
    pointers = runs.list_live(project=hold_tests.PROJECT)
    assert len(pointers) == 1
    assert pointers[0]["phase"] == queue_admission.QUEUED_PHASE
    assert not pointers[0].get("waits")


def test_a_working_run_redispatch_keeps_its_phase_and_its_pool(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run already launched is answered, never re-read as a waiting request.

    The waits decide a request that has not launched; a pointer already
    ``starting`` is the record of a launch that happened. Reading its waits and
    writing the hold over it would take that run off its lane and leave it
    stored as a request nothing is running, so the re-dispatch answers the run
    that exists and its pointer is left exactly as it stands.
    """
    _declare_section_wait(
        dispatch_repo,
        section="dispatch",
        refs=[f"{PLAN}#upstream"],
        sections=("upstream",),
    )
    hold_tests._configure(dispatch_repo, monkeypatch, session_slots=4)
    hold_tests._stub_hold(monkeypatch, offered_local=True)

    first = hold_tests._invoke(dispatch_repo, node="working-redispatch")
    assert first.exit_code == 0, first.output
    pointers = runs.list_live(project=hold_tests.PROJECT)
    assert len(pointers) == 1
    assert pointers[0]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE

    # The request the first dispatch stored is then launched by hand, which is
    # the state the re-dispatch has to leave alone.
    run_id = str(pointers[0]["run_id"])

    def launch(pointer: dict) -> dict:
        launched = {
            key: value
            for key, value in pointer.items()
            if key not in ("waits", "waits_at")
        }
        return {**launched, "phase": "starting", "backend": hold_tests.LOCAL}

    runs._mutate_pointer(run_id, launch)

    second = hold_tests._invoke(dispatch_repo, node="working-redispatch")
    assert second.exit_code == 0, second.output

    live = runs.list_live(project=hold_tests.PROJECT)
    assert len(live) == 1, live
    assert live[0]["phase"] == "starting"
    assert live[0]["backend"] == hold_tests.LOCAL
    assert str(live[0]["run_id"]) == run_id
    # The hold writes its refs and the time it wrote them in one write, so a
    # pointer carrying no ``waits_at`` is one the hold never touched, whatever
    # the refs it carried before.
    assert not live[0].get("waits_at")
    assert not live[0].get("waits")
    assert live[0]["phase"] not in queue_admission.WAITING_UPSTREAM_PHASES


# ── the watch line knows the two phases ──────────────────────────────────────
#
# A request that is stored and not launched has to be drawable, or a held
# request is real and visible nowhere. The classification is read from the
# pointer's own phase and the remedy comes from the phase-to-verb map, so both
# have to carry the two names the admission pass writes.


@pytest.mark.parametrize(
    ("phase", "verb", "remedy"),
    [
        (queue_admission.AWAITING_UPSTREAM_PHASE, "wait", "to land"),
        (queue_admission.BLOCKED_UPSTREAM_PHASE, "decide", "recover or drop"),
    ],
)
def test_the_watch_line_reads_both_waiting_phases(
    phase: str, verb: str, remedy: str
) -> None:
    record = {
        "run_id": "r-watch",
        "project": PROJECT,
        "phase": phase,
        "waits": [f"{PLAN}#upstream"],
        "session": SESSION,
    }
    reading = recovery_classification.classify_pointer(record, now_seconds=0.0)

    # The classifier is read against what it must publish for this phase -- the
    # verb its own vocabulary maps the word to, and the remedy that verb
    # produces -- rather than against the phase this test wrote into the record,
    # which would only echo the input back. A classifier that echoed any word
    # would still have to map it to this arm's verb, and the two arms' verbs
    # differ, so an echo of the wrong phase fails here.
    classification = str(reading["classification"])
    assert recovery_vocabulary.RECOVERY_VERBS.get(classification) == verb
    assert remedy in str(reading.get("next_action") or "")
    assert recovery_vocabulary.RECOVERY_VERBS[phase] == verb


def test_the_vocabulary_names_the_phases_the_admission_module_declares() -> None:
    """The phase words the readers draw are the ones the owner declares.

    The phase-to-verb map and the duty set are module-level data other modules
    import by name, so they cannot be filled from the admission module at
    import: that module imports these readers, and closing the loop fails. The
    words are therefore spelled in the map, and pinned here to the owner's own
    declarations so the two cannot drift apart unobserved.
    """
    verbs = recovery_vocabulary.RECOVERY_VERBS
    assert verbs.get(queue_admission.AWAITING_UPSTREAM_PHASE) == "wait"
    assert verbs.get(queue_admission.BLOCKED_UPSTREAM_PHASE) == "decide"
    actionable = recovery_vocabulary.ACTIONABLE_RECOVERY_CLASSIFICATIONS
    assert queue_admission.AWAITING_UPSTREAM_PHASE not in actionable
    assert queue_admission.BLOCKED_UPSTREAM_PHASE in actionable


def test_only_the_unreleasable_waiting_phase_is_a_duty() -> None:
    """A held request is a duty only once nothing will ever release it."""
    actionable = recovery_vocabulary.ACTIONABLE_RECOVERY_CLASSIFICATIONS
    assert queue_admission.AWAITING_UPSTREAM_PHASE not in actionable
    assert queue_admission.BLOCKED_UPSTREAM_PHASE in actionable
