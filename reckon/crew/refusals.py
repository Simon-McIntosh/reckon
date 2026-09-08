"""Render crew refusals with the command that resolves them.

The string keys are stable census data emitted by the refusal-visibility test.
They identify operator-facing refusal families without leaking into callable
names. Keeping the remedies here gives every emitting path one formatter and
gives the command boundary one registry to extend when it adds a refusal.
"""

from __future__ import annotations

from collections.abc import Mapping

NO_CREW_VERB_SENTENCE = (
    "No Reckon crew verb exists; resolve the cited condition outside the crew "
    "command surface."
)

DISPATCH_REFUSAL_REMEDIES: Mapping[str, str | None] = {
    "D01": "`reckon crew preflight` after correcting the cited configuration or ledger condition",
    "D02": (
        "`reckon crew preflight`, then `reckon crew dispatch`; for an existing "
        "blocked run, use `reckon crew resume-ready`"
    ),
    "D03": (
        "`reckon crew dispatch --local` after configuring the local backend, "
        "or `reckon crew dispatch` without `--local`"
    ),
    "D04": "`reckon crew dispatch --backend <available-backend>`",
    "D05": (
        "`reckon crew dispatch` or `reckon crew shadow` after committing or "
        "registering the plan"
    ),
    "D06": (
        "the same `reckon crew preflight`, `reckon crew dispatch`, or `reckon "
        "crew shadow` after correcting the cited flight key"
    ),
    "D07": ("`reckon crew dispatch` after repairing the named node-contract property"),
    "D08": "`reckon crew dispatch` once for each smaller node",
    "D09": (
        "`reckon crew observe` and `reckon crew complete` on an occupying run, "
        "then `reckon crew dispatch`"
    ),
    "D10": (
        "`reckon crew preflight`, then `reckon crew dispatch --backend "
        "<clear-backend>` or `reckon crew resume-ready`"
    ),
    "D11": (
        "each run's reported `reckon crew` next action, or `reckon crew dispatch "
        "--allow-unreconciled-runs`"
    ),
    "D12": (
        "`reckon crew observe` and the owner's reported reconciliation command, "
        "then `reckon crew dispatch`"
    ),
    "D13": (
        "`reckon crew watch` and `reckon crew follow`, or `reckon crew dispatch "
        "--no-watch`"
    ),
    "D14": "`reckon crew member add`, then `reckon crew dispatch`",
    "D15": (
        "`reckon crew member list`, then `reckon crew dispatch` with a matching "
        "member and backend"
    ),
    "D16": (
        "`reckon crew observe` and reconcile the owning run, or `reckon crew "
        "dispatch --member <free-member>`"
    ),
    "D17": ("`reckon crew dispatch` or `reckon crew shadow` after reinstalling Reckon"),
    "D18": (
        "`reckon crew dispatch --no-watch`, or repair the arming environment and "
        "retry `reckon crew dispatch`"
    ),
    "D19": (
        "`reckon crew dispatch --no-watch`, or restore the executable and retry "
        "`reckon crew dispatch`"
    ),
    "D20": (
        "`reckon crew ledger --view records`, then `reckon crew shadow` with a "
        "valid primary run"
    ),
    "D21": (
        "`reckon crew shadow --backend <candidate>` after correcting the flight "
        "override"
    ),
    "D22": (
        "`reckon crew dispatch` or `reckon crew shadow` after correcting the "
        "backend launch; use `reckon crew attach` only for an in-harness directive"
    ),
}


def format_refusal(family_id: str, detail: str) -> str:
    """Append the registered resolving action to one refusal, idempotently."""
    try:
        remedy = DISPATCH_REFUSAL_REMEDIES[family_id]
    except KeyError as exc:
        raise ValueError(f"unknown crew refusal family {family_id!r}") from exc

    rendered_detail = str(detail).strip()
    if remedy is None:
        if NO_CREW_VERB_SENTENCE in rendered_detail:
            return rendered_detail
        return " ".join(
            part for part in (rendered_detail, NO_CREW_VERB_SENTENCE) if part
        )

    sentence = f"Resolve with {remedy}."
    if sentence in rendered_detail:
        return rendered_detail
    return " ".join(part for part in (rendered_detail, sentence) if part)
