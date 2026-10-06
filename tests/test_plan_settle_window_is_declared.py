"""The plan-review settle window is declared in the shipped defaults layer.

The sweep reads ``review.plan_settle_seconds`` through
``reckon/flight.py:plan_review_settle_seconds``, whose code fallback answers
600 for any config that names no key. So the fallback passes a test that only
asks the accessor. The assertions here pin the value to the shipped file —
the layer's own validation accepts it, and resolution reports the shipped
layer as its origin with the key present — so deleting the declaration
reddens this module rather than hiding behind the fallback.
"""

from __future__ import annotations

from pathlib import Path

from reckon import flight


def test_shipped_defaults_declare_the_plan_settle_window():
    path = flight.shipped_defaults_path()
    flight.validate_layer(flight.read_layer_file(path), path)

    resolved = flight.resolve(host_path=Path("/nonexistent/flight.yaml"))
    assert resolved.origin("review.plan_settle_seconds") == "shipped"
    assert resolved.config["review"]["plan_settle_seconds"] == 600
    assert flight.plan_review_settle_seconds(resolved.config) == 600


def test_shipped_defaults_declare_the_plan_change_threshold():
    path = flight.shipped_defaults_path()
    flight.validate_layer(flight.read_layer_file(path), path)

    resolved = flight.resolve(host_path=Path("/nonexistent/flight.yaml"))
    assert resolved.origin("review.plan_change_threshold") == "shipped"
    assert resolved.config["review"]["plan_change_threshold"] == 0.30
    assert flight.plan_review_change_threshold(resolved.config) == 0.30
