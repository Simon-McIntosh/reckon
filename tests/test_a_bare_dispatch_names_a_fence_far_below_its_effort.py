"""A dispatch whose default fence is far below the estimate says so.

A node dispatched with no ``--time-budget`` takes the role overlay's default,
and the shipped implement figure is minutes where a plan section may declare
hours. Nothing compared the two, so a six-hour section was fenced at eight
minutes and the worker was stopped with most of its work undone. The dispatch
now carries one warning — in its result and on stderr, before anything
launches — naming the estimate, the resolved fence, the flight key that
supplied it and the ``--time-budget`` to pass.

The estimate read here is the node's own figure or the hours its named section
declares, never the plan's total: a plan's effort is the same figure for every
section of it, so a quarter-hour section of a twenty-hour plan would be warned
about wrongly. That distinction is one of the cases below.

Negative control: removing the warning turns the six-hour case red.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import capabilities, flight
from reckon import cli as cli_module
from reckon.crew import dispatch_plan, picker
from reckon.crew.node import TaskNode

# A plan whose total effort is twenty hours while one of its sections declares
# a quarter of an hour. The pair is what tells a section's own figure apart
# from the plan's total: reading the total would warn about the short section.
PLAN_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="proj">
  <meta name="reckon-type" content="plan">
  <meta name="plan-slug" content="a-long-plan">
  <meta name="plan-title" content="A Long Plan">
  <meta name="plan-status" content="active">
  <meta name="plan-effort-hours" content="20">
</head>
<body>
  <h2 id="s1">&#167;1 &mdash; Six hours of work</h2>
  <section data-reckon="section" data-id="s1" data-effort-hours="6"
           data-capability-version="1.0" data-capability-class="general"
           data-capability-reasoning="standard"
           data-capability-verification="strict" data-capability-risk="low"
           data-status="implementable" data-links=""></section>
  <h2 id="s2">&#167;2 &mdash; A quarter hour of work</h2>
  <section data-reckon="section" data-id="s2" data-effort-hours="0.25"
           data-capability-version="1.0" data-capability-class="general"
           data-capability-reasoning="standard"
           data-capability-verification="strict" data-capability-risk="low"
           data-status="implementable" data-links=""></section>
</body>
</html>
"""

# The host layer declares a cli backend and names no time budget anywhere, so
# the resolved implement fence is the shipped ``roles.implement.time_budget``
# of eight minutes rather than a figure this fixture wrote down.
FLIGHT_LAYER = """\
default_backend: worker
backends:
  worker:
    launch: cli
    command: codex
    model: some-model
    effort: high
    sandbox: worktree-full
    session_reuse: true
"""

GOAL = "land the long section's first cut"
DONE_WHEN = (
    "uv run pytest tests/test_a_bare_dispatch_names_a_fence_far_below_its_effort.py "
    "reports passed"
)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp crew home holding the flight layer a dispatch resolves against."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    (home / "flight.yaml").write_text(FLIGHT_LAYER, encoding="utf-8")
    return home


@pytest.fixture()
def plan_repo(tmp_path: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed throwaway repository carrying one plan of two sections."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "a-long-plan.html").write_text(
        PLAN_HTML, encoding="utf-8"
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/a-long-plan.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (crew_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _resolved_config(crew_home: Path) -> dict:
    """Resolve the flight layer the fixture's crew home carries."""
    return flight.resolve(host_path=crew_home / "flight.yaml").config


def _arguments(
    repo: Path,
    *,
    node_id: str,
    section: str,
    role: str = "implement",
    time_budget: str = "",
    plan: str = "a-long-plan",
    estimated_hours: str = "",
) -> list[str]:
    """The dispatch a bare coordinator would issue for one plan section."""
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        plan,
        "--section",
        section,
        "--role",
        role,
        "--node",
        node_id,
        "--goal",
        GOAL,
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "result.json",
        "--spec-level",
        "guided",
        "--session",
        f"session-{node_id}",
        "--repo",
        str(repo),
        "--dry-run",
    ]
    if time_budget:
        arguments += ["--time-budget", time_budget]
    if estimated_hours:
        arguments += ["--estimated-hours", estimated_hours]
    return arguments


def _dispatch(repo: Path, arguments: list[str]):
    return CliRunner().invoke(cli_module.main, arguments)


def _document(result) -> dict:
    """Return the one JSON document the dispatch wrote to stdout."""
    return json.loads(result.stdout)


def _fence_lines(document: dict) -> list[str]:
    """The warnings this node's check owns, told apart by what they name."""
    return [line for line in document.get("warnings", []) if "--time-budget" in line]


def test_the_shipped_implement_fence_is_what_the_fixture_resolves(
    crew_home: Path,
) -> None:
    """The eight-minute default under test is the shipped one, not a literal.

    Read from the resolved flight layer rather than written into the fixture,
    so a change to the shipped figure moves this check with it instead of
    leaving it asserting a number nothing declares any more.
    """
    config = _resolved_config(crew_home)

    assert config["roles"]["implement"]["time_budget"] == "8m"
    assert "time_budget" not in config["backends"]["worker"]


def test_a_six_hour_section_names_the_default_fence(
    plan_repo: Path, crew_home: Path
) -> None:
    """One line, naming the estimate, the fence, its key and the flag."""
    result = _dispatch(
        plan_repo, _arguments(plan_repo, node_id="bare-six-hour", section="s1")
    )

    document = _document(result)
    lines = _fence_lines(document)

    assert len(lines) == 1, document.get("warnings")
    line = lines[0]
    assert "6h" in line
    assert "8m" in line
    assert "roles.implement.time_budget" in line
    assert "--time-budget" in line
    # The dispatch is warned about, not refused.
    assert document["ok"]


def test_the_line_reaches_stderr_while_stdout_carries_nothing_under_it(
    plan_repo: Path, crew_home: Path
) -> None:
    """A caller decoding stdout hears the warning on the notice channel."""
    result = _dispatch(
        plan_repo, _arguments(plan_repo, node_id="bare-channel", section="s1")
    )

    document = _document(result)
    line = _fence_lines(document)[0]

    assert line in result.stderr
    # The line reaches the notice channel; it does not also land on stdout as
    # a second document the caller would have to parse around.
    assert line not in result.stdout.splitlines()


def test_an_explicit_budget_under_the_quarter_carries_no_line(
    plan_repo: Path, crew_home: Path
) -> None:
    """The exemption is the named budget, not the size of it.

    One hour is still under a quarter of the six-hour estimate, so a check
    that warned on the ratio alone would emit a line here and fail.
    """
    result = _dispatch(
        plan_repo,
        _arguments(plan_repo, node_id="named-budget", section="s1", time_budget="1h"),
    )

    document = _document(result)

    assert not _fence_lines(document), document.get("warnings")
    assert document["time_budget"] == "1h"


def test_a_short_section_of_a_long_plan_carries_no_line(
    plan_repo: Path, crew_home: Path
) -> None:
    """The section's figure is read, never the plan's twenty-hour total.

    A quarter hour is six times the eight-minute fence, so the plan's total
    would warn here while the section's own figure does not.
    """
    result = _dispatch(
        plan_repo, _arguments(plan_repo, node_id="short-section", section="s2")
    )

    document = _document(result)

    assert not _fence_lines(document), document.get("warnings")


def test_a_node_with_no_estimate_carries_no_line(
    plan_repo: Path, crew_home: Path, tmp_path: Path
) -> None:
    """Nothing to compare the fence against, so nothing is said.

    A node whose authority is a brief names no plan section, so neither the
    node nor a section declares hours.
    """
    brief = tmp_path / "brief.md"
    brief.write_text(
        "Extend reckon/crew/dispatch_plan.py rather than adding a sibling.\n",
        encoding="utf-8",
    )
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--brief",
        str(brief),
        "--role",
        "implement",
        "--node",
        "no-estimate",
        "--goal",
        GOAL,
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "result.json",
        "--spec-level",
        "guided",
        "--session",
        "session-no-estimate",
        "--repo",
        str(plan_repo),
        "--dry-run",
    ]

    document = _document(_dispatch(plan_repo, arguments))

    assert not _fence_lines(document), document.get("warnings")


def test_two_role_layers_name_the_one_the_fence_came_from(
    plan_repo: Path, crew_home: Path
) -> None:
    """A later role layer overwrites an earlier one, and is the one named.

    The role declares a budget of its own and a different one for this node's
    spec level; the resolved fence is the spec level's, so naming the role's
    own figure would send a reader to edit a layer that supplied nothing.
    """
    (crew_home / "flight.yaml").write_text(
        FLIGHT_LAYER
        + "roles:\n"
        + "  implement:\n"
        + "    time_budget: 1h\n"
        + "    by_spec_level:\n"
        + "      guided:\n"
        + "        time_budget: 8m\n",
        encoding="utf-8",
    )

    result = _dispatch(
        plan_repo, _arguments(plan_repo, node_id="two-layers", section="s1")
    )

    document = _document(result)
    line = _fence_lines(document)[0]

    assert "8m" in line
    assert "roles.implement.by_spec_level.guided.time_budget" in line
    assert "roles.implement.time_budget" not in line


def test_a_budget_layer_is_named_by_the_figure_it_declares() -> None:
    """Every named key is one whose own declared figure is this run's fence.

    The role declares a budget of its own, two more under capability classes —
    the first of the two declaring a figure the run did not resolve to — and a
    spec level under it. A read that takes the first key holding any budget, or
    the first class alphabetically, names a layer that supplied nothing.
    """
    config = {
        "fences": {"time_budget": "8m"},
        "roles": {
            "implement": {
                "time_budget": "2h",
                "by_spec_level": {"guided": {"time_budget": "30m"}},
                "by_capability_class": {
                    "alpha": {"time_budget": "1h"},
                    "zeta": {"time_budget": "8m"},
                },
            }
        },
    }
    node = TaskNode(
        id="a-layer-read", goal="read a layer", plan="a-plan", spec_level="guided"
    )
    read = dispatch_plan._time_budget_layer_key

    # Each layer in turn is named only by the figure it actually declares.
    assert read(config, {}, backend_name="worker", node=node, budget="8m") == (
        "roles.implement.by_capability_class.zeta.time_budget"
    )
    assert read(config, {}, backend_name="worker", node=node, budget="30m") == (
        "roles.implement.by_spec_level.guided.time_budget"
    )
    # Two layers declare an hour: the later one is the layer that supplies it.
    assert read(config, {}, backend_name="worker", node=node, budget="1h") == (
        "roles.implement.by_capability_class.alpha.time_budget"
    )
    assert read(config, {}, backend_name="worker", node=node, budget="2h") == (
        "roles.implement.time_budget"
    )
    # A fence no layer declares is named by no key, never by the first one held.
    assert read(config, {}, backend_name="worker", node=node, budget="5s") == ""


def test_the_sections_effort_is_read_the_way_the_picker_reads_it(
    plan_repo: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The section's effort is the picker's own read of the record.

    The advisory holds no second opinion about what a section costs: it asks
    the read the picker sizes nodes with, so the two cannot drift. Replacing
    that read with one that answers nothing leaves the six-hour case with
    nothing to compare the fence against.
    """
    monkeypatch.setattr(picker, "_section_effort_hours", lambda *_args, **_kwargs: None)

    result = _dispatch(
        plan_repo, _arguments(plan_repo, node_id="shared-read", section="s1")
    )

    document = _document(result)

    assert not _fence_lines(document), document.get("warnings")


def test_the_nodes_own_estimate_is_read_before_the_section(
    plan_repo: Path, crew_home: Path
) -> None:
    """An --estimated-hours figure is the node's estimate and stands alone."""
    result = _dispatch(
        plan_repo,
        _arguments(
            plan_repo,
            node_id="own-estimate",
            section="s2",
            estimated_hours="12",
        ),
    )

    document = _document(result)
    lines = _fence_lines(document)

    assert len(lines) == 1, document.get("warnings")
    assert "12h" in lines[0]


def test_a_refused_node_carries_no_line_and_keeps_its_one_document(
    plan_repo: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch that refuses the node advises it of no fence.

    The refused node is never launched, so a fence cannot stop it and the line
    would be advice with no run to advise. The refusal's own document is what a
    caller must be able to decode, so the notice channel stays silent here.
    """
    arguments = _arguments(plan_repo, node_id="refused", section="s1")
    # The key is the one the dispatch itself reports for its agent, so the
    # horizon below lands on this node's configuration without the fixture
    # restating how a configuration is spelled.
    key = _document(_dispatch(plan_repo, arguments))["competence"]["agent_key"]
    monkeypatch.setattr(
        capabilities,
        "load_capabilities",
        lambda: _horizon_cache(key, horizon=2.5),
    )

    result = _dispatch(plan_repo, arguments)
    document = _document(result)

    assert document["error"] == "competence-refusal", document
    assert not _fence_lines(document), document.get("warnings")
    assert "--time-budget" not in result.stderr


def _horizon_cache(key: str, *, horizon: float) -> dict:
    """A capability cache declaring one agent's competence horizon."""
    return {
        "configurations": [
            {
                "key": key,
                "competence_horizon_hours": horizon,
                "speed": {"mean": 1.0},
            }
        ]
    }


def test_the_real_crew_home_is_untouched(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every case resolves against the fixture's home, never the operator's."""
    from reckon._store import _config_home

    assert Path(_config_home()).resolve() == crew_home.resolve()
    assert Path.home() != crew_home
