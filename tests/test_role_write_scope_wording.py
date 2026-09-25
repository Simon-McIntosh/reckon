"""The write refusal names the delivery roots it matched against.

A role that may not write repository paths is refused whenever a declared path
is not one of the delivery roots it does have. The refusal used to describe
every such path as resolving inside the repository, which is false for a path
outside both the repository and the roots — measured on a report path under a
session's own scratch directory, whose directory name happened to carry the
repository's flattened path and so read plausibly.

The refusal now reports the check it made rather than a conclusion drawn from a
list it never named: a path inside the repository says so, and a path outside
every delivery root names the roots it was matched against together with the
correction they allow, so a caller can move the path in one edit. The two cases
are kept apart because they call for opposite corrections.

Both cases are driven through the entry point that compiles the refusal, and
every wording is read from the emitted message rather than from a constant in
the module under test — a refusal that stopped naming the roots would otherwise
pass on some other sentence still matching the old phrasing.

The declared negative control restores the sentence reporting every
non-delivery-root path as resolving inside the repository, and the assertion it
reddens is the one requiring the outside-both-roots case to report the
enumerated roots rather than the repository sentence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.runs import reports_dir, runs_dir

# The mutation this file's check must fail against, spelled out so a red log
# can repeat it verbatim as its first line and a reader can reproduce it.
NEGATIVE_CONTROL_MUTATION = (
    "Restore the sentence reporting every non-delivery-root path as resolving "
    "inside the repository, which must redden "
    "tests/test_role_write_scope_wording.py::"
    "test_a_path_outside_the_repository_names_the_roots_checked"
)

# The XDG config home this host carries. A synthesised home is asserted to be
# the one both cases resolved through; the legacy ~/docs-server layout is not
# planted here, and the check below fails only if a message names this one.
REAL_CONFIG_HOME = Path.home() / ".config" / "reckon"

DISPATCH_CONFIG = {
    "default_backend": "native",
    "backends": {"native": {"launch": "in-harness", "time_budget": "20m"}},
    "roles": {
        "test": {"execution_capable": True},
        "implement": {"execution_capable": True},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep flight resolution off the workstation's real host layer."""
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    """A synthesised repository, and the working directory inside it.

    The gate judges a declared path against the repository the dispatch runs
    from, so the working directory moves into this tree; it is a real git
    checkout and not a stand-in for one, and it is cut from no other tree.
    """
    root = tmp_path / "repo"
    source = root / "reckon" / "crew" / "node.py"
    source.parent.mkdir(parents=True)
    source.write_text("# module under verification\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.chdir(root)
    return root


def _outside_path(tmp_path: Path) -> Path:
    """A path outside the repository and outside every delivery root."""
    return tmp_path / "scratch" / "session-scratch" / "report.md"


def _node(role: str, write_paths: list[str], *, manifest_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="scope-wording-node",
        goal="measure the timestamp work against a stated base",
        plan="plan-a",
        section="s1",
        role=role,
        done_when="pytest tests/example.py reports 3 passed",
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=str(manifest_path),
        spec_level="exact",
    )


def _scoped_refusal(role: str, write_paths: list[str], *, manifest_path: Path) -> str:
    """Composed refusal text, read from the message the validator emits."""
    verdict = crew.validate_node(
        _node(role, write_paths, manifest_path=manifest_path), budget_ceiling="25m"
    )
    assert "scoped" in verdict.failed_properties, verdict.findings
    return " | ".join(
        str(finding["detail"])
        for finding in verdict.findings
        if finding["property"] == "scoped"
    )


def _manifest() -> Path:
    return runs_dir() / "r-scope-wording" / "manifest.md"


# ── The two cases are told apart in the message ────────────────────────────


def test_a_path_outside_the_repository_names_the_roots_checked(
    home, repository, tmp_path
):
    """The recorded defect: a scratch path reported as resolving in the repo."""
    manifest = _manifest()
    declared = _outside_path(tmp_path)
    assert not declared.resolve().is_relative_to(repository.resolve()), declared

    detail = _scoped_refusal("test", [str(declared)], manifest_path=manifest)

    # The declared negative control restores the sentence that reported every
    # non-delivery-root path as resolving inside the repository. This line is
    # the assertion it reddens: that sentence names no root at all.
    assert str(runs_dir().resolve()) in detail, detail
    assert str(reports_dir().resolve()) in detail, detail
    assert str(manifest) in detail, detail
    assert str(repository.resolve()) in detail, detail
    assert "resolve inside the repository" not in detail, detail
    assert "declare the path under one of those roots" in detail, detail
    assert str(declared) in detail, detail


def test_a_path_inside_the_repository_still_says_so(home, repository):
    """The more serious case keeps its own sentence and names the root checked."""
    manifest = _manifest()
    declared = repository / "reckon" / "crew" / "node.py"

    detail = _scoped_refusal("test", [str(declared)], manifest_path=manifest)

    assert "inside the repository" in detail, detail
    assert str(repository.resolve()) in detail, detail
    assert "outside every delivery root" not in detail, detail


def test_a_mixed_declaration_carries_both_sentences(home, repository, tmp_path):
    """A mixed scope is not collapsed onto whichever list was checked first."""
    manifest = _manifest()
    inside = repository / "reckon" / "crew" / "node.py"
    outside = _outside_path(tmp_path)

    detail = _scoped_refusal(
        "test", [str(inside), str(outside)], manifest_path=manifest
    )

    assert "inside the repository" in detail, detail
    assert "outside every delivery root" in detail, detail
    assert str(outside) in detail, detail


def test_the_delivery_roots_themselves_are_still_admitted(home, repository):
    """The refusal enumerates exactly the roots the gate admits."""
    manifest = _manifest()
    verdict = crew.validate_node(
        _node(
            "test",
            [str(runs_dir()), str(reports_dir()), str(manifest)],
            manifest_path=manifest,
        ),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_the_dispatch_gate_refuses_an_outside_path_with_the_roots(
    home, repository, tmp_path
):
    """The wording reaches a caller through the dispatch gate, not only the judge."""
    declared = _outside_path(tmp_path)
    resolution = crew.plan_dispatch(
        node=_node("test", [str(declared)], manifest_path=_manifest()),
        config=DISPATCH_CONFIG,
    )

    assert not resolution.validation.ok
    detail = " | ".join(
        str(finding["detail"])
        for finding in resolution.validation.findings
        if finding["property"] == "scoped"
    )
    assert str(runs_dir().resolve()) in detail, detail
    assert "resolve inside the repository" not in detail, detail


# ── The synthesised home is the one that was read ──────────────────────────


def test_the_real_config_home_is_untouched(home, repository, tmp_path):
    """Both cases resolve through the injected home, and write nothing there.

    A leak would show as a path under the real config home in the message, or
    as an entry created there; both are asserted, and the injected roots must
    appear in the message, so a home that silently stopped being honoured
    cannot pass this check.
    """
    marker = "r-scope-wording-real-home-probe"
    manifest = runs_dir() / marker / "manifest.md"
    probe = REAL_CONFIG_HOME / "crew" / "runs" / marker
    existed = REAL_CONFIG_HOME.exists()
    assert not probe.exists(), probe

    outside_detail = _scoped_refusal(
        "test", [str(_outside_path(tmp_path))], manifest_path=manifest
    )
    inside_detail = _scoped_refusal(
        "test",
        [str(repository / "reckon" / "crew" / "node.py")],
        manifest_path=manifest,
    )

    assert str(REAL_CONFIG_HOME) not in outside_detail, outside_detail
    assert str(REAL_CONFIG_HOME) not in inside_detail, inside_detail
    assert str(runs_dir().resolve()) in outside_detail, outside_detail
    # A leak shows either as a message naming the real home, asserted above, or
    # as an entry created under it — so the probe is re-checked after both cases.
    assert REAL_CONFIG_HOME.exists() == existed, REAL_CONFIG_HOME
    assert not probe.exists(), probe
    assert runs_dir() == (home / "crew" / "runs").resolve()
