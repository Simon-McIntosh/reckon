import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]

COMMITTED_LEDGER = ROOT / "docs" / "state" / "reckon" / "crew.json"

LANE_MODEL = "deepseek-v4-flash"
LANE_PEER = "glm-5.3"


def normalized(text: str) -> str:
    return " ".join(text.split())


def reference() -> str:
    """The effort-routing reference, read verbatim from the repo."""
    return normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "effort-routing.md"
        ).read_text()
    )


def documented_filter(ledger: Path, model: str, spec_level: str) -> tuple[int, int]:
    """Run the filter the reference documents against one committed ledger.

    Keeps runs whose agent model is the lane backend at the declared spec
    level; a run passes iff its gate is exactly "passed"; the run count is
    every kept run. Each committed run counts once — a re-dispatch of the same
    node is a distinct run.
    """
    data = json.loads(ledger.read_text())
    runs = data["data"]["runs"]
    selected = [
        run
        for run in runs
        if (run.get("agent") or {}).get("model") == model
        and run.get("spec_level") == spec_level
    ]
    passed = sum(1 for run in selected if run.get("gate") == "passed")
    return passed, len(selected)


def test_local_lane_stays_qualified_with_the_measurement_handed_over() -> None:
    """The routing decisions themselves are prose: `exact` is qualified and
    `guided` is permitted only with the measurement handed over."""
    text = reference()
    assert "qualified" in text
    assert "`deepseek-v4-flash` — qualified" in text
    assert "permitted only with the measurement handed over" in text


def test_figures_are_a_dated_snapshot_with_sample_sizes() -> None:
    """Each figure is labelled a snapshot carrying an ISO date and its sample
    size, not a standing fact that pins a number."""
    text = reference()
    assert "Snapshot (dated" in text
    assert re.search(r"Snapshot \(dated \d{4}-\d{2}-\d{2}\)", text)
    assert "not a standing fact" in text
    assert re.search(r"passed of \d+ runs", text)
    assert "sample size" in text


def test_reference_states_the_derivation_recipe() -> None:
    """The reference names the ledger, the filter and the deduplication rule
    beside the figures, so a reader can reproduce them."""
    text = reference()
    assert "docs/state/<project>/crew.json" in text
    assert "agent.model" in text
    assert "spec_level" in text
    assert "gate" in text
    assert "Deduplication" in text
    assert "Each committed ledger run counts once" in text
    assert "re-dispatch" in text
    assert "counts once" in text


def test_open_is_never_routed_to_the_lane() -> None:
    text = reference()
    assert "zero runs" in text
    assert "`open` | never routed" in text


def test_the_other_local_identifier_is_explicitly_not_routed() -> None:
    text = reference()
    assert "glm-5.3" in text
    assert "explicitly NOT routed" in text
    # the exclusion figure carries the same dated-snapshot framing as the
    # routed rows: its count reads "<passed> of <sample> runs", not a pin
    assert re.search(r"glm-5\.3.*?its snapshot reads \d+ passed of \d+ runs", text)


def test_reference_no_longer_denies_a_live_mapping() -> None:
    """The section records the measured mapping instead of claiming none exists."""
    text = reference()
    assert "no live mapping" not in text
    assert "now has a live mapping" in text


def test_documented_filter_runs_against_the_committed_ledger() -> None:
    """The documented filter runs from the committed ledger and returns a pass
    count and a run count, without asserting they equal the snapshot."""
    assert COMMITTED_LEDGER.is_file()
    for model, spec_level in (
        (LANE_MODEL, "exact"),
        (LANE_MODEL, "guided"),
        (LANE_PEER, "exact"),
    ):
        passed, runs = documented_filter(COMMITTED_LEDGER, model, spec_level)
        assert isinstance(passed, int)
        assert isinstance(runs, int)
        assert runs >= 0
        assert 0 <= passed <= runs
