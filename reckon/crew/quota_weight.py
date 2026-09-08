"""Configured token rates yield a dimensionless quota-consumption weight."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from reckon import flight

LONG_CONTEXT_INPUT_THRESHOLD = 272_000
# The published rule charges an entire request whose input exceeds the threshold
# at twice its input rate and one-and-a-half times its output rate, rather than
# applying either multiplier only to tokens beyond the threshold.
INPUT_SURCHARGE_MULTIPLIER = 2.0
OUTPUT_SURCHARGE_MULTIPLIER = 1.5


@dataclass(frozen=True, slots=True)
class ModelRate:
    """One model's published input and output rates per million tokens."""

    input_per_million: float
    output_per_million: float


def _configured_rates() -> dict[str, ModelRate]:
    """Return complete model-rate pairs from the resolved flight configuration.

    No rate is shipped by default: until a host declares one, every model stays
    explicitly unpriced rather than inheriting a moving number that can go stale.
    """
    rates: dict[str, ModelRate] = {}
    backends = flight.resolve().config.get("backends") or {}
    if not isinstance(backends, Mapping):
        return rates
    for backend in backends.values():
        if not isinstance(backend, Mapping):
            continue
        model_identifier = backend.get("model")
        input_rate = backend.get("input_rate_per_million")
        output_rate = backend.get("output_rate_per_million")
        if not model_identifier or input_rate is None or output_rate is None:
            continue
        rates[str(model_identifier)] = ModelRate(
            input_per_million=float(input_rate),
            output_per_million=float(output_rate),
        )
    return rates


@dataclass(frozen=True, slots=True)
class RequestTokenUsage:
    """Input and output token quantities recorded for one request."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class RelativeQuotaWeight:
    """A known dimensionless quota weight and its request-level evidence."""

    model_identifier: str
    weight: float
    rate: ModelRate
    requests_over_threshold: int
    input_tokens: int
    output_tokens: int
    surcharged_input_tokens: float
    surcharged_output_tokens: float
    surcharge_applied: bool


@dataclass(frozen=True, slots=True)
class UnknownQuotaWeight:
    """An explicit refusal to assign a weight to an undeclared model."""

    model_identifier: str
    weight: None = None
    rate: None = None
    requests_over_threshold: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    surcharged_input_tokens: float = 0.0
    surcharged_output_tokens: float = 0.0
    surcharge_applied: bool = False


type QuotaWeightResult = RelativeQuotaWeight | UnknownQuotaWeight


def quota_weight(
    model_identifier: str,
    requests: Sequence[RequestTokenUsage],
) -> QuotaWeightResult:
    """Return the declared model's relative quota weight or an explicit unknown.

    Callers obtain the sequence from the metered client's per-session receipt
    file, whose records carry each request's input and output tokens. This
    module accepts only those plain quantities: it reads no receipt itself.

    A request whose input is strictly above the long-context threshold receives
    both published whole-request multipliers. The returned input and output
    totals expose the surcharge separately from the unsurcharged consumption.
    """
    input_tokens = 0
    output_tokens = 0
    surcharged_input_tokens = 0.0
    surcharged_output_tokens = 0.0
    requests_over_threshold = 0
    for request in requests:
        if request.input_tokens < 0 or request.output_tokens < 0:
            raise ValueError("token quantities must be non-negative")
        input_tokens += request.input_tokens
        output_tokens += request.output_tokens
        if request.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD:
            requests_over_threshold += 1
            surcharged_input_tokens += request.input_tokens * INPUT_SURCHARGE_MULTIPLIER
            surcharged_output_tokens += (
                request.output_tokens * OUTPUT_SURCHARGE_MULTIPLIER
            )
        else:
            surcharged_input_tokens += request.input_tokens
            surcharged_output_tokens += request.output_tokens

    surcharge_applied = requests_over_threshold > 0
    rates = _configured_rates()
    rate = rates.get(model_identifier)
    if rate is None:
        return UnknownQuotaWeight(
            model_identifier=model_identifier,
            requests_over_threshold=requests_over_threshold,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            surcharged_input_tokens=surcharged_input_tokens,
            surcharged_output_tokens=surcharged_output_tokens,
            surcharge_applied=surcharge_applied,
        )

    if not requests:
        return RelativeQuotaWeight(
            model_identifier=model_identifier,
            weight=0.0,
            rate=rate,
            requests_over_threshold=0,
            input_tokens=0,
            output_tokens=0,
            surcharged_input_tokens=0.0,
            surcharged_output_tokens=0.0,
            surcharge_applied=False,
        )

    # Each component is normalised by the largest rate configuration declares.
    # The reference cannot be a model name: model identities are operator data,
    # and source must remain neutral when configured backends change.
    normalising_rate = ModelRate(
        input_per_million=max(item.input_per_million for item in rates.values()),
        output_per_million=max(item.output_per_million for item in rates.values()),
    )
    weighted_tokens = surcharged_input_tokens * (
        rate.input_per_million / normalising_rate.input_per_million
    ) + surcharged_output_tokens * (
        rate.output_per_million / normalising_rate.output_per_million
    )
    return RelativeQuotaWeight(
        model_identifier=model_identifier,
        weight=weighted_tokens,
        rate=rate,
        requests_over_threshold=requests_over_threshold,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        surcharged_input_tokens=surcharged_input_tokens,
        surcharged_output_tokens=surcharged_output_tokens,
        surcharge_applied=surcharge_applied,
    )
