"""The admit service's review sweep reaches every project and survives a failure."""

from __future__ import annotations

from reckon.crew.admission.review_sweep import ReviewSweep


def test_one_sweep_asks_every_live_project_and_reports_what_it_dispatched() -> None:
    asked: list[str] = []

    def sweep(project: str) -> dict:
        asked.append(project)
        return {"reviews_dispatched": [f"review-of-{project}"]}

    reviews = ReviewSweep(projects=lambda: ["alpha", "beta"], sweep=sweep)

    assert reviews.sweep_once() == ["review-of-alpha", "review-of-beta"]
    assert asked == ["alpha", "beta"]
    assert reviews.passes == 1


def test_one_project_that_raises_does_not_end_the_sweep() -> None:
    def sweep(project: str) -> dict:
        if project == "alpha":
            raise RuntimeError("unreadable project")
        return {"reviews_dispatched": ["review-of-beta"]}

    reviews = ReviewSweep(projects=lambda: ["alpha", "beta"], sweep=sweep)

    assert reviews.sweep_once() == ["review-of-beta"]


def test_a_stopped_sweep_dispatches_nothing_further() -> None:
    asked: list[str] = []
    reviews = ReviewSweep(
        projects=lambda: ["alpha", "beta"],
        sweep=lambda project: asked.append(project) or {},
    )

    assert reviews.stop(timeout=1.0)
    assert reviews.sweep_once() == []
    assert asked == []


def test_a_sweep_ends_once_the_code_it_runs_has_moved() -> None:
    stamps = iter(["one", "two", "two", "two"])
    asked: list[str] = []
    reviews = ReviewSweep(
        projects=lambda: ["alpha"],
        sweep=lambda project: asked.append(project) or {},
        interval=0.01,
        code_stamp=lambda: next(stamps, "two"),
    )

    reviews.start()
    assert reviews.stop(timeout=5.0)
    assert asked == []


def test_a_running_sweep_repeats_until_it_is_stopped() -> None:
    import threading

    seen = threading.Event()
    counts = {"alpha": 0}

    def sweep(project: str) -> dict:
        counts[project] += 1
        if counts[project] >= 2:
            seen.set()
        return {}

    reviews = ReviewSweep(projects=lambda: ["alpha"], sweep=sweep, interval=0.01)
    reviews.start()
    try:
        assert seen.wait(5.0)
    finally:
        assert reviews.stop(timeout=5.0)
