"""A picker input that cannot be built is recorded, never raised out of dispatch.

The three inputs dispatch computes for the picker are advisory: the ledger
rows, the shared verdict inputs and the budget view. The picker re-reads
whatever it is not handed, so a reader of any of them failing must not abort
the dispatch it was only meant to inform. Reading them inline made a damaged
ledger raise out of the picker before the repair path reached its own
ledger-reason refusal, so a corrupt ledger surfaced as a CorruptEnvelopeError
instead of the refusal the code already knows how to write. Each input is now
built behind a guard that records the failure against the input's own name and
lets the picker fall back.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_picker as dispatch_picker_module
from reckon import _plan_html, crew, ledger

PROJECT = "proj"
PLAN = "plan-a"

DISPATCH_CONFIG = {
    "default_backend": "native",
    "backends": {
        "native": {
            "launch": "in-harness",
            "model": "embedded-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

INPUT_BUILDERS = {
    "records": "_picker_ledger_rows",
    "verdict_inputs": "_picker_verdict_inputs",
    "budget_snapshot": "_picker_budget_snapshot",
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_plan(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc">'
        '<h2 id="s2">&sect;2 &mdash; Section two</h2>'
        "</main></body></html>\n"
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    fleet = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _git(root, "add", "skills")
    _git(root, "commit", "-q", "-m", "test: provision the fleet script")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _selection(action: str = "route", backend: str | None = "native", **extra):
    fields = {
        "action": action,
        "backend": backend,
        "family": "test",
        "model": "embedded-model" if backend == "native" else None,
        "effort": "high",
        "probabilities": {"native": 0.9},
        "confidence": 0.9,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "latency_ms": 1.0,
        "excluded": [],
    } | extra
    return SimpleNamespace(as_dict=lambda: fields)


def _dispatch_one(
    repository: Path, tmp_path: Path, node_id: str, *, repairs: str = ""
) -> dict:
    node = crew.TaskNode(
        id=node_id,
        goal="keep a failing picker input from blocking the dispatch",
        plan=PLAN,
        section="s2",
        spec_level="guided",
        done_when="the section's tests pass and the plan records the advance",
        write_paths=["package/target.py"],
        time_budget="20m",
        manifest_path=str(tmp_path / f"{node_id}.md"),
    )
    return crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repository,
        config=DISPATCH_CONFIG,
        session="dispatch-session",
        launcher=lambda *args, **kwargs: 42001,
        check_budget=False,
        repairs=repairs,
    )


def _raise_builder(name: str):
    def build(*args, **kwargs):
        raise RuntimeError(f"boom:{name}")

    return build


@pytest.mark.parametrize("name", sorted(INPUT_BUILDERS))
def test_a_failing_picker_input_never_blocks_dispatch(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """One builder raising leaves the dispatch to its own verdict.

    The failing builder is injected by name while the picker itself is a
    healthy stub: the recorded fallback must name only the input that failed,
    so it is attributable to the guard rather than to the picker.
    """
    monkeypatch.setattr(dispatch_picker_module, INPUT_BUILDERS[name], _raise_builder(name))
    from reckon.crew import picker

    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: _selection())

    record = _dispatch_one(repository, tmp_path, f"picker-input-{name}")

    selection = record["picker_selection"]
    assert selection["action"] == "fallback"
    assert f"boom:{name}" in selection["fallback_reason"]
    assert set(selection["input_errors"]) == {name}
    assert selection["input_errors"][name].startswith(f"RuntimeError: boom:{name}")


def test_a_healthy_dispatch_records_the_picker_answer(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with no builder failing, the picker's own answer is recorded
    and no input error is fabricated, so the fallback assertions are not
    vacuous."""
    from reckon.crew import picker

    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: _selection())

    record = _dispatch_one(repository, tmp_path, "picker-input-control")

    selection = record["picker_selection"]
    assert selection["action"] == "route"
    assert selection["backend"] == "native"
    assert "input_errors" not in selection


def test_a_corrupt_ledger_refuses_with_the_ledger_reason(
    repository: Path, tmp_path: Path
) -> None:
    """The guarded ledger read no longer pre-empts the repair path's refusal.

    The picker's ledger read and the repair check read the same damaged file;
    before the guard the former raised CorruptEnvelopeError out of the
    dispatch, so the refusal the repair path writes was never reached.
    """
    damaged = ledger.ledger_path(PROJECT, root=repository)
    damaged.parent.mkdir(parents=True, exist_ok=True)
    damaged.write_text(
        '<<<<<<< HEAD\n{"runs": [\n=======\n{}\n>>>>>>> repair\n',
        encoding="utf-8",
    )

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch_one(
            repository,
            tmp_path,
            "picker-input-damaged-ledger",
            repairs="r-20260918T092500000000-maybe-present",
        )

    message = str(refusal.value)
    assert "could not be read" in message
    assert str(damaged) in message
