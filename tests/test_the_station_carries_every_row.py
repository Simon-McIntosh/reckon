"""The station carries every row an hour of the producer's event log recorded.

The cutover question is not whether the station can classify — §6 measures that
— but whether a row the fleet produced still reaches a pane once the producers
are gone. A row is lost in four ways: the store drops it, the store files it
under the wrong audience, the sequence numbers stop ordering it across the
hour, or the subscriber delivers it twice or to the wrong session. Each of
those is measured here.

The recording is one hour of ``reckon``'s own event log, the file
``crew watch`` appends for the ``reckon`` project: every transition it wrote
between 17:00 and 18:00 UTC on 2026-10-23, plus the state each run already
held when the hour opened (the producer's ``baseline`` rows, which are
inventory rather than events). The rows are carried verbatim as the five facts
a reader can check — the instant, the run, the session, the two states and the
kind — because that is what a replayed row must still say when it lands.

Two row families appear. A *run* row names a run and is addressed to the
session that owns it. A *broadcast* row names no run — a budget lift granted or
ended is workstation-wide — and is addressed to every pane. The hour holds 86
run rows and 14 broadcast rows across six sessions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import recovery_watch, station_store, ticker

PROJECT = "reckon"
HOUR_OPENS = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)

# One recorded row per line, so a row can be read against the log it came from.
# fmt: off
_TRANSITIONS: tuple[tuple[Any, ...], ...] = (
    ('17:00:37', 'r-20261023T163506923854-a-disposition-after-promotion-reaches-the-reflex', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'dispatched', 'working', 'transition', 'a-disposition-after-promotion-reaches-the-reflex', 'reckon'),
    ('17:00:58', 'r-20261023T165823944264-review-code-e50eed172bd8', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'dispatched', 'working', 'transition', 'review-code-e50eed172bd8', 'reckon'),
    ('17:01:04', 'r-20261023T165809722735-the-cached-index-absorbs-each-change', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'dispatched', 'withdrawn', 'transition', 'the-cached-index-absorbs-each-change', 'reckon'),
    ('17:02:09', 'r-20261023T162630148384-review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'queued', 'dispatched', 'transition', 'review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'reckon'),
    ('17:02:49', 'r-20261023T165047678769-plan-review-of-the-monitor-reads-each-fact-once', '6f111d4a-cc31-4417-8974-385d904ca329', 'working', 'complete', 'transition', 'plan-review-of-the-monitor-reads-each-fact-once', 'reckon'),
    ('17:03:19', 'r-20261023T165943828944-review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'dispatched', 'queued', 'transition', 'review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'reckon'),
    ('17:03:24', 'r-20261023T162630148384-review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'dispatched', 'working', 'transition', 'review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'reckon'),
    ('17:04:58', 'r-20261023T170425114375-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'queued', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:05:34', 'r-20261023T165047678769-plan-review-of-the-monitor-reads-each-fact-once', '6f111d4a-cc31-4417-8974-385d904ca329', 'complete', 'recorded', 'transition', 'plan-review-of-the-monitor-reads-each-fact-once', 'reckon'),
    ('17:05:55', 'r-20261023T163506923854-a-disposition-after-promotion-reaches-the-reflex', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'working', 'queued', 'transition', 'a-disposition-after-promotion-reaches-the-reflex', 'reckon'),
    ('17:06:07', 'r-20261023T112727139233-a-queued-request-waits-on-its-dependencies', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'working', 'completed_unpromoted', 'transition', 'a-queued-request-waits-on-its-dependencies', 'reckon'),
    ('17:06:46', 'r-20261023T140921050545-an-ended-lift-stays-ended', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'working', 'blocked', 'transition', 'an-ended-lift-stays-ended', 'reckon'),
    ('17:06:56', 'r-20261023T140921050545-an-ended-lift-stays-ended', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'blocked', 'blocked', 'manifest-rewritten', 'an-ended-lift-stays-ended', 'reckon'),
    ('17:07:08', 'r-20261023T140921050545-an-ended-lift-stays-ended', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'blocked', 'blocked', 'manifest-rewritten', 'an-ended-lift-stays-ended', 'reckon'),
    ('17:07:41', 'r-20261023T140921050545-an-ended-lift-stays-ended', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'blocked', 'working', 'transition', 'an-ended-lift-stays-ended', 'reckon'),
    ('17:08:13', 'r-20261023T170707287094-the-station-store-is-node-local', '6f111d4a-cc31-4417-8974-385d904ca329', 'dispatched', 'queued', 'transition', 'the-station-store-is-node-local', 'reckon'),
    ('17:09:43', 'r-20261023T165823944264-review-code-e50eed172bd8', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'working', 'complete', 'transition', 'review-code-e50eed172bd8', 'reckon'),
    ('17:10:23', 'r-20261023T170425114375-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'queued', 'dispatched', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:11:10', 'r-20261023T170425114375-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'working', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:12:31', 'r-20261023T165823944264-review-code-e50eed172bd8', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'complete', 'recorded', 'transition', 'review-code-e50eed172bd8', 'reckon'),
    ('17:15:23', 'r-20261023T135622629487-a-scope-refusal-names-the-paths-it-parsed', '6f111d4a-cc31-4417-8974-385d904ca329', 'queued', 'dispatched', 'transition', 'a-scope-refusal-names-the-paths-it-parsed', 'reckon'),
    ('17:15:37', 'r-20261023T135622629487-a-scope-refusal-names-the-paths-it-parsed', '6f111d4a-cc31-4417-8974-385d904ca329', 'dispatched', 'queued', 'transition', 'a-scope-refusal-names-the-paths-it-parsed', 'reckon'),
    ('17:16:22', 'r-20261023T165943828944-review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'queued', 'dispatched', 'transition', 'review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'reckon'),
    ('17:17:11', 'r-20261023T165943828944-review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'dispatched', 'working', 'transition', 'review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'reckon'),
    ('17:17:41', 'r-20261023T154506367282-a-resumed-attempt-is-read-through-its-own-records', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'completed_unpromoted', 'complete', 'transition', 'a-resumed-attempt-is-read-through-its-own-records', 'reckon'),
    ('17:17:42', 'r-20261023T165016085496-review-of-a-resumed-attempt-is-read-through-its-own-records-0231ca112322', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'working', 'complete', 'transition', 'review-of-a-resumed-attempt-is-read-through-its-own-records-0231ca112322', 'reckon'),
    ('17:17:45', 'r-20261023T171701568985-plan-review-of-the-page-paints-from-an-index', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'dispatched', 'queued', 'transition', 'plan-review-of-the-page-paints-from-an-index', 'reckon'),
    ('17:17:51', 'r-20261023T124731718173-the-retention-sweep-runs-on-its-own', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'completed_unpromoted', 'completed_unpromoted', 'manifest-rewritten', 'the-retention-sweep-runs-on-its-own', 'reckon'),
    ('17:18:15', 'r-20261023T124731718173-the-retention-sweep-runs-on-its-own', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'completed_unpromoted', 'complete', 'transition', 'the-retention-sweep-runs-on-its-own', 'reckon'),
    ('17:18:15', 'r-20261023T162630148384-review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'working', 'complete', 'transition', 'review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'reckon'),
    ('17:18:33', 'r-20261023T170707287094-the-station-store-is-node-local', '6f111d4a-cc31-4417-8974-385d904ca329', 'queued', 'dispatched', 'transition', 'the-station-store-is-node-local', 'reckon'),
    ('17:18:43', 'r-20261023T140921050545-an-ended-lift-stays-ended', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'working', 'completed_unpromoted', 'transition', 'an-ended-lift-stays-ended', 'reckon'),
    ('17:18:43', 'r-20261023T170425114375-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'working', 'complete', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:18:53', 'r-20261023T133818950687-work-outside-scope-returns-to-the-coordinator', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'completed_unpromoted', 'complete', 'transition', 'work-outside-scope-returns-to-the-coordinator', 'reckon'),
    ('17:18:53', 'r-20261023T165438861239-review-of-work-outside-scope-returns-to-the-coordinator-36a4419069e1', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'working', 'complete', 'transition', 'review-of-work-outside-scope-returns-to-the-coordinator-36a4419069e1', 'reckon'),
    ('17:19:30', 'r-20261023T170707287094-the-station-store-is-node-local', '6f111d4a-cc31-4417-8974-385d904ca329', 'dispatched', 'working', 'transition', 'the-station-store-is-node-local', 'reckon'),
    ('17:19:51', 'r-20261023T162630148384-review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'complete', 'recorded', 'transition', 'review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'reckon'),
    ('17:20:00', 'r-20261023T165016085496-review-of-a-resumed-attempt-is-read-through-its-own-records-0231ca112322', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'complete', 'recorded', 'transition', 'review-of-a-resumed-attempt-is-read-through-its-own-records-0231ca112322', 'reckon'),
    ('17:20:00', 'r-20261023T165036277912-review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'queued', 'dispatched', 'transition', 'review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'reckon'),
    ('17:22:01', 'r-20261023T165036277912-review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'dispatched', 'working', 'transition', 'review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'reckon'),
    ('17:22:51', 'r-20261023T154506367282-a-resumed-attempt-is-read-through-its-own-records', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'complete', 'working', 'transition', 'a-resumed-attempt-is-read-through-its-own-records', 'reckon'),
    ('17:23:16', 'r-20261023T140402039862-a-coordinators-status-repair-prints-no-halted-row', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'queued', 'dispatched', 'transition', 'a-coordinators-status-repair-prints-no-halted-row', 'reckon'),
    ('17:23:26', 'r-20261023T165438861239-review-of-work-outside-scope-returns-to-the-coordinator-36a4419069e1', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'complete', 'recorded', 'transition', 'review-of-work-outside-scope-returns-to-the-coordinator-36a4419069e1', 'reckon'),
    ('17:23:58', 'r-20261023T155457612965-the-quiet-cycle-test-sees-a-cold-git-cache', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'queued', 'dispatched', 'transition', 'the-quiet-cycle-test-sees-a-cold-git-cache', 'reckon'),
    ('17:24:07', 'r-20261023T124731718173-the-retention-sweep-runs-on-its-own', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'complete', 'promoted', 'transition', 'the-retention-sweep-runs-on-its-own', 'reckon'),
    ('17:24:41', 'r-20261023T140402039862-a-coordinators-status-repair-prints-no-halted-row', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'working', 'transition', 'a-coordinators-status-repair-prints-no-halted-row', 'reckon'),
    ('17:25:22', 'r-20261023T172150946013-review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'queued', 'transition', 'review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'reckon'),
    ('17:25:38', 'r-20261023T155457612965-the-quiet-cycle-test-sees-a-cold-git-cache', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'working', 'transition', 'the-quiet-cycle-test-sees-a-cold-git-cache', 'reckon'),
    ('17:26:35', 'r-20261023T170425114375-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'complete', 'recorded', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:26:39', 'r-20261023T163347899598-a-quoted-session-id-is-not-a-session-refusal', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'dispatched', 'queued', 'transition', 'a-quoted-session-id-is-not-a-session-refusal', 'reckon'),
    ('17:28:27', 'r-20261023T163506923854-a-disposition-after-promotion-reaches-the-reflex', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'queued', 'dispatched', 'transition', 'a-disposition-after-promotion-reaches-the-reflex', 'reckon'),
    ('17:29:51', 'r-20261023T163506923854-a-disposition-after-promotion-reaches-the-reflex', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'dispatched', 'working', 'transition', 'a-disposition-after-promotion-reaches-the-reflex', 'reckon'),
    ('17:30:19', 'r-20261023T171701568985-plan-review-of-the-page-paints-from-an-index', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'queued', 'dispatched', 'transition', 'plan-review-of-the-page-paints-from-an-index', 'reckon'),
    ('17:30:43', 'r-20261023T112727139233-a-queued-request-waits-on-its-dependencies', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'completed_unpromoted', 'complete', 'transition', 'a-queued-request-waits-on-its-dependencies', 'reckon'),
    ('17:30:44', 'r-20261023T165943828944-review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'working', 'complete', 'transition', 'review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'reckon'),
    ('17:31:19', 'r-20261023T171701568985-plan-review-of-the-page-paints-from-an-index', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'dispatched', 'working', 'transition', 'plan-review-of-the-page-paints-from-an-index', 'reckon'),
    ('17:32:28', 'r-20261023T172150946013-review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'queued', 'dispatched', 'transition', 'review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'reckon'),
    ('17:33:04', 'r-20261023T135622629487-a-scope-refusal-names-the-paths-it-parsed', '6f111d4a-cc31-4417-8974-385d904ca329', 'queued', 'dispatched', 'transition', 'a-scope-refusal-names-the-paths-it-parsed', 'reckon'),
    ('17:33:37', 'r-20261023T135622629487-a-scope-refusal-names-the-paths-it-parsed', '6f111d4a-cc31-4417-8974-385d904ca329', 'dispatched', 'queued', 'transition', 'a-scope-refusal-names-the-paths-it-parsed', 'reckon'),
    ('17:33:40', 'r-20261023T172150946013-review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'working', 'transition', 'review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'reckon'),
    ('17:33:40', 'r-20261023T173108837411-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'queued', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:35:27', 'r-20261023T165036277912-review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'working', 'complete', 'transition', 'review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'reckon'),
    ('17:35:31', 'r-20261023T081540567684-a-malformed-request-is-refused-before-storage', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'completed_unpromoted', 'complete', 'transition', 'a-malformed-request-is-refused-before-storage', 'reckon'),
    ('17:35:34', 'r-20261023T165943828944-review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'complete', 'recorded', 'transition', 'review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'reckon'),
    ('17:37:03', 'r-20261023T173502566578-plan-review-of-a-queue-holds-the-whole-plan', '404513fc-8e68-4a0f-8060-29ac9ff3d5fd', 'dispatched', 'working', 'transition', 'plan-review-of-a-queue-holds-the-whole-plan', 'reckon'),
    ('17:38:41', 'r-20261023T112727139233-a-queued-request-waits-on-its-dependencies', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'complete', 'working', 'transition', 'a-queued-request-waits-on-its-dependencies', 'reckon'),
    ('17:40:09', 'r-20261023T165036277912-review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'complete', 'recorded', 'transition', 'review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'reckon'),
    ('17:40:41', 'r-20261023T163347899598-a-quoted-session-id-is-not-a-session-refusal', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'queued', 'dispatched', 'transition', 'a-quoted-session-id-is-not-a-session-refusal', 'reckon'),
    ('17:40:52', 'r-20261023T163347899598-a-quoted-session-id-is-not-a-session-refusal', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'dispatched', 'queued', 'transition', 'a-quoted-session-id-is-not-a-session-refusal', 'reckon'),
    ('17:41:11', 'r-20261023T081540567684-a-malformed-request-is-refused-before-storage', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'complete', 'complete', 'manifest-rewritten', 'a-malformed-request-is-refused-before-storage', 'reckon'),
    ('17:42:33', 'r-20261023T171701568985-plan-review-of-the-page-paints-from-an-index', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'working', 'complete', 'transition', 'plan-review-of-the-page-paints-from-an-index', 'reckon'),
    ('17:44:08', 'r-20261023T163347899598-a-quoted-session-id-is-not-a-session-refusal', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'queued', 'dispatched', 'transition', 'a-quoted-session-id-is-not-a-session-refusal', 'reckon'),
    ('17:44:32', 'r-20261023T163347899598-a-quoted-session-id-is-not-a-session-refusal', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'dispatched', 'queued', 'transition', 'a-quoted-session-id-is-not-a-session-refusal', 'reckon'),
    ('17:45:49', 'r-20261023T081540567684-a-malformed-request-is-refused-before-storage', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'complete', 'promoted', 'transition', 'a-malformed-request-is-refused-before-storage', 'reckon'),
    ('17:45:59', None, '4f554c2d-3a28-4c55-8645-67a1e8a30dea', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
    ('17:45:59', None, '3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
    ('17:46:23', None, '4f554c2d-3a28-4c55-8645-67a1e8a30dea', 'None', 'lift-granted', 'transition', 'codex-sub', 'reckon'),
    ('17:46:23', None, '3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3', 'None', 'lift-granted', 'transition', 'codex-sub', 'reckon'),
    ('17:46:33', 'r-20261023T171701568985-plan-review-of-the-page-paints-from-an-index', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'complete', 'recorded', 'transition', 'plan-review-of-the-page-paints-from-an-index', 'reckon'),
    ('17:49:50', None, '4f554c2d-3a28-4c55-8645-67a1e8a30dea', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
    ('17:49:50', None, '3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
    ('17:49:59', 'r-20261023T173502566578-plan-review-of-a-queue-holds-the-whole-plan', '404513fc-8e68-4a0f-8060-29ac9ff3d5fd', 'working', 'complete', 'transition', 'plan-review-of-a-queue-holds-the-whole-plan', 'reckon'),
    ('17:52:05', None, '4f554c2d-3a28-4c55-8645-67a1e8a30dea', 'None', 'lift-granted', 'transition', 'codex-sub', 'reckon'),
    ('17:52:05', None, '3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3', 'None', 'lift-granted', 'transition', 'codex-sub', 'reckon'),
    ('17:52:29', 'r-20261023T145257555333-a-roadmap-read-answers-from-its-snapshot', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'working', 'completed_unpromoted', 'transition', 'a-roadmap-read-answers-from-its-snapshot', 'reckon'),
    ('17:52:38', 'r-20261023T135622629487-a-scope-refusal-names-the-paths-it-parsed', '6f111d4a-cc31-4417-8974-385d904ca329', 'queued', 'dispatched', 'transition', 'a-scope-refusal-names-the-paths-it-parsed', 'reckon'),
    ('17:52:47', 'r-20261023T135622629487-a-scope-refusal-names-the-paths-it-parsed', '6f111d4a-cc31-4417-8974-385d904ca329', 'dispatched', 'queued', 'transition', 'a-scope-refusal-names-the-paths-it-parsed', 'reckon'),
    ('17:53:01', None, '4f554c2d-3a28-4c55-8645-67a1e8a30dea', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
    ('17:53:01', None, '3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
    ('17:53:42', 'r-20261023T173108837411-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'queued', 'dispatched', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:55:06', 'r-20261023T140921050545-an-ended-lift-stays-ended', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'completed_unpromoted', 'complete', 'transition', 'an-ended-lift-stays-ended', 'reckon'),
    ('17:55:06', 'r-20261023T172150946013-review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'working', 'complete', 'transition', 'review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'reckon'),
    ('17:55:09', 'r-20261023T145257555333-a-roadmap-read-answers-from-its-snapshot', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'completed_unpromoted', 'exited-unfinished', 'transition', 'a-roadmap-read-answers-from-its-snapshot', 'reckon'),
    ('17:55:11', 'r-20261023T145257555333-a-roadmap-read-answers-from-its-snapshot', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'exited-unfinished', 'working', 'transition', 'a-roadmap-read-answers-from-its-snapshot', 'reckon'),
    ('17:56:09', 'r-20261023T173108837411-plan-review-of-a-live-worker-is-never-drawn-halted', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'dispatched', 'working', 'transition', 'plan-review-of-a-live-worker-is-never-drawn-halted', 'reckon'),
    ('17:57:04', None, '4f554c2d-3a28-4c55-8645-67a1e8a30dea', 'None', 'lift-granted', 'transition', 'codex-sub', 'reckon'),
    ('17:57:04', None, '3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3', 'None', 'lift-granted', 'transition', 'codex-sub', 'reckon'),
    ('17:57:42', 'r-20261023T140402039862-a-coordinators-status-repair-prints-no-halted-row', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'working', 'waiting', 'transition', 'a-coordinators-status-repair-prints-no-halted-row', 'reckon'),
    ('17:58:15', None, '4f554c2d-3a28-4c55-8645-67a1e8a30dea', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
    ('17:58:15', None, '3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3', 'lift-in-force', 'lift-ended', 'transition', 'codex-sub', 'reckon'),
)
# fmt: on

# The state each run already held when the hour opened, recorded as the
# producer's baseline rows. Replaying without them would read a run's first
# change as its arrival, which is a row the recording does not have.
# fmt: off
_SEEDED: tuple[tuple[Any, ...], ...] = (
    ('r-20261023T162630148384-review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67', 'dispatched', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'review-of-the-retention-sweep-runs-on-its-own-f82edc7a3e67'),
    ('r-20261023T163347899598-a-quoted-session-id-is-not-a-session-refusal', 'dispatched', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'a-quoted-session-id-is-not-a-session-refusal'),
    ('r-20261023T163506923854-a-disposition-after-promotion-reaches-the-reflex', 'queued', 'd768318d-3e86-4094-bdc7-3ed5964852ba', 'a-disposition-after-promotion-reaches-the-reflex'),
    ('r-20261023T165036277912-review-of-a-malformed-request-is-refused-before-storage-0f995b9129af', 'queued', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'review-of-a-malformed-request-is-refused-before-storage-0f995b9129af'),
    ('r-20261023T165809722735-the-cached-index-absorbs-each-change', 'dispatched', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'the-cached-index-absorbs-each-change'),
    ('r-20261023T165943828944-review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f', 'dispatched', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'review-of-a-queued-request-waits-on-its-dependencies-b65536558a8f'),
    ('r-20261023T170425114375-plan-review-of-a-live-worker-is-never-drawn-halted', 'dispatched', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'plan-review-of-a-live-worker-is-never-drawn-halted'),
    ('r-20261023T170707287094-the-station-store-is-node-local', 'queued', '6f111d4a-cc31-4417-8974-385d904ca329', 'the-station-store-is-node-local'),
    ('r-20261023T171701568985-plan-review-of-the-page-paints-from-an-index', 'dispatched', '530468fb-d739-474c-9fa1-7f1ef4af1afa', 'plan-review-of-the-page-paints-from-an-index'),
    ('r-20261023T172150946013-review-of-an-ended-lift-stays-ended-1eb916cfcdc7', 'dispatched', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'review-of-an-ended-lift-stays-ended-1eb916cfcdc7'),
    ('r-20261023T173108837411-plan-review-of-a-live-worker-is-never-drawn-halted', 'dispatched', 'a337e0e6-c34c-4497-878a-bc42ce1b7cf6', 'plan-review-of-a-live-worker-is-never-drawn-halted'),
    ('r-20261023T173502566578-plan-review-of-a-queue-holds-the-whole-plan', 'dispatched', '404513fc-8e68-4a0f-8060-29ac9ff3d5fd', 'plan-review-of-a-queue-holds-the-whole-plan'),
    ('r-20261023T175236866631-a-sprint-move-writes-the-plans-sprint', 'dispatched', 'efd0ee8c-9d4e-4fa3-a440-ac05791d4758', 'a-sprint-move-writes-the-plans-sprint'),
)
# fmt: on


class _Clock:
    """A clock the case advances, so a settle window is measured not waited out."""

    def __init__(self, now: float | None = None) -> None:
        self.now = HOUR_OPENS.timestamp() if now is None else float(now)

    def __call__(self) -> float:
        return self.now


def _moment(stamp: str, offset: int = 0) -> datetime:
    hours, minutes, seconds = (int(part) for part in stamp.split(":"))
    return HOUR_OPENS.replace(hour=hours, minute=minutes, second=seconds) + timedelta(
        microseconds=offset
    )


def _recorded_rows() -> list[dict[str, Any]]:
    """The hour as the station's own row objects, one per recorded transition.

    Each row is built by ``recovery_watch._watch_transition``, the function the
    station's tick calls for every transition it publishes, from the facts the
    recording carries. Building through it rather than hand-writing the rows is
    what makes the row this case checks the row a pane would actually receive.
    """
    rows: list[dict[str, Any]] = []
    for stamp, run_id, session, previous, current, kind, node, project in _TRANSITIONS:
        snapshot: dict[str, Any] = {
            "run_id": run_id,
            "node": node,
            "session": session,
            "project": project,
            "state": current,
            "detail": "",
        }
        if kind == "manifest-rewritten":
            snapshot["manifest_rewritten"] = True
            snapshot["manifest_status"] = "complete"
            snapshot["manifest_commits"] = []
        event = recovery_watch._watch_transition(
            project,
            kind=kind,
            snapshot=snapshot,
            previous=previous,
            current=current,
            counts={"working": 0, "blocked": 0, "unpromoted": 0},
        )
        event["observed_at"] = _moment(stamp).isoformat()
        # The two fields the station's tick adds to every row before it is
        # written: which panes may receive it, and what duty it puts on them.
        event["audience"] = _audience(event)
        event["duty"] = ticker.transition_class(previous, current)
        rows.append(event)
    return rows


def _audience(row: dict[str, Any]) -> str:
    """Every pane for a broadcast, the owning session for a run row."""
    return str(row.get("session") or "*")


def _carry(store: station_store.StationStore) -> list[dict[str, Any]]:
    """Write the whole hour through the store, one row per recorded transition."""
    rows = _recorded_rows()
    for index, row in enumerate(rows):
        moment = _moment(_TRANSITIONS[index][0], index)
        store.record_tick(PROJECT, [row], {}, at=moment)
    return rows


def _recorded_keys() -> list[tuple[Any, ...]]:
    return [
        (run_id or None, previous, current, kind)
        for _stamp, run_id, _session, previous, current, kind, _node, _project in (
            _TRANSITIONS
        )
    ]


def _carried_keys(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            row.get("run_id"),
            row.get("from_state"),
            row.get("to_state"),
            row.get("event"),
        )
        for row in (json.loads(entry["payload"]) for entry in rows)
    ]


def test_every_recorded_row_is_carried_once(tmp_path: Path) -> None:
    """The hour lands whole: no row dropped, none doubled, none reworded."""
    store = station_store.StationStore(tmp_path / "runtime")
    try:
        carried = _carry(store)
        rows = store.events()
    finally:
        store.close()
    assert len(carried) == len(_TRANSITIONS) == 100
    assert len(rows) == len(_TRANSITIONS)
    assert _carried_keys(rows) == _recorded_keys()


def test_the_hour_is_carried_in_one_ordered_sequence(tmp_path: Path) -> None:
    """One counter orders the hour, so a cursor alone resumes it."""
    store = station_store.StationStore(tmp_path / "runtime")
    try:
        _carry(store)
        rows = store.events()
    finally:
        store.close()
    ids = [int(row["id"]) for row in rows]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert [row["time"] for row in rows] == sorted(row["time"] for row in rows)


def test_a_carried_row_names_the_facts_a_pane_draws(tmp_path: Path) -> None:
    """Every row carries the fields the pane's line is composed from.

    A row that reaches the store missing one of these renders a line a reader
    cannot act on: no node to send the coordinator to, no from → to to say what
    moved, no counters to size the fleet it left.
    """
    store = station_store.StationStore(tmp_path / "runtime")
    try:
        _carry(store)
        rows = store.events()
    finally:
        store.close()
    required = {
        "project",
        "event",
        "observed_at",
        "run_id",
        "node",
        "session",
        "from_state",
        "to_state",
        "working",
        "blocked",
        "unpromoted",
        "lane",
        "model",
        "time_budget",
        "detail",
    }
    for entry in rows:
        payload = json.loads(entry["payload"])
        missing = required - set(payload)
        assert not missing, (payload.get("run_id"), sorted(missing))
        assert payload["project"] == PROJECT
        assert entry["project"] == PROJECT
    delivered = [
        json.loads(entry["payload"])
        for entry in rows
        if json.loads(entry["payload"])["to_state"] == "completed_unpromoted"
    ]
    assert delivered, "the hour holds delivered-and-unpromoted rows"
    assert all(row["run_id"] for row in delivered)
    assert all(row["session"] for row in delivered)


def test_a_lift_row_reaches_the_session_it_lifts(tmp_path: Path) -> None:
    """A lift grant and its end are one row each, addressed to the lifted session.

    The lift document is workstation-wide, but the row is not: the recording
    carries one row per (lift, session) pair, so a pane reads the pane's own
    lift and no other session's.
    """
    store = station_store.StationStore(tmp_path / "runtime")
    try:
        _carry(store)
        rows = store.events()
        lifts = [entry for entry in rows if not json.loads(entry["payload"])["run_id"]]
        per_session: dict[str, list[str]] = {}
        for entry in lifts:
            payload = json.loads(entry["payload"])
            per_session.setdefault(entry["audience"], []).append(payload["to_state"])
    finally:
        store.close()
    assert len(lifts) == 14
    assert set(per_session) == {
        "4f554c2d-3a28-4c55-8645-67a1e8a30dea",
        "3338dfd2-fb2f-4aa6-bf1b-d4cec24ea1d3",
    }
    for states in per_session.values():
        assert len(states) == 7
        assert set(states) == {"lift-granted", "lift-ended"}
    assert {json.loads(entry["payload"])["node"] for entry in lifts} == {"codex-sub"}


def test_a_pane_reads_its_own_session_and_nothing_else(tmp_path: Path) -> None:
    """Scoping holds: a pane is offered its session's rows and no other session's."""
    mine = "d768318d-3e86-4094-bdc7-3ed5964852ba"
    store = station_store.StationStore(tmp_path / "runtime")
    try:
        _carry(store)
        offered = store.events_for(project=PROJECT, audiences=(mine, "*"), after_id=0)
    finally:
        store.close()
    assert {entry["audience"] for entry in offered} == {mine}
    payloads = [json.loads(entry["payload"]) for entry in offered]
    assert {row["session"] for row in payloads if row["run_id"]} <= {mine, ""}
    expected = {
        run_id
        for _stamp, run_id, session, _p, _c, _k, _n, _pr in _TRANSITIONS
        if session == mine
    }
    assert expected and {row["run_id"] for row in payloads if row["run_id"]} == expected


def test_the_lift_row_sweep_belongs_to_the_stream_producer(tmp_path: Path) -> None:
    """The broadcast rows are generated by the producer seat, and nowhere else.

    This is the surface §12's retirement has to move: ``_sweep_lift_rows``
    reads the workstation's lift document only for a project registered as this
    process's stream producer, so a station that owns the node emits no lift
    row at all. The store carries the row and every pane receives it once
    (measured above); what is missing is a station-side call, which is why the
    seat is still arming.
    """
    from reckon.crew import runs

    assert PROJECT not in runs._WATCH_STREAM_PRODUCERS
    memory = {"a-lift": {"state": "granted"}}
    assert recovery_watch._sweep_lift_rows(PROJECT, memory) is memory
    assert recovery_watch._sweep_reset_rows(PROJECT, {"seen"}) == {"seen"}
