"""Configured token rates yield a dimensionless quota-consumption weight,
and a dated per-model rate ledger for a notional cost figure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from reckon import flight

LONG_CONTEXT_INPUT_THRESHOLD = 272_000
# The published rule charges an entire request whose input exceeds the threshold
# at twice its input rate and one-and-a-half times its output rate, rather than
# applying either multiplier only to tokens beyond the threshold.
INPUT_SURCHARGE_MULTIPLIER = 2.0
OUTPUT_SURCHARGE_MULTIPLIER = 1.5

# A declared rate older than this many days is returned with its age attached
# rather than silently priced as if it were current. Rates move on their own
# cadence; a snapshot of a moving number is the fuse this ledger exists to keep
# visible, and the horizon is where the ledger stops trusting a stale pair.
RATE_STALENESS_DAYS = 180


@dataclass(frozen=True, slots=True)
class ModelRate:
    """One model's published input and output rates per million tokens, dated.

    ``input_per_million`` and ``output_per_million`` are the declared public
    per-million prices; ``as_of`` is the date they were published. Only a
    dated rate is a price: a pair without an ``as_of`` leaves the backend
    explicitly unpriced rather than priced at an unverifiable age.
    """

    input_per_million: float
    output_per_million: float
    as_of: date


@dataclass(frozen=True, slots=True)
class BackendRateStatus:
    """One configured backend's rate standing: priced and dated, or unpriced.

    ``rate`` is ``None`` for a backend that is explicitly unpriced — no
    declared pair, no model, or a pair without an ``as_of`` date. A priced
    backend additionally carries the pair's age in whole days at the anchor
    date and whether that age exceeds the declared staleness horizon, so a
    stale price is reported with its age rather than silently relied on.
    """

    backend: str
    model_identifier: str | None
    rate: ModelRate | None = None
    age_days: int | None = None
    stale: bool = False

    @property
    def priced(self) -> bool:
        return self.rate is not None


def _as_date(value: object) -> date | None:
    """Coerce a config value to a date, or ``None`` when it is not one.

    Flight layers are parsed by ``yaml.safe_load``, so an unquoted ISO date in
    a config file arrives as a ``datetime.date`` while a quoted one arrives as
    a string. Accept both and refuse everything else — an ``as_of`` that does
    not parse cannot date a rate.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _backend_rate_statuses(
    backends: Mapping[str, object] | None,
    anchor: date,
) -> dict[str, BackendRateStatus]:
    """Classify every configured backend as priced-and-dated or explicitly unpriced.

    The whole backend map is classified, not a hand-picked subset, so a
    backend added to the resolved configuration later is covered by the same
    call — the measure ranges over whatever the merged config holds.
    """
    statuses: dict[str, BackendRateStatus] = {}
    for backend_name, raw in (backends or {}).items():
        if not isinstance(raw, Mapping):
            continue
        model_identifier = raw.get("model")
        input_rate = raw.get("input_rate_per_million")
        output_rate = raw.get("output_rate_per_million")
        published = _as_date(raw.get("as_of"))
        if (
            not model_identifier
            or input_rate is None
            or output_rate is None
            or published is None
        ):
            statuses[str(backend_name)] = BackendRateStatus(
                backend=str(backend_name),
                model_identifier=str(model_identifier) if model_identifier else None,
            )
            continue
        rate = ModelRate(
            input_per_million=float(input_rate),
            output_per_million=float(output_rate),
            as_of=published,
        )
        age_days = max(0, (anchor - published).days)
        statuses[str(backend_name)] = BackendRateStatus(
            backend=str(backend_name),
            model_identifier=str(model_identifier),
            rate=rate,
            age_days=age_days,
            stale=age_days > RATE_STALENESS_DAYS,
        )
    return statuses


def backend_rate_statuses(
    anchor: date | None = None,
) -> dict[str, BackendRateStatus]:
    """Return every resolved backend's rate standing at the anchor date.

    ``anchor`` defaults to today; callers that must stay deterministic (tests,
    snapshots) pass an explicit date. Each backend is either priced with a
    dated pair plus its age, or explicitly unpriced — a model with no declared
    rate never inherits a neighbour's, and a pair without an ``as_of`` is not a
    price at all.
    """
    effective_anchor = anchor or datetime.now(UTC).date()
    backends = flight.resolve().config.get("backends") or {}
    if not isinstance(backends, Mapping):
        return {}
    return _backend_rate_statuses(backends, effective_anchor)


def _configured_rates() -> dict[str, ModelRate]:
    """Return complete dated model-rate pairs from the resolved configuration.

    No rate is shipped by default, and a pair without an ``as_of`` date is not
    a price: until a host declares a dated pair, every model stays explicitly
    unpriced rather than inheriting a moving number that can go stale.
    """
    rates: dict[str, ModelRate] = {}
    for status in backend_rate_statuses().values():
        if not status.priced or status.model_identifier is None:
            continue
        rates[status.model_identifier] = status.rate
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
    normalising_input = max(item.input_per_million for item in rates.values())
    normalising_output = max(item.output_per_million for item in rates.values())
    weighted_tokens = surcharged_input_tokens * (
        rate.input_per_million / normalising_input
    ) + surcharged_output_tokens * (rate.output_per_million / normalising_output)
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
