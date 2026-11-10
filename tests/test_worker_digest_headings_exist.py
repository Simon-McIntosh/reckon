"""Role mappings name real canonical blocks, and workers receive the fleet rule."""

import pytest

from reckon.crew import worker_digest as digest

POLICY = """# Worker digest canonical policy fixture

## User-Facing Communication

## Git Safety

### Banned Commands

### Anomaly Protocol

### Commit Discipline

### Pre-Commit Hook Policy

### No Stray Clones (binding)

### Naming & Comment Hygiene (binding)

#### Mandatory pre-stage naming check (binding)

## Parallel Agent Safety

### A Worker That Invents Its Own Manifest Status Slips The Review Guard

## Killing Processes On A Shared Login Node (binding)

## Bug and Failure Ownership

## Runtime Model Routing

## Compute Infrastructure (SDCC) — never heavy work on the login node (binding)

### The fleet node is for coordination; heavy work goes to a partition (binding, lead directive 2026-11-10)

**The fleet allocation (`~/.local/state/fleet/record.json`) is one node, 28 CPUs
and 120 GB, shared by every orchestrator session, every worker, and reckon's
station, producers and followers. It runs coordination, not compute.** Its job is
to keep orchestrators responsive. A heavy gate run in place takes cores and GPFS
bandwidth from every coordinator on the node, and once the monitor processes are
starved every session stalls together.

## IMAS Data Access

## A Worker's Process Ends With Its Turn

## Reading a Fleet Monitor Without Being Misled

## Closing A Fail-Open Guard: Measure What The Suite Was Resting On

## Test Visibility

## Development Environment (binding, all repos)

## Test Execution Protocol
"""


@pytest.fixture
def canonical_policy() -> str:
    """The pinned policy headings and fleet paragraph, independent of host state."""
    return POLICY


@pytest.mark.parametrize("role", digest.ROLES)
def test_every_mapped_heading_exists_in_the_canonical_source(
    role: str, canonical_policy: str
) -> None:
    blocks = digest.block_map(canonical_policy)
    assert "## Git Safety" in blocks
    headers = [
        digest.EXCERPTS[key].header if key in digest.EXCERPTS else key
        for key in digest.SECTION_MAP[role]
    ]

    missing = [header for header in headers if header not in blocks]
    assert not missing, f"{role}: canonical headings absent: {missing}"


@pytest.mark.parametrize("role", ["implement", "test"])
def test_committed_compute_digests_carry_the_partition_rule(
    role: str, canonical_policy: str
) -> None:
    text = digest.digest_path(role).read_text(encoding="utf-8")

    assert (
        "### The fleet node is for coordination; heavy work goes to a partition" in text
    )
    assert "### On the fleet allocation, run heavy work in place" not in text
    assert (
        digest.block_map(canonical_policy)[digest.FLEET_COMPUTE_HEADER].strip() in text
    )
