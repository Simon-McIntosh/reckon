"""A merged-head gate that cannot start says so, instead of blaming the merge.

The replay re-runs a run's recorded gate command against the tree that ships.
A worker records its interpreter relatively — ``.venv/bin/python`` — because
``.venv`` is there. Measured on 22 November 2026: the merged-head tree carried no
``.venv``, the shell exited 127 before the gate ran, and the finding read "the
merge changes what this node's gate proves", sending the coordinator into the
merge for a missing program.

These cases pin the three outcomes that must stay distinct:

* a relative environment interpreter resolves and the gate runs at a tree with
  no ``.venv`` of its own;
* a program that exists nowhere is reported as a gate that did not start,
  naming the program and the tree, and never as a gate the merge changed;
* a gate that starts and fails is still a failure, carrying its failure ids.
"""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion_gate
from reckon.crew.node import CrewError

PROGRAM_THAT_EXISTS_NOWHERE = "./no-such-gate-program-6f1c"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], text=True
    ).strip()


@pytest.fixture()
def merged_tree(tmp_path: Path) -> Path:
    """A checkout with a commit and no ``.venv``, as a merged head may be."""
    repo = tmp_path / "merged"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "subject.py").write_text("VALUE = 'merged'\n", encoding="utf-8")
    _git(repo, "add", "subject.py")
    _git(
        repo,
        "commit",
        "-qm",
        "test: prepare the merged tree\n\nRecord the fixture head.",
    )
    return repo


def _replay(tree: Path, command: str, log: Path) -> dict:
    return promotion_gate.rerun_gate_at_integrated_revision(
        repository=tree,
        gate_check={"command": command},
        base_verdict="passed",
        integrated_revision="HEAD",
        timeout_seconds=60.0,
        replay_log_path=log,
    )


def test_a_relative_environment_interpreter_runs_at_a_tree_without_a_venv(
    merged_tree: Path, tmp_path: Path
) -> None:
    """``.venv/bin/python`` is resolved, so the gate runs where the tree holds none."""
    assert not (merged_tree / ".venv").exists(), "the fixture must carry no .venv"
    log = tmp_path / "verify-gate.log"

    report = _replay(merged_tree, ".venv/bin/python -c 'print(\"the gate ran\")'", log)

    assert report["integrated_verdict"] == "passed", (
        f"the gate did not run: verdict={report['integrated_verdict']} "
        f"exit_status={report['exit_status']} finding={report['finding']}"
    )
    assert report["gate_started"] is True
    assert report["ran"] is True
    assert report["exit_status"] == 0
    assert report["ok"] is True
    assert report["gate_program"] == ".venv/bin/python"
    assert report["gate_program_rewritten"]
    text = log.read_text(encoding="utf-8")
    assert "# command:" in text
    assert "the gate ran" in text
    assert text.rstrip().splitlines()[-1] == "EXIT=0"


def test_a_program_that_exists_nowhere_is_refused_naming_it_and_the_tree(
    merged_tree: Path, tmp_path: Path
) -> None:
    """A gate that never started is not a gate the merge changed."""
    log = tmp_path / "verify-gate.log"
    tree = str(merged_tree.resolve())

    report = _replay(merged_tree, f"{PROGRAM_THAT_EXISTS_NOWHERE} --version", log)

    assert report["integrated_verdict"] == "not-run", (
        f"a gate that never started was reported as "
        f"{report['integrated_verdict']} with exit_status="
        f"{report['exit_status']} and finding={report['finding']}"
    )
    assert report["gate_started"] is False
    assert report["ran"] is False
    assert report["exit_status"] is None
    assert report["ok"] is False
    assert report["missing_program"] == PROGRAM_THAT_EXISTS_NOWHERE
    assert PROGRAM_THAT_EXISTS_NOWHERE in str(report["reason"])
    assert tree in str(report["reason"])

    finding = report["finding"]
    assert finding is not None
    assert finding["gate_not_started"] is True
    assert finding["missing_program"] == PROGRAM_THAT_EXISTS_NOWHERE
    assert PROGRAM_THAT_EXISTS_NOWHERE in finding["message"]
    assert tree in finding["message"]
    assert "the merge changes what this node's gate proves" not in finding["message"]

    text = log.read_text(encoding="utf-8")
    assert "# replayed revision:" in text
    assert PROGRAM_THAT_EXISTS_NOWHERE in text
    assert "EXIT=" not in text, "a gate that never started has no exit status"


def test_a_gate_that_starts_and_fails_is_still_reported_with_its_failure_ids(
    merged_tree: Path, tmp_path: Path
) -> None:
    """A real failure keeps its verdict and the ids its own output named."""
    log = tmp_path / "verify-gate.log"

    report = _replay(
        merged_tree,
        'sh -c \'echo "FAILED tests/test_sample.py::test_broken"; '
        'echo "1 failed"; exit 1\'',
        log,
    )

    assert report["gate_started"] is True
    assert report["ran"] is True
    assert report["exit_status"] == 1
    assert report["integrated_verdict"] == "failed"
    assert report["finding"] is not None
    assert report["finding"]["integrated_verdict"] == "failed"
    assert report["finding"]["failure_ids"] == ["tests/test_sample.py::test_broken"]


def test_one_resolver_serves_both_the_promotion_guard_and_the_replay(
    merged_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard that admits a command and the replay that runs it share one resolver.

    A second tokenisation, PATH lookup and executable test is a second answer to
    "can this program start", and with two answers the promotion service can
    admit a command the replay cannot start, or refuse one it can. The resolver
    therefore lives in one module and the guard asks it, which this pins by
    making the resolver answer for both.
    """
    from reckon.crew import landing_measure

    for name in ("gate_program", "resolve_gate_program", "rewrite_gate_program"):
        assert not hasattr(landing_measure, name), (
            f"landing_measure re-implements {name}; the resolver belongs to "
            "promotion_gate and the guard must ask it"
        )

    answers: list[dict] = []

    def recording(command: str, **options) -> dict:
        answers.append({"command": command, "options": options})
        return {
            "program": PROGRAM_THAT_EXISTS_NOWHERE,
            "seats": [str(merged_tree.resolve())],
            "resolved": None,
            "source": None,
        }

    monkeypatch.setattr(promotion_gate, "resolve_gate_program", recording)
    with pytest.raises(CrewError) as refused:
        promotion_gate._require_executable_gate_command(
            "r-20261122T120000000000-resolver",
            {"command": f"{PROGRAM_THAT_EXISTS_NOWHERE} --version", "exit_status": 0},
        )

    assert answers, "the guard did not ask the shared resolver"
    assert answers[0]["command"] == f"{PROGRAM_THAT_EXISTS_NOWHERE} --version"
    assert answers[0]["options"].get("interpreter"), (
        "the guard resolved without the interpreter fallback the replay passes, "
        "so a command the replay can start could be refused at promotion"
    )
    assert PROGRAM_THAT_EXISTS_NOWHERE in str(refused.value)


def test_the_module_defines_each_shared_helper_once() -> None:
    """A second definition in one module silently shadows the first.

    ``_is_executable_file`` and ``_SHELL_ASSIGNMENT`` are helpers the resolver and
    the guard both depend on. Two definitions of either means the module answers
    two ways depending on which one happens to be read last, which is the defect
    that having one owner of the executable test is supposed to remove.
    """
    source = inspect.getsource(promotion_gate)

    for name in ("_is_executable_file", "_SHELL_ASSIGNMENT"):
        definitions = len(re.findall(rf"^(?:def\s+)?{name}\b", source, re.MULTILINE))
        assert definitions == 1, (
            f"{name} is defined {definitions} times in promotion_gate; the later "
            "definition silently shadows the earlier"
        )


def test_the_rewrite_touches_the_program_token_and_no_earlier_one() -> None:
    """An occurrence of the program's spelling inside an earlier word is not it."""
    command = (
        f"GATE={PROGRAM_THAT_EXISTS_NOWHERE} {PROGRAM_THAT_EXISTS_NOWHERE} --version"
    )

    rewritten = promotion_gate.rewrite_gate_program(
        command, PROGRAM_THAT_EXISTS_NOWHERE, "/opt/gate/python"
    )

    assert rewritten == f"GATE={PROGRAM_THAT_EXISTS_NOWHERE} /opt/gate/python --version"


def test_the_refusal_names_the_program_the_resolver_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``env``-wrapped command is refused naming the interpreter, not ``env``."""
    monkeypatch.setattr(promotion_gate, "_run_checkout_roots", lambda run_id: ())

    with pytest.raises(CrewError) as refused:
        promotion_gate._require_executable_gate_command(
            "r-20261122T120000000000-env",
            {
                "command": f"env -C /nonexistent {PROGRAM_THAT_EXISTS_NOWHERE} --version",
                "exit_status": 0,
            },
        )

    message = str(refused.value)
    assert PROGRAM_THAT_EXISTS_NOWHERE in message
    assert "'env'" not in message
