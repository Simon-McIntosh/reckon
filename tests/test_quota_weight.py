from __future__ import annotations

from collections.abc import Sequence

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
    RequestTokenUsage,
    UnknownQuotaWeight,
    quota_weight,
)


def _expected_totals(
    requests: Sequence[RequestTokenUsage],
) -> tuple[int, int, float, float, int]:
    input_tokens = sum(request.input_tokens for request in requests)
    output_tokens = sum(request.output_tokens for request in requests)
    requests_over_threshold = sum(
        request.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD for request in requests
    )
    surcharged_input_tokens = sum(
        request.input_tokens
        * (
            INPUT_SURCHARGE_MULTIPLIER
            if request.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD
            else 1.0
        )
        for request in requests
    )
    surcharged_output_tokens = sum(
        request.output_tokens
        * (
            OUTPUT_SURCHARGE_MULTIPLIER
            if request.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD
            else 1.0
        )
        for request in requests
    )
    return (
        input_tokens,
        output_tokens,
        surcharged_input_tokens,
        surcharged_output_tokens,
        requests_over_threshold,
    )


def _expected_weight(
    model: str,
    requests: Sequence[RequestTokenUsage],
    *,
    apply_surcharge: bool = True,
) -> float:
    (
        input_tokens,
        output_tokens,
        surcharged_input_tokens,
        surcharged_output_tokens,
        _,
    ) = _expected_totals(requests)
    rate = MODEL_RATES[model]
    reference_rate = MODEL_RATES[REFERENCE_MODEL]
    weighted_input = surcharged_input_tokens if apply_surcharge else input_tokens
    weighted_output = surcharged_output_tokens if apply_surcharge else output_tokens
    return weighted_input * (
        rate.input_per_million / reference_rate.input_per_million
    ) + weighted_output * (rate.output_per_million / reference_rate.output_per_million)


def test_equal_quantities_follow_the_declared_rate_ratios() -> None:
    quantity = 12_345
    requests = (RequestTokenUsage(input_tokens=quantity, output_tokens=quantity),)

    results = {
        model: quota_weight(model, requests)
        for model in (REFERENCE_MODEL, MIDDLE_MODEL, EFFICIENT_MODEL)
    }

    for model, result in results.items():
        assert isinstance(result, RelativeQuotaWeight)
        assert result.weight == pytest.approx(_expected_weight(model, requests))
        assert result.rate is MODEL_RATES[model]
        assert result.surcharge_applied is False

    reference_rate = MODEL_RATES[REFERENCE_MODEL]
    reference_input_ratio = (
        reference_rate.input_per_million / reference_rate.input_per_million
    )
    reference_output_ratio = (
        reference_rate.output_per_million / reference_rate.output_per_million
    )
    assert results[REFERENCE_MODEL].weight == pytest.approx(
        quantity * reference_input_ratio + quantity * reference_output_ratio
    )
    assert results[REFERENCE_MODEL].weight != reference_input_ratio


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(1, 1), (91, 7), (4, 113)],
)
def test_declared_family_ordering_is_strict(
    input_tokens: int, output_tokens: int
) -> None:
    requests = (
        RequestTokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    reference = quota_weight(REFERENCE_MODEL, requests)
    middle = quota_weight(MIDDLE_MODEL, requests)
    efficient = quota_weight(EFFICIENT_MODEL, requests)

    assert isinstance(reference, RelativeQuotaWeight)
    assert isinstance(middle, RelativeQuotaWeight)
    assert isinstance(efficient, RelativeQuotaWeight)
    assert efficient.weight < middle.weight < reference.weight


def test_surcharge_and_totals_are_attributed_to_each_crossing_request() -> None:
    requests = (
        RequestTokenUsage(
            input_tokens=LONG_CONTEXT_INPUT_THRESHOLD + 1,
            output_tokens=27_000,
        ),
        RequestTokenUsage(input_tokens=73_000, output_tokens=4_000),
    )
    result = quota_weight(MIDDLE_MODEL, requests)
    expected = _expected_totals(requests)

    assert isinstance(result, RelativeQuotaWeight)
    assert result.weight == pytest.approx(_expected_weight(MIDDLE_MODEL, requests))
    assert result.input_tokens == expected[0]
    assert result.output_tokens == expected[1]
    assert result.surcharged_input_tokens == pytest.approx(expected[2])
    assert result.surcharged_output_tokens == pytest.approx(expected[3])
    assert result.requests_over_threshold == expected[4]
    assert result.surcharge_applied is True


def test_larger_uncrossed_run_outweighs_two_crossing_requests() -> None:
    many_request_count = 64
    many_total_input = 16_000_000
    many_small_requests = tuple(
        RequestTokenUsage(
            input_tokens=many_total_input // many_request_count,
            output_tokens=0,
        )
        for _ in range(many_request_count)
    )
    crossing_request_count = 2
    crossing_total_input = 600_000
    two_large_requests = tuple(
        RequestTokenUsage(
            input_tokens=crossing_total_input // crossing_request_count,
            output_tokens=0,
        )
        for _ in range(crossing_request_count)
    )

    many_small = quota_weight(REFERENCE_MODEL, many_small_requests)
    two_large = quota_weight(REFERENCE_MODEL, two_large_requests)

    assert isinstance(many_small, RelativeQuotaWeight)
    assert isinstance(two_large, RelativeQuotaWeight)
    assert many_small.requests_over_threshold == sum(
        request.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD
        for request in many_small_requests
    )
    assert two_large.requests_over_threshold == sum(
        request.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD
        for request in two_large_requests
    )
    assert many_small.weight == pytest.approx(
        _expected_weight(REFERENCE_MODEL, many_small_requests)
    )
    assert two_large.weight == pytest.approx(
        _expected_weight(REFERENCE_MODEL, two_large_requests)
    )
    assert many_small.weight > two_large.weight


def test_weight_scales_with_the_number_of_identical_requests() -> None:
    request_mix = (
        RequestTokenUsage(input_tokens=81_000, output_tokens=9_000),
        RequestTokenUsage(
            input_tokens=LONG_CONTEXT_INPUT_THRESHOLD + 1,
            output_tokens=17_000,
        ),
    )
    scale = 10
    scaled_mix = request_mix * scale
    base = quota_weight(MIDDLE_MODEL, request_mix)
    scaled = quota_weight(MIDDLE_MODEL, scaled_mix)
    expected_ratio = len(scaled_mix) / len(request_mix)

    assert isinstance(base, RelativeQuotaWeight)
    assert isinstance(scaled, RelativeQuotaWeight)
    assert base.weight == pytest.approx(_expected_weight(MIDDLE_MODEL, request_mix))
    assert scaled.weight == pytest.approx(_expected_weight(MIDDLE_MODEL, scaled_mix))
    assert scaled.weight / base.weight == pytest.approx(expected_ratio)


def test_one_crossing_request_lies_between_none_and_both_crossing() -> None:
    output_tokens = LONG_CONTEXT_INPUT_THRESHOLD // 10
    neither = (
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD, output_tokens),
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD, output_tokens),
    )
    one = (
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD + 1, output_tokens),
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD, output_tokens),
    )
    both = (
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD + 1, output_tokens),
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD + 1, output_tokens),
    )

    results = [
        quota_weight(REFERENCE_MODEL, requests) for requests in (neither, one, both)
    ]

    assert all(isinstance(result, RelativeQuotaWeight) for result in results)
    assert [result.weight for result in results] == pytest.approx(
        [
            _expected_weight(REFERENCE_MODEL, requests)
            for requests in (neither, one, both)
        ]
    )
    assert results[0].weight < results[1].weight < results[2].weight


def test_surcharge_delta_tracks_tokens_inside_the_crossing_request() -> None:
    small_crossing_request = RequestTokenUsage(
        LONG_CONTEXT_INPUT_THRESHOLD + 1,
        LONG_CONTEXT_INPUT_THRESHOLD // 20,
    )
    small_fraction_run = (
        small_crossing_request,
        *(RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD, 1) for _ in range(20)),
    )
    large_crossing_request = RequestTokenUsage(
        LONG_CONTEXT_INPUT_THRESHOLD * 4,
        LONG_CONTEXT_INPUT_THRESHOLD,
    )
    large_fraction_run = (
        large_crossing_request,
        RequestTokenUsage(1, 1),
    )

    small_fraction = quota_weight(REFERENCE_MODEL, small_fraction_run)
    large_fraction = quota_weight(REFERENCE_MODEL, large_fraction_run)

    assert isinstance(small_fraction, RelativeQuotaWeight)
    assert isinstance(large_fraction, RelativeQuotaWeight)
    small_delta = small_fraction.weight - _expected_weight(
        REFERENCE_MODEL, small_fraction_run, apply_surcharge=False
    )
    large_delta = large_fraction.weight - _expected_weight(
        REFERENCE_MODEL, large_fraction_run, apply_surcharge=False
    )
    assert small_delta == pytest.approx(
        _expected_weight(REFERENCE_MODEL, small_fraction_run)
        - _expected_weight(REFERENCE_MODEL, small_fraction_run, apply_surcharge=False)
    )
    assert large_delta == pytest.approx(
        _expected_weight(REFERENCE_MODEL, large_fraction_run)
        - _expected_weight(REFERENCE_MODEL, large_fraction_run, apply_surcharge=False)
    )
    assert small_delta < large_delta


def test_undeclared_model_returns_an_explicit_unknown_for_consumption() -> None:
    undeclared_model = "gpt-5.3-codex-spark"
    requests = (RequestTokenUsage(input_tokens=100, output_tokens=20),)
    result = quota_weight(undeclared_model, requests)
    declared = [
        quota_weight(model, requests)
        for model in (REFERENCE_MODEL, MIDDLE_MODEL, EFFICIENT_MODEL)
    ]

    assert isinstance(result, UnknownQuotaWeight)
    assert result.model_identifier == undeclared_model
    assert result.weight is None
    assert result.rate is None
    assert result.weight != sum(request.input_tokens for request in requests)
    assert all(result != known for known in declared)
    assert all(result.weight != known.weight for known in declared)


def test_empty_request_sequence_is_a_known_zero_weight() -> None:
    requests: tuple[RequestTokenUsage, ...] = ()
    result = quota_weight(EFFICIENT_MODEL, requests)
    expected_zero = float(sum(request.input_tokens for request in requests))

    assert isinstance(result, RelativeQuotaWeight)
    assert result.weight == expected_zero
    assert result.rate is MODEL_RATES[EFFICIENT_MODEL]
    assert result.requests_over_threshold == sum(
        request.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD for request in requests
    )


def test_zero_tokens_for_a_declared_model_is_a_known_zero_weight() -> None:
    requests = (RequestTokenUsage(input_tokens=0, output_tokens=0),)
    result = quota_weight(EFFICIENT_MODEL, requests)

    assert isinstance(result, RelativeQuotaWeight)
    assert result.weight == pytest.approx(_expected_weight(EFFICIENT_MODEL, requests))
    assert result.rate is MODEL_RATES[EFFICIENT_MODEL]


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1)],
)
def test_negative_token_quantity_is_refused(
    input_tokens: int, output_tokens: int
) -> None:
    requests = (
        RequestTokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    with pytest.raises(ValueError, match="token quantities must be non-negative"):
        quota_weight(REFERENCE_MODEL, requests)


def test_threshold_boundary_crosses_only_when_strictly_greater() -> None:
    output_tokens = LONG_CONTEXT_INPUT_THRESHOLD // 10
    at_threshold_requests = (
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD, output_tokens),
    )
    above_threshold_requests = (
        RequestTokenUsage(LONG_CONTEXT_INPUT_THRESHOLD + 1, output_tokens),
    )
    at_threshold = quota_weight(REFERENCE_MODEL, at_threshold_requests)
    above_threshold = quota_weight(REFERENCE_MODEL, above_threshold_requests)
    at_expected = _expected_totals(at_threshold_requests)
    above_expected = _expected_totals(above_threshold_requests)

    assert isinstance(at_threshold, RelativeQuotaWeight)
    assert isinstance(above_threshold, RelativeQuotaWeight)
    assert at_threshold.requests_over_threshold == at_expected[4]
    assert above_threshold.requests_over_threshold == above_expected[4]
    assert at_threshold.surcharged_input_tokens == pytest.approx(at_expected[2])
    assert above_threshold.surcharged_input_tokens == pytest.approx(above_expected[2])
    assert at_threshold.weight == pytest.approx(
        _expected_weight(REFERENCE_MODEL, at_threshold_requests)
    )
    assert above_threshold.weight == pytest.approx(
        _expected_weight(REFERENCE_MODEL, above_threshold_requests)
    )
