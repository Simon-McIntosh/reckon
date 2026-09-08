from __future__ import annotations

import pytest

from reckon.crew.quota_weight import (
    EFFICIENT_MODEL,
    INPUT_SURCHARGE_MULTIPLIER,
    LONG_CONTEXT_INPUT_THRESHOLD,
    MIDDLE_MODEL,
    MODEL_RATES,
    OUTPUT_SURCHARGE_MULTIPLIER,
    REFERENCE_MODEL,
    RelativeQuotaWeight,
    UnknownQuotaWeight,
    quota_weight,
)


def _expected_weight(
    model: str,
    input_tokens: int,
    output_tokens: int,
    requests_over_threshold: int = 0,
) -> float:
    total_tokens = input_tokens + output_tokens
    if total_tokens == 0:
        return 0.0

    rate = MODEL_RATES[model]
    reference_rate = MODEL_RATES[REFERENCE_MODEL]
    input_factor = 1.0 + requests_over_threshold * (INPUT_SURCHARGE_MULTIPLIER - 1.0)
    output_factor = 1.0 + requests_over_threshold * (OUTPUT_SURCHARGE_MULTIPLIER - 1.0)
    return (
        input_tokens
        * (rate.input_per_million / reference_rate.input_per_million)
        * input_factor
        + output_tokens
        * (rate.output_per_million / reference_rate.output_per_million)
        * output_factor
    ) / total_tokens


def test_equal_quantities_follow_the_declared_rate_ratios() -> None:
    input_tokens = 12_345
    output_tokens = 12_345

    results = {
        model: quota_weight(model, input_tokens, output_tokens, 0)
        for model in (REFERENCE_MODEL, MIDDLE_MODEL, EFFICIENT_MODEL)
    }

    for model, result in results.items():
        assert isinstance(result, RelativeQuotaWeight)
        assert result.weight == pytest.approx(
            _expected_weight(model, input_tokens, output_tokens)
        )
        assert result.rate is MODEL_RATES[model]
        assert result.surcharge_applied is False

    reference_rate = MODEL_RATES[REFERENCE_MODEL]
    assert results[REFERENCE_MODEL].weight == (
        reference_rate.input_per_million / reference_rate.input_per_million
    )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(1, 1), (91, 7), (4, 113)],
)
def test_declared_family_ordering_is_strict(
    input_tokens: int, output_tokens: int
) -> None:
    reference = quota_weight(REFERENCE_MODEL, input_tokens, output_tokens, 0)
    middle = quota_weight(MIDDLE_MODEL, input_tokens, output_tokens, 0)
    efficient = quota_weight(EFFICIENT_MODEL, input_tokens, output_tokens, 0)

    assert isinstance(reference, RelativeQuotaWeight)
    assert isinstance(middle, RelativeQuotaWeight)
    assert isinstance(efficient, RelativeQuotaWeight)
    assert efficient.weight < middle.weight < reference.weight


def test_each_crossing_adds_the_declared_whole_request_surcharge() -> None:
    input_tokens = LONG_CONTEXT_INPUT_THRESHOLD + 1
    output_tokens = 27_000
    without_surcharge = quota_weight(MIDDLE_MODEL, input_tokens, output_tokens, 0)
    with_surcharge = quota_weight(MIDDLE_MODEL, input_tokens, output_tokens, 1)

    assert isinstance(without_surcharge, RelativeQuotaWeight)
    assert isinstance(with_surcharge, RelativeQuotaWeight)
    expected_without = _expected_weight(MIDDLE_MODEL, input_tokens, output_tokens)
    expected_with = _expected_weight(MIDDLE_MODEL, input_tokens, output_tokens, 1)
    assert without_surcharge.weight == pytest.approx(expected_without)
    assert with_surcharge.weight == pytest.approx(expected_with)
    assert with_surcharge.weight - without_surcharge.weight == pytest.approx(
        expected_with - expected_without
    )
    assert with_surcharge.weight > without_surcharge.weight
    assert with_surcharge.surcharge_applied is True


def test_crossing_count_outweighs_a_larger_uncrossed_cumulative_total() -> None:
    many_small_requests = quota_weight(REFERENCE_MODEL, 16_000_000, 0, 0)
    two_large_requests = quota_weight(REFERENCE_MODEL, 600_000, 0, 2)

    assert isinstance(many_small_requests, RelativeQuotaWeight)
    assert isinstance(two_large_requests, RelativeQuotaWeight)
    assert many_small_requests.weight == pytest.approx(
        _expected_weight(REFERENCE_MODEL, 16_000_000, 0, 0)
    )
    assert two_large_requests.weight == pytest.approx(
        _expected_weight(REFERENCE_MODEL, 600_000, 0, 2)
    )
    assert many_small_requests.weight < two_large_requests.weight


def test_undeclared_model_returns_an_explicit_unknown() -> None:
    undeclared_model = "gpt-5.3-codex-spark"
    result = quota_weight(undeclared_model, 100, 20, 0)
    declared = [
        quota_weight(model, 100, 20, 0)
        for model in (REFERENCE_MODEL, MIDDLE_MODEL, EFFICIENT_MODEL)
    ]

    assert isinstance(result, UnknownQuotaWeight)
    assert result.model_identifier == undeclared_model
    assert result.weight is None
    assert result.rate is None
    assert result.weight != 0
    assert all(result != known for known in declared)
    assert all(result.weight != known.weight for known in declared)


def test_zero_tokens_is_a_known_zero_weight() -> None:
    result = quota_weight(EFFICIENT_MODEL, 0, 0, 0)

    assert isinstance(result, RelativeQuotaWeight)
    assert result.weight == 0.0
    assert result.rate is MODEL_RATES[EFFICIENT_MODEL]


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1)],
)
def test_negative_token_quantity_is_refused(
    input_tokens: int, output_tokens: int
) -> None:
    with pytest.raises(ValueError, match="token quantities must be non-negative"):
        quota_weight(REFERENCE_MODEL, input_tokens, output_tokens, 0)


def test_negative_crossing_count_is_refused() -> None:
    with pytest.raises(ValueError, match="crossing count must be non-negative"):
        quota_weight(REFERENCE_MODEL, 1, 1, -1)
