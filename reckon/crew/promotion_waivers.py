"""Records the waivers a promotion applied onto the row it lands.

A waiver is a guard's own product: the checks it waived and the reason the
operator stated. Nothing else holds it — the guard's output is consumed by the
promotion, and the live pointer carrying the run is deleted as the row is
written — so each one is copied onto the committed row before that happens.
The recording lives here so a reader asking which waivers ride a row finds
them in one place rather than spread through the promotion body.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def record_domain_waiver(
    run: dict[str, Any], domain_waiver_record: Mapping[str, Any] | None
) -> None:
    """Copy a waived domain check and its reason onto the promoted row.

    The promoted row is the durable record: the guard's waiver reaches nothing
    else once the run's pointer is gone, so a reviewer auditing a landed run
    could otherwise not see that a declared invariant failed or why. A run
    whose declared checks held has no waiver, and the key keeps the null the
    record seeding leaves rather than reading as a waiver applied for no reason.
    """
    if domain_waiver_record is None:
        return
    run["domain_waiver"] = dict(domain_waiver_record)
