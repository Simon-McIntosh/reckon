"""Published token rates yield a dimensionless weight relative to the strongest family member at one, never a dollar amount, because subscription workers are not priced per token and the rates are used only as ratios.

The subscription's per-window message allowances for the three declared members
are roughly one to two to twenty, agreeing in direction and order of magnitude
with the independent token-rate ratios.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

REFERENCE_MODEL = "gpt-5.6-sol"
MIDDLE_MODEL = "gpt-5.6-terra"
EFFICIENT_MODEL = "gpt-5.6-luna"

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


# These published values are declared data because a guessed or silently
# defaulted rate would misroute every later comparison that consumes the weight.
MODEL_RATES: Mapping[str, ModelRate] = MappingProxyType(
    {
        REFERENCE_MODEL: ModelRate(input_per_million=4.00, output_per_million=20.00),
        MIDDLE_MODEL: ModelRate(input_per_million=2.00, output_per_million=12.00),
        EFFICIENT_MODEL: ModelRate(input_per_million=0.20, output_per_million=1.20),
    }
)


@dataclass(frozen=True, slots=True)
class RelativeQuotaWeight:
    """A declared model's dimensionless rate weight and its evidence."""

    model_identifier: str
    weight: float
    rate: ModelRate
    surcharge_applied: bool


@dataclass(frozen=True, slots=True)
class UnknownQuotaWeight:
    """An explicit refusal to assign a weight to an undeclared model."""

    model_identifier: str
    weight: None = None
    rate: None = None
    surcharge_applied: bool = False


type QuotaWeightResult = RelativeQuotaWeight | UnknownQuotaWeight


def quota_weight(
    model_identifier: str,
    cumulative_input_tokens: int,
    cumulative_output_tokens: int,
    requests_over_threshold: int,
) -> QuotaWeightResult:
    """Return the declared model's relative quota weight or an explicit unknown.

    Token quantities determine the input/output mix rather than a currency-sized
    total. Each recorded threshold crossing adds the published whole-request
    surcharge increment, preserving information a cumulative token count loses.
    """
    if cumulative_input_tokens < 0 or cumulative_output_tokens < 0:
        raise ValueError("token quantities must be non-negative")
    if requests_over_threshold < 0:
        raise ValueError("crossing count must be non-negative")

    rate = MODEL_RATES.get(model_identifier)
    if rate is None:
        return UnknownQuotaWeight(
            model_identifier=model_identifier,
            surcharge_applied=requests_over_threshold > 0,
        )

    total_tokens = cumulative_input_tokens + cumulative_output_tokens
    surcharge_applied = requests_over_threshold > 0
    if total_tokens == 0:
        return RelativeQuotaWeight(
            model_identifier=model_identifier,
            weight=0.0,
            rate=rate,
            surcharge_applied=surcharge_applied,
        )

    reference_rate = MODEL_RATES[REFERENCE_MODEL]
    input_factor = 1.0 + requests_over_threshold * (INPUT_SURCHARGE_MULTIPLIER - 1.0)
    output_factor = 1.0 + requests_over_threshold * (OUTPUT_SURCHARGE_MULTIPLIER - 1.0)
    weighted_tokens = (
        cumulative_input_tokens
        * (rate.input_per_million / reference_rate.input_per_million)
        * input_factor
        + cumulative_output_tokens
        * (rate.output_per_million / reference_rate.output_per_million)
        * output_factor
    )
    return RelativeQuotaWeight(
        model_identifier=model_identifier,
        weight=weighted_tokens / total_tokens,
        rate=rate,
        surcharge_applied=surcharge_applied,
    )
