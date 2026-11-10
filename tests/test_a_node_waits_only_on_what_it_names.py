"""A node waits on exactly what its own dispatch and its own section declare.

A section's waits describe the section's own deliverable, not every node a
coordinator files under it. A dispatch naming its own ``--after`` refs and
setting ``--waits-only-after`` waits on those alone: the section's declared
refs, the plan's ``depends_on`` and the section's gates are none of them, and
no admission pass adds them back. A request that names no ``--after`` never
takes a sibling's, because the pass's re-derivation cache is keyed by the
request's own refs rather than by its section alone.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, ledger
from reckon.crew import dispatch_batch, queue_admission, runs
from reckon.crew.node import TaskNode, validate_node
from tests import test_a_queued_request_waits_on_its_dependencies as waits_tests

CONFIG = waits_tests.CONFIG
PLAN = waits_tests.PLAN
PROJECT = waits_tests.PROJECT
SESSION = waits_tests.SESSION
_declare = waits_tests._declare
_declare_wait_refs = waits_tests._declare_wait_refs
_gate_section = waits_tests._gate_section
_node = waits_tests._node
_run = waits_tests._run


@pytest.fixture()
def chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The mounted single-plan checkout the queue tests already build."""
    return waits_tests.chain_repo.__wrapped__(tmp_path, monkeypatch)


PLAN_B = "plan-b"
UPSTREAM = "r-upstream"

# Three refs a section can inherit, one from each source the plan names: two
# sections of its own plan, and the plan the plan itself waits on.
SECTION_REFS = [f"{PLAN}#s1", f"{PLAN}#s3"]

INHERITED = [*SECTION_REFS, PLAN_B]


def _declare_section_waits(repository: Path) -> None:
    """Give the fixture plan a section that waits on two unlanded sections."""
    _declare_wait_refs(repository, section="s2", refs=SECTION_REFS)


def _declare_plan_dependency(repository: Path) -> None:
    """Add a second plan and make the fixture plan wait on it."""
    sibling = repository / "docs" / "plans" / f"{PLAN_B}.html"
    sibling.write_text(
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{PLAN_B}">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-section-declarations" content="{}">\n'
        '<h2 id="s1">§1</h2>\n',
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / f"{PLAN}.html"
    text = plan.read_text(encoding="utf-8")
    plan.write_text(
        f'<meta name="plan-depends-on" content="{PLAN_B}">\n{text}',
        encoding="utf-8",
    )
    _run(
        repository,
        "add",
        "--",
        *[
            str(path.relative_to(repository))
            for path in (
                sibling,
                plan,
            )
        ],
    )
    _run(repository, "commit", "-q", "-m", "docs: declare the plan dependency")


def _queue_upstream(repository: Path) -> None:
    runs.queue_dispatch(
        {
            "run_id": UPSTREAM,
            "project": PROJECT,
            "phase": queue_admission.QUEUED_PHASE,
            "session": SESSION,
            "queued_at": queue_admission._utc_now(),
            "node": {
                "id": "n-upstream",
                "goal": "settle the upstream request",
                "plan": PLAN,
                "section": "s1",
            },
        }
    )


def _land(repository: Path, run_id: str) -> None:
    """Settle a queued request at the current revision, as a landing does."""
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("status: complete\n", encoding="utf-8")
    runs.pointer_path(run_id).unlink()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    row = ledger.run_path(PROJECT, run_id)
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps({"run_id": run_id, "promoted_revision": revision}),
        encoding="utf-8",
    )


def _node_at(repository: Path, node_id: str, section: str, **extra) -> TaskNode:
    """A queueable node: the queue contract wants the manifest it delivers to."""
    return _node(
        node_id,
        section,
        manifest_path=str(repository / "notes" / f"{node_id}-manifest.md"),
        **extra,
    )


def _queue(repository: Path, nodes: list[TaskNode], ids: dict[str, str]) -> dict:
    return queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        run_ids=ids,
    )


def _waits(run_id: str) -> list[str]:
    return queue_admission.section_waits_for(runs.read_pointer(run_id))


def test_the_flag_waits_on_its_own_ref_alone_and_no_pass_adds_the_sections_back(
    chain_repo: Path,
) -> None:
    """Queued as its own ref lands, never re-widened by an admission pass."""
    _declare_section_waits(chain_repo)
    _declare_plan_dependency(chain_repo)
    _queue_upstream(chain_repo)

    stored = _queue(
        chain_repo,
        [
            _node_at(
                chain_repo,
                "n-flag",
                "s2",
                after=[UPSTREAM],
                waits_only_after=True,
            )
        ],
        {"n-flag": "r-flag"},
    )
    assert stored["r-flag"]["waits"] == [f"run:{UPSTREAM}"]
    assert stored["r-flag"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    pointer = runs.read_pointer("r-flag")
    assert pointer["node"]["section"] == "s2", (
        "the request still records its section for routing and landing"
    )
    assert pointer["node"]["waits_only_after"] is True, "the flag is stored"

    # A pass in between re-derives every waiting request. The section's waits
    # and the plan's dependency are what the flag drops, so neither returns.
    queue_admission.advance_waiting_requests(runs.list_live())
    assert _waits("r-flag") == [f"run:{UPSTREAM}"]
    assert runs.read_pointer("r-flag")["phase"] == (
        queue_admission.AWAITING_UPSTREAM_PHASE
    )

    _land(chain_repo, UPSTREAM)
    queue_admission.advance_waiting_requests(runs.list_live())
    assert runs.read_pointer("r-flag")["phase"] == queue_admission.QUEUED_PHASE


def test_without_the_flag_the_same_dispatch_waits_on_all_four(
    chain_repo: Path,
) -> None:
    """The discriminator: the flag is what removes the inherited three."""
    _declare_section_waits(chain_repo)
    _declare_plan_dependency(chain_repo)
    _queue_upstream(chain_repo)

    stored = _queue(
        chain_repo,
        [_node_at(chain_repo, "n-plain", "s2", after=[UPSTREAM])],
        {"n-plain": "r-plain"},
    )
    assert stored["r-plain"]["waits"] == [
        *INHERITED,
        f"run:{UPSTREAM}",
    ]
    assert stored["r-plain"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE


def test_a_request_naming_no_after_never_takes_a_siblings(
    chain_repo: Path,
) -> None:
    """One pass re-derives two requests of one section, sibling second."""
    _declare_section_waits(chain_repo)
    _declare_plan_dependency(chain_repo)
    _queue_upstream(chain_repo)

    _queue(
        chain_repo,
        [
            _node_at(chain_repo, "n-sibling", "s2", after=[UPSTREAM]),
            _node_at(chain_repo, "n-plain", "s2"),
        ],
        {"n-sibling": "r-sibling", "n-plain": "r-plain"},
    )

    # Make the sibling's stored waits stale, so the pass must re-derive it and
    # the cache it fills is the one the second request would read.
    def stale(pointer: dict) -> dict:
        pointer["waits"] = [f"{PLAN}#s1"]
        return pointer

    runs._mutate_pointer("r-sibling", stale)

    pointer = runs.read_pointer("r-sibling")
    sibling_pointer = runs.read_pointer("r-plain")
    queue_admission.advance_waiting_requests([pointer, sibling_pointer])

    assert _waits("r-sibling") == [*INHERITED, f"run:{UPSTREAM}"], (
        "the pass re-derived the sibling, so the cache it fills was read"
    )
    assert _waits("r-plain") == INHERITED, (
        "a request naming no after never waits on its sibling's"
    )


def test_the_flag_suppresses_the_sections_gate_ref(chain_repo: Path) -> None:
    """A gate on the section arrives through the same readiness append."""
    _declare_wait_refs(chain_repo, section="s2", refs=[f"{PLAN}#s1"])
    _gate_section(chain_repo, gate_id="g1", section="s2", verdict="")
    _queue_upstream(chain_repo)
    readiness = f"readiness:{PROJECT}:{PLAN}#s2"

    flagged = _node("n-flag", "s2", after=[UPSTREAM], waits_only_after=True)
    flagged_refs, _projects, _unmet = queue_admission.dispatch_waits(
        flagged, repo_root=chain_repo, project=PROJECT
    )
    assert flagged_refs == [f"run:{UPSTREAM}"], "the gate ref is not inherited"

    plain = _node_at(chain_repo, "n-plain", "s2", after=[UPSTREAM])
    plain_refs, _projects, _unmet = queue_admission.dispatch_waits(
        plain, repo_root=chain_repo, project=PROJECT
    )
    assert readiness in plain_refs, "the same section, unflagged, keeps its gate"
    assert f"run:{UPSTREAM}" in plain_refs


def test_a_batch_entry_keeps_the_flag_and_waits_on_its_own_ref(
    chain_repo: Path,
) -> None:
    """The batch path stores the flag and waits on the entry's ref alone."""
    _declare_section_waits(chain_repo)
    _declare_plan_dependency(chain_repo)
    _queue_upstream(chain_repo)
    entry = {
        "project": PROJECT,
        "plan": PLAN,
        "section": "s2",
        "node": "n-batch",
        "goal": "read the pinned configuration",
        "after": [UPSTREAM],
        "waits_only_after": True,
        "done_when": (
            "the command `cat notes/read.log` prints the 2 configuration keys it "
            "read and exits 0"
        ),
        "spec_level": "guided",
        "write_paths": [f"docs/plans/{PLAN}.html"],
        "time_budget": "20m",
    }

    # The flag has to survive the entry-to-node conversion, which reads every
    # declared field: a field the batch does not know is refused by name, so a
    # converter that dropped it would store a request that waits on everything.
    prepared = dispatch_batch._Plans(chain_repo)
    _project, prepared_node, _derived = dispatch_batch._entry_node(
        0, entry, plans=prepared
    )
    assert prepared_node.waits_only_after is True, "the flag survives _entry_node"
    assert prepared_node.after == [UPSTREAM]

    result = dispatch_batch.queue_batch(
        [entry],
        session=SESSION,
        config_for=lambda _project: json.loads(json.dumps(CONFIG)),
        repo_root=chain_repo,
    )
    row = result["results"][0]
    assert result["queued"] == 1, json.dumps(row)
    assert row["status"] == queue_admission.AWAITING_UPSTREAM_PHASE, row
    assert row["waits"] == [f"run:{UPSTREAM}"], "the section's waits are not inherited"

    pointer = runs.read_pointer(row["run_id"])
    assert pointer["node"]["waits_only_after"] is True, "the flag is stored"
    assert pointer["node"]["section"] == "s2", "the section is still recorded"
    assert queue_admission.section_waits_for(pointer) == [f"run:{UPSTREAM}"]

    queue_admission.advance_waiting_requests(runs.list_live())
    assert _waits(row["run_id"]) == [f"run:{UPSTREAM}"], "no pass widens it"

    _land(chain_repo, UPSTREAM)
    queue_admission.advance_waiting_requests(runs.list_live())
    assert runs.read_pointer(row["run_id"])["phase"] == queue_admission.QUEUED_PHASE


def test_the_flag_without_an_after_ref_is_refused() -> None:
    """A request waiting only on refs it does not name would wait on nothing."""
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--session",
            SESSION,
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--section",
            "s2",
            "--node",
            "n-empty",
            "--goal",
            "read the pinned configuration",
            "--waits-only-after",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "waits on nothing" in result.output

    verdict = validate_node(
        TaskNode(
            id="n-empty",
            goal="read the pinned configuration",
            plan=PLAN,
            section="s2",
            waits_only_after=True,
        )
    )
    assert "waits-only-after" in {
        str(finding.get("property")) for finding in verdict.findings
    }, "the batch path refuses it through the node contract too"
