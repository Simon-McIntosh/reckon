from pathlib import Path

ROOT = Path(__file__).parents[1]


def normalized(text: str) -> str:
    return " ".join(text.split())


def reference() -> str:
    """The effort-routing reference, read verbatim from the repo."""
    return normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "effort-routing.md"
        ).read_text()
    )


def test_local_lane_is_qualified_at_exact_with_measured_record() -> None:
    """The local lane routes `exact` on a measured record, cited as numbers
    beside their sample size."""
    text = reference()
    assert "42 passed of 44 runs" in text
    assert "qualified" in text
    assert (
        "| `exact` | `deepseek-v4-flash` — qualified | 42 passed of 44 runs |" in text
    )


def test_guided_requires_the_measurement_handed_over() -> None:
    """`guided` is routed to the lane only when the measurement is handed over."""
    text = reference()
    assert "30 passed of 38 runs" in text
    assert "permitted only with the measurement handed over" in text


def test_open_is_never_routed_to_the_lane() -> None:
    text = reference()
    assert "zero runs" in text
    assert "`open` | never routed" in text


def test_the_other_local_identifier_is_explicitly_not_routed() -> None:
    text = reference()
    assert "0 passed of 9 runs" in text
    assert "glm-5.3" in text
    assert "explicitly NOT routed" in text


def test_reference_no_longer_denies_a_live_mapping() -> None:
    """The section records the measured mapping instead of claiming none exists."""
    text = reference()
    assert "no live mapping" not in text
    assert "now has a live mapping" in text
