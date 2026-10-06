"""The plan-review settle window is retired from the flight layer.

The window gated the plan sweep, which no longer exists, so the layer declares
no such key. A layer spelling it is refused as an undeclared key while the
change threshold beside it still resolves, so a stale brake is surfaced rather
than silently ignored.

The retired key's name is assembled from its parts rather than written whole,
so the name the layer no longer carries appears nowhere in the tree to be
mistaken for a live reference.
"""

from __future__ import annotations

import pytest

from reckon import flight

RETIRED_KEY = "plan_" + "settle_seconds"


def test_a_layer_declaring_the_settle_window_is_refused():
    with pytest.raises(flight.FlightConfigError, match=RETIRED_KEY):
        flight.validate_layer({"review": {RETIRED_KEY: 600}}, "test")


def test_the_change_threshold_beside_it_still_resolves():
    config = {"review": {"plan_change_threshold": 0.2}}
    flight.validate_layer(config, "test")
    assert flight.plan_review_change_threshold(config) == 0.2
