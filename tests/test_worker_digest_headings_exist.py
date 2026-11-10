"""Role mappings name real canonical blocks, and workers receive the fleet rule."""

import pytest

from reckon.crew import worker_digest as digest

CANONICAL = digest.canonical_path()
requires_canonical = pytest.mark.skipif(
    not CANONICAL.exists(),
    reason=f"{digest.CANONICAL_SOURCE} is not present on this host",
)


@requires_canonical
@pytest.mark.parametrize("role", digest.ROLES)
def test_every_mapped_heading_exists_in_the_canonical_source(role: str) -> None:
    blocks = digest.block_map(CANONICAL.read_text(encoding="utf-8"))
    assert "## Git Safety" in blocks
    headers = [
        digest.EXCERPTS[key].header if key in digest.EXCERPTS else key
        for key in digest.SECTION_MAP[role]
    ]

    missing = [header for header in headers if header not in blocks]
    assert not missing, f"{role}: canonical headings absent: {missing}"


@requires_canonical
@pytest.mark.parametrize("role", ["implement", "test"])
def test_committed_compute_digests_carry_the_partition_rule(role: str) -> None:
    text = digest.digest_path(role).read_text(encoding="utf-8")

    assert (
        "### The fleet node is for coordination; heavy work goes to a partition" in text
    )
    assert "### On the fleet allocation, run heavy work in place" not in text
    canonical = CANONICAL.read_text(encoding="utf-8")
    assert digest.block_map(canonical)[digest.FLEET_COMPUTE_HEADER].strip() in text
