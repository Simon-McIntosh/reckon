"""Plan resolution lists its directory once per directory change.

Resolving a plan by slug takes the plan's canonical path, then asks whether any
sibling in that directory claims the same identity. That second question is one
lookup into a held index of the directory, and the directory's listing is what
the index is revalidated against: a glob of the directory and a stat of every
sibling. The listing was rebuilt on each resolve, so 200 named reads paid 400
directory listings across two passes even though nothing had moved.

The directory's entry names are what now follow its own entry-set identity,
which moves exactly when an entry is added, removed or renamed. The test builds
200 plans and resolves all 200 twice, then again after one plan is added and one
renamed. The two passes pay one glob between them instead of 400, and the pass
after the change costs one further glob. Each entry is still stamped on every
resolve, exactly as the base revision stamps it, because a held stamp is a stamp
the file may no longer carry; the in-place case below holds that down by
asserting the refusal the base revision gives when a rewritten sibling claims an
already resolved slug, and that the rewrite left the directory's own identity
alone. Every resolution answers what the base revision answers.

Negative control: at the base revision the two passes glob the directory at
least 400 times.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import file_memo, metadata_index, resources

PROJECT = "sample"
PLAN_COUNT = 200


def _plan_doc(slug: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="active">
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _name(number: int) -> str:
    return f"plan-{number:03d}"


def _write_plan(docs: Path, name: str, *, slug: str | None = None) -> Path:
    """Write one plan at its canonical path and return that path."""
    path = docs / "plans" / f"{name}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_doc(slug or name), encoding="utf-8")
    return path


class _ListingMeter:
    """Count the directory listings a plan resolve takes.

    ``resources._identity_listing`` is the one place that enumerates a plans
    directory: it globs that directory and stamps every sibling. Standing in
    for it, and for the directory's own ``glob``, attributes both counts to the
    listing, and the ``file_memo.file_signature`` calls made across one listing
    are the siblings it stamped.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.globs = 0
        self.sibling_stats: list[int] = []
        self._stats = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = self
        real_glob = Path.glob
        real_signature = file_memo.file_signature
        real_listing = resources._identity_listing

        def counting_glob(self: Path, pattern: str, *args, **kwargs):
            if Path(self) == meter.directory:
                meter.globs += 1
            return real_glob(self, pattern, *args, **kwargs)

        def counting_signature(path):
            meter._stats += 1
            return real_signature(path)

        def counting_listing(directory, docs_dir, project):
            before = meter._stats
            try:
                return real_listing(directory, docs_dir, project)
            finally:
                meter.sibling_stats.append(meter._stats - before)

        monkeypatch.setattr(Path, "glob", counting_glob)
        monkeypatch.setattr(file_memo, "file_signature", counting_signature)
        monkeypatch.setattr(resources, "_identity_listing", counting_listing)

    def reset(self) -> None:
        """Start a fresh count for the pass that is about to run."""
        self.globs = 0
        self.sibling_stats.clear()


def _resolved(docs: Path, slugs) -> dict[str, resources.Resource | None]:
    return {slug: resources.resolve_resource(docs, PROJECT, slug) for slug in slugs}


def test_plan_resolution_reads_the_directory_once_per_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    paths = {
        _name(number): _write_plan(docs, _name(number)) for number in range(PLAN_COUNT)
    }
    # The base revision's answer for each file is the classifier's, which is
    # what a canonical read resolves to.
    expected = {
        name: resources.identify_resource(docs, path, PROJECT)
        for name, path in paths.items()
    }
    assert all(resource is not None for resource in expected.values()), (
        "the fixture's plans did not classify, so equality below is vacuous"
    )

    meter = _ListingMeter(docs / "plans")
    meter.install(monkeypatch)

    first = _resolved(docs, list(paths))
    second = _resolved(docs, list(reversed(list(paths))))

    # The instrument must see the listing it counts, or an absence below is an
    # absence of measurement rather than of listings.

    assert meter.sibling_stats and meter.sibling_stats[0] == PLAN_COUNT, (
        "the first listing stamped "
        f"{meter.sibling_stats[:1] or 'nothing'} siblings, not {PLAN_COUNT}"
    )
    assert meter.globs <= 1, (
        f"two passes over {PLAN_COUNT} plans globbed the directory {meter.globs} times"
    )
    # Every listing stamps the same 200 entries, and no listing is shorter than
    # the directory: the held names never stand in for a stamp, because a held
    # stamp is one the file may no longer carry (see the in-place case below).
    assert set(meter.sibling_stats) == {PLAN_COUNT}, (
        "listings stamped different numbers of siblings: "
        f"{sorted(set(meter.sibling_stats))}"
    )
    assert first == expected
    assert second == expected

    # A plan added and a plan renamed move the directory's own stamp, so the
    # next resolve lists it again.
    added = _write_plan(docs, "plan-added")
    renamed = paths[_name(42)].rename(docs / "plans" / "plan-042-renamed.html")
    changed = dict(paths)
    changed[_name(42)] = renamed
    changed["plan-added"] = added
    expected_after = {
        name: resources.identify_resource(docs, path, PROJECT)
        for name, path in changed.items()
    }

    meter.reset()
    third = _resolved(docs, list(changed))

    assert meter.globs == 1, (
        "a directory with one plan added and one renamed globbed "
        f"{meter.globs} times on the next pass"
    )
    assert third == expected_after


def test_a_sibling_rewritten_in_place_is_seen(tmp_path: Path) -> None:
    """A rewrite that claims a resolved slug is refused, as the base revision does.

    The directory's own identity cannot see this write: no entry is added,
    removed or renamed, so the held names are still right. Only the rewritten
    sibling's own stamp moves, and a listing that held that stamp would hand the
    held index the identity the file carried before the write, resolve the slug
    it now claims, and lose the refusal the base revision gives.
    """
    docs = tmp_path / "docs"
    plans = docs / "plans"
    _write_plan(docs, "alpha")
    beta = _write_plan(docs, "beta")

    # Warm the held names and the held index on an answer the rewrite must
    # invalidate.
    assert resources.resolve_resource(docs, PROJECT, "alpha") is not None
    assert resources.resolve_resource(docs, PROJECT, "beta") is not None
    before = metadata_index._dir_identity(plans)
    assert before is not None

    beta.write_text(_plan_doc("alpha"), encoding="utf-8")

    assert metadata_index._dir_identity(plans) == before, (
        "the rewrite moved the directory, so this case no longer tests a "
        "sibling's own stamp"
    )
    with pytest.raises(resources.ResourceCollision) as refused:
        resources.resolve_resource(docs, PROJECT, "alpha")

    message = str(refused.value)
    assert "duplicate resource" in message, message
    assert "plans/alpha.html" in message and "plans/beta.html" in message, message
