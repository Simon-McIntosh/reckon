"""The shared lease arbitrates separate hosts without a local process probe."""

from __future__ import annotations

import json
import time

from reckon.crew.host_lease import (
    LEASE_RENEW_SECONDS,
    LEASE_STALE_SECONDS,
    HostLease,
)


def test_fresh_holder_blocks_takeover_and_renewal_extends_it(tmp_path):
    now = [time.time()]

    def clock():
        return now[0]

    first = HostLease(tmp_path, "ingest", "host-one", 101, "job-one", clock=clock)
    second = HostLease(tmp_path, "ingest", "host-two", 202, "job-two", clock=clock)

    assert first.claim()
    record = json.loads(first.path.read_text(encoding="utf-8"))
    assert (record["host"], record["pid"], record["job"]) == (
        "host-one",
        101,
        "job-one",
    )
    assert second.holder() == first.owner
    assert not second.claim()
    now[0] += LEASE_RENEW_SECONDS
    assert first.renew()
    now[0] += LEASE_STALE_SECONDS - LEASE_RENEW_SECONDS - 1
    assert second.holder() == first.owner
    assert not second.claim()


def test_stale_holder_is_replaced_and_cannot_release_successor(tmp_path):
    now = [time.time()]

    def clock():
        return now[0]

    first = HostLease(tmp_path, "publish", "host-one", 101, "job-one", clock=clock)
    second = HostLease(tmp_path, "publish", "host-two", 202, "job-two", clock=clock)

    assert first.claim()
    now[0] += LEASE_STALE_SECONDS + 1
    assert first.holder() is None
    assert second.claim()
    assert second.holder() == second.owner
    assert not first.renew()
    assert not first.release()
    assert second.path.exists()
    assert second.release()
    assert not second.path.exists()
