"""The follower's in-place reloader: swap a stale image at a record boundary.

A follower watches a stream and must pick up new code without dropping the rows
it has not yet delivered. The reloader proves a replacement image loads, then
re-executes this process into it at a complete stream-record boundary, carrying
its reader position, its lifetime deadline, its owner and -- for a producer --
an open watch-seat descriptor across the exec. ``_StampPoll`` drives the check
for a process that owns no stream loop of its own.

The shared line printers, environment names and probe-retry constants this
family reads stay owned by ``reckon.crew_follow_commands``, which re-exports
this module's classes. They are fetched from there at call time rather than
imported here, because that module imports this one to re-export it, and
because a caller that patches one of those names does so on that module.
"""

import json
import os
import random
import re
import shlex
import signal
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon import checkout_trust


def image_reexec_argv(image_root: Path) -> list[str]:
    """Run the current crew command with its package imported from an image."""
    launcher = (
        "import sys; "
        f"sys.path.insert(0, {str(image_root)!r}); "
        "from reckon.cli import main; main()"
    )
    return [sys.executable, "-c", launcher, *sys.argv[1:]]


def image_reexec_environment(
    source_root: Path,
    commit: str | None,
    pin: int | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Carry an image's source, identity and lease through one in-place exec."""
    from reckon.crew_follow_commands import (
        _FOLLOWER_IMAGE_COMMIT_ENV,
        _FOLLOWER_IMAGE_PIN_ENV,
        _FOLLOWER_SOURCE_ROOT_ENV,
        _SHIM_PYTHON_ENV,
    )

    # The carried identity belongs to the replacement, not children launched
    # before exec. An image has no project virtualenv, so its shims also need
    # the interpreter this process already uses.
    carried = dict(os.environ if environment is None else environment)
    if commit is not None:
        carried[_FOLLOWER_SOURCE_ROOT_ENV] = str(source_root)
        carried[_FOLLOWER_IMAGE_COMMIT_ENV] = commit
        if pin is not None:
            os.set_inheritable(pin, True)
            carried[_FOLLOWER_IMAGE_PIN_ENV] = str(pin)
        carried.setdefault(_SHIM_PYTHON_ENV, sys.executable)
    return carried


def _probe_retry_jitter(gap: float) -> float:
    """The random part of the wait before a timed-out probe is retried.

    Drawn fresh on every timeout, so two followers that timed out together do
    not re-probe together: they share the gap's nominal length but not the
    instant it is measured from.
    """
    from reckon.crew_follow_commands import (
        FOLLOWER_RELOAD_PROBE_RETRY_JITTER_FRACTION,
    )

    return random.uniform(  # noqa: S311 - scheduling jitter, not a secret
        0.0, gap * FOLLOWER_RELOAD_PROBE_RETRY_JITTER_FRACTION
    )


class _FollowerReloader:
    """Replace a stale follower only at a complete stream-record boundary."""

    def __init__(
        self,
        project: str,
        registration,
        *,
        stream=None,
        deadline: float | None = None,
        owner: tuple[int, str] | None = None,
        color: bool = False,
        label: str = "crew follow",
        seat: bool = False,
    ) -> None:
        from reckon.crew import runs
        from reckon.crew_follow_commands import (
            _FOLLOWER_IMAGE_COMMIT_ENV,
            _FOLLOWER_IMAGE_PIN_ENV,
            _FOLLOWER_SOURCE_ROOT_ENV,
            FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS,
            _import_root,
        )

        self.project = project
        self.registration = registration
        self.stream = stream
        self.color = color
        # The command this reloader speaks for, so its deferral and failure
        # lines name the process a reader must cycle rather than always the
        # follower's.
        self.label = label
        # A producer holds the project's watch seat rather than a session
        # registration, so its replacement must be handed the seat descriptor
        # rather than the reader checkpoint.
        self.seat = seat
        # The instant this arming ends, handed to the replacement only through
        # the environment ``os.execve`` passes: it never enters this image's
        # ``os.environ``, so no child started here inherits it.
        self.deadline = deadline
        # The process that armed this follower, fixed before the reload. It is
        # carried to the replacement beside the deadline, so the replacement
        # keeps the owner the original arming had rather than adopting whatever
        # process replaced the image.
        self.owner = owner
        self.import_root = _import_root(runs)
        self.image_pin: int | None = None
        carried_pin = os.environ.pop(_FOLLOWER_IMAGE_PIN_ENV, None)
        if carried_pin is not None:
            try:
                candidate_pin = int(carried_pin)
                if checkout_trust.pin_matches_materialized_tree(
                    candidate_pin, self.import_root
                ):
                    os.set_inheritable(candidate_pin, False)
                    self.image_pin = candidate_pin
            except (OSError, ValueError):
                self.image_pin = None
        if self.image_pin is None and checkout_trust.published_reload_image(
            self.import_root
        ):
            self.image_pin = checkout_trust.pin_materialized_tree(self.import_root)
        self.replacement_pin: int | None = None
        self.source_root = Path(
            os.environ.get(_FOLLOWER_SOURCE_ROOT_ENV, str(self.import_root))
        ).resolve()
        self.code_stamp = os.environ.get(_FOLLOWER_IMAGE_COMMIT_ENV) or (
            runs.follower_code_stamp()
        )
        self.replacement_root = self.import_root
        self.checked_at: float | None = None
        self.failed = False
        # The stamp whose replacement was proven unimportable and therefore not
        # performed. Kept so the check is not repeated and the deferred line is
        # not reprinted on every tick; a reload retries only once the code
        # changes again.
        self.deferred_stamp: str | None = None
        # Whether a reload is presently held because a tracked file differs from
        # HEAD. Unlike an import failure the deferral cannot be keyed on the
        # stamp: committing the edit cleans the tree without changing a byte, so
        # the stamp a dirty deferral was taken at is the stamp a clean tree still
        # reports. The flag keeps the tree re-read on each cadence until it is
        # clean, and the printed line is owed once per contiguous dirtiness.
        self.dirty_deferral = False
        # The instant before which a probe that ran out of its bound is not
        # repeated, and the gap the next timeout will earn. Held with the stamp
        # they were taken at, so a code stamp that moves starts the gap afresh
        # rather than inheriting a backoff the previous checkout earned.
        self.probe_retry_at: float | None = None
        self.probe_retry_gap = FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS
        # How far the nominal retry instant is brought forward for this timeout,
        # so two followers that timed out together do not re-probe together.
        self.probe_retry_jitter = 0.0
        self.probe_retry_stamp: str | None = None
        # Whether a timeout's deferral line has already been said for the
        # present contiguous run of timeouts, so a slow node says it once
        # rather than on every cadence.
        self.timeout_deferral = False

    def _clear_probe_retry(self) -> None:
        """Forget a timeout's backoff: the code moved, or a probe answered."""
        from reckon.crew_follow_commands import (
            FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS,
        )

        self.probe_retry_at = None
        self.probe_retry_gap = FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS
        self.probe_retry_jitter = 0.0
        self.probe_retry_stamp = None
        self.timeout_deferral = False

    def _replacement_imports(self) -> tuple[bool, str]:
        """Prove the replacement image loads before re-executing into it.

        The proof is the shared one in ``reckon.checkout_trust``: it runs every
        tracked module in a throwaway interpreter against the same import root
        the exec leads with, because this process is the wrong place to discover
        that the new code does not load -- a failed import here would take the
        live pane down with it. Returns whether the image is safe and, when it
        is not, one line naming why.
        """
        return checkout_trust.reckon_imports(self.replacement_root)

    def _echo_deferred_dirty(self, detail: str) -> None:
        """Say once that a moved code stamp is held back by an unclean tree."""
        from reckon.crew_follow_commands import _dim_history_line, _echo_follow_line

        line = (
            f"reckon {self.label} deferred its reload: tracked files are "
            f"modified against HEAD ({detail}); keeping the current image, "
            "retrying when the tree is clean"
        )
        if self.color:
            line = _dim_history_line(line)
        _echo_follow_line(line, stream=self.stream)

    def _record_reload_deferral(self, reason: str) -> None:
        """Keep a producer's reload refusal beside its live seat identity."""
        if not self.seat:
            return
        from reckon.crew import runs

        handle = runs._WATCH_SEAT_HANDLES.get(self.project)
        if handle is None:
            return
        record = runs._read_watch_record(handle)
        deferral = {
            "reason": reason,
            "paths": sorted(set(re.findall(r"reckon/[A-Za-z0-9_./-]+", reason))),
        }
        if record.get("reload_deferral") != deferral:
            record["reload_deferral"] = deferral
            runs._write_watch_record(handle, record)

    def _record_reload_prune(
        self, removed: Iterable[Path], kept: Iterable[Path]
    ) -> None:
        """Keep the last prune on the seat's record rather than in the ticker.

        A reload prunes its own cache as housekeeping on every move of HEAD, so
        a ticker line per tree per reload repeats for as long as a lease lives
        and asks no reader to do anything. The seat's record holds the last
        prune instead: when it ran, how many trees went, and which trees a lease
        kept, which is what a diagnosis reads without relaying a stream.
        """
        if not self.seat:
            return
        from reckon.crew import runs

        handle = runs._WATCH_SEAT_HANDLES.get(self.project)
        if handle is None:
            return
        record = runs._read_watch_record(handle)
        record["reload_prune"] = {
            "when": runs._utc_now(),
            "removed": len(list(removed)),
            "kept": [str(path) for path in kept],
        }
        runs._write_watch_record(handle, record)

    def _clear_reload_deferral(self) -> None:
        """Clear the refusal as this producer begins its replacement."""
        if not self.seat:
            return
        from reckon.crew import runs

        handle = runs._WATCH_SEAT_HANDLES.get(self.project)
        if handle is None:
            return
        record = runs._read_watch_record(handle)
        if "reload_deferral" in record:
            record.pop("reload_deferral")
            runs._write_watch_record(handle, record)

    def _materialize_replacement(
        self, commit: str
    ) -> tuple[Path, list[Path], list[Path], int] | None:
        """Publish this arming's replacement image, retrying one raced prune.

        A second materialiser on this host or another sorts the published
        images, keeps the newest few and removes the rest, so the tree this one
        has just published but not yet leased can be removed underneath it.
        Re-publishing once recovers a single such loss. A target that vanishes
        again is a prune that is not a race, so the reload defers naming the
        vanished path rather than spending the cadence's budget on it.
        """
        from reckon.crew_follow_commands import _echo_follow_line

        failure: OSError | RuntimeError | None = None
        for _ in range(2):
            try:
                return checkout_trust.materialize_committed_tree(
                    self.source_root, commit, active_root=self.import_root
                )
            except (OSError, RuntimeError) as exc:
                failure = exc
        self._record_reload_deferral(f"could not materialize HEAD ({failure})")
        _echo_follow_line(
            f"reckon {self.label} deferred its reload: could not "
            f"materialize HEAD ({failure})",
            stream=self.stream,
        )
        return None

    def poll(self, checkpoint: Mapping[str, Any]) -> None:
        """Check at a bounded cadence and re-exec with the reader checkpoint."""
        from reckon.crew import runs
        from reckon.crew.dispatch import _export_launched_workers_for_reexec
        from reckon.crew_follow_commands import (
            _FOLLOWER_CHECKPOINT_ENV,
            _FOLLOWER_LIFETIME_ENV,
            FOLLOWER_RELOAD_PROBE_RETRY_CAP_SECONDS,
            FOLLOWER_RELOAD_PROOF_PENDING_SECONDS,
            _dim_history_line,
            _echo_follow_line,
        )

        if self.image_pin is not None:
            checkout_trust.refresh_materialized_tree_pin(
                self.image_pin, self.import_root
            )
        if self.failed:
            return
        moment = time.monotonic()
        if (
            self.checked_at is not None
            and moment - self.checked_at < runs.FOLLOWER_FRESHNESS_SECONDS
        ):
            return
        self.checked_at = moment
        if self.replacement_pin is not None:
            os.close(self.replacement_pin)
            self.replacement_pin = None
        committed_image = bool(re.fullmatch(r"[0-9a-f]{40}", self.code_stamp))
        # The identity names the content of the reckon/ package at HEAD, not the
        # commit that carries it: a commit changing no file under reckon/ moves
        # nothing here, so no reload is owed and no reload tree is extracted for
        # it. A tree id is a 40-digit git object, which is what a reload tree is
        # named by; a content stamp is 64 and is handled by the dirty-tree path.
        current_stamp = runs.follower_code_stamp()
        if current_stamp == checkout_trust.IDENTITY_UNKNOWN:
            # A read the identity depends on did not answer, so this tick holds
            # no new identity to act on: keep the current image and the recorded
            # stamp rather than materialising a tree from a partial read, and let
            # the next tick re-read.
            return
        current_head = (
            current_stamp
            if committed_image and re.fullmatch(r"[0-9a-f]{40}", current_stamp)
            else None
        )
        if current_stamp == self.code_stamp:
            # No code has moved, so there is nothing to reload onto: a tree left
            # dirty without a stamp change is not a reload this arming owes.
            self.dirty_deferral = False
            self._clear_probe_retry()
            return
        if current_stamp == self.deferred_stamp and not self.dirty_deferral:
            return
        if (
            self.probe_retry_stamp is not None
            and self.probe_retry_stamp != current_stamp
        ):
            # The code moved, so a gap earned by the checkout that timed out no
            # longer describes the one now on disk: this checkout starts afresh.
            self._clear_probe_retry()
        if (
            self.probe_retry_at is not None
            and moment < self.probe_retry_at - self.probe_retry_jitter
        ):
            # A probe on this stamp timed out or is pending elsewhere, and the
            # wait it earned has not passed. Leave the image alone until then:
            # materialising it again re-pins, re-stamps and re-ranks a tree on
            # the shared filesystem every cadence for nothing. The jitter only
            # ever brings the wait forward, so the nominal gap stays the longest
            # a checkout waits.
            return
        if current_head and current_stamp == current_head:
            materialized = self._materialize_replacement(current_head)
            if materialized is None:
                return
            self.replacement_root, removed, kept, self.replacement_pin = materialized
            self._record_reload_prune(removed, kept)
        else:
            # A non-commit stamp belongs to an older or non-Git image. Keep its
            # existing trust check until it can reload from a source checkout.
            dirty, detail = checkout_trust.tracked_reckon_tree_is_dirty(
                self.import_root
            )
            if dirty:
                self._record_reload_deferral(
                    f"tracked files modified against HEAD ({detail})"
                )
                if not self.dirty_deferral:
                    self.dirty_deferral = True
                    self._echo_deferred_dirty(detail)
                return
            self.replacement_root = self.import_root
        self.dirty_deferral = False
        reloadable, reason = self._replacement_imports()
        if not reloadable:
            if isinstance(reason, checkout_trust.ImportProofPending):
                # Another process is proving this image and nothing has failed:
                # read its verdict after a short jittered wait, saying nothing
                # and leaving the timeout backoff and the seat record untouched.
                self.probe_retry_stamp = current_stamp
                self.probe_retry_at = moment + FOLLOWER_RELOAD_PROOF_PENDING_SECONDS
                self.probe_retry_jitter = _probe_retry_jitter(
                    FOLLOWER_RELOAD_PROOF_PENDING_SECONDS
                )
                return
            if isinstance(reason, checkout_trust.ImportProbeTimedOut):
                # A probe that ran out of its bound says nothing about the code:
                # the checkout may load perfectly and merely have been slow under
                # load. So the reload is retried rather than held until the code
                # moves -- ``deferred_stamp`` is left unset -- but the retry waits
                # out a gap that doubles on each further timeout, so a sustained
                # spike is re-probed a handful of times rather than every cadence.
                if self.probe_retry_stamp != current_stamp:
                    # A checkout whose gap has not started yet: owe its deferral
                    # line afresh rather than inheriting the last one's.
                    self.timeout_deferral = False
                    self.probe_retry_stamp = current_stamp
                self.probe_retry_at = moment + self.probe_retry_gap
                self.probe_retry_jitter = _probe_retry_jitter(self.probe_retry_gap)
                self.probe_retry_gap = min(
                    self.probe_retry_gap * 2, FOLLOWER_RELOAD_PROBE_RETRY_CAP_SECONDS
                )
                self._record_reload_deferral(reason)
                if not self.timeout_deferral:
                    self.timeout_deferral = True
                    line = (
                        f"reckon {self.label} deferred its reload: {reason}; "
                        "keeping the current image, probing again after "
                        f"{self.probe_retry_at - moment:g} s"
                    )
                    if self.color:
                        line = _dim_history_line(line)
                    _echo_follow_line(line, stream=self.stream)
                return
            self._clear_probe_retry()
            self._record_reload_deferral(f"new image does not import ({reason})")
            self.deferred_stamp = current_stamp
            line = (
                f"reckon {self.label} deferred its reload: the new image does not "
                f"import ({reason}); keeping the current image, retrying when the "
                "code changes"
            )
            if self.color:
                line = _dim_history_line(line)
            _echo_follow_line(line, stream=self.stream)
            return

        # The probe answered: whatever gap a timeout last earned, and the
        # deferral line it owed, belong to that run of timeouts, which is over.
        self._clear_probe_retry()
        # The launched-worker registry does not survive an image replacement,
        # though the process (and therefore the parent-child relationships)
        # does. Hand the outstanding pids over beside the checkpoint so the
        # replacement image continues to own the workers it launched.
        _export_launched_workers_for_reexec()
        if self.registration is not None:
            self.registration.prepare_reexec()
        # The replacement receives the same image identity and launcher as
        # every other crew process that changes code without changing pid.
        exec_environment = image_reexec_environment(
            self.source_root,
            current_head if current_head == current_stamp else None,
            self.replacement_pin,
        )
        if not self.seat:
            # The reader's place reaches the replacement image through the
            # mapping this exec passes and never through ``os.environ``: every
            # child this image starts inherits the process environment, and a
            # checkpoint describes one reader's place in one stream rather than
            # anything a child could use.
            exec_environment[_FOLLOWER_CHECKPOINT_ENV] = json.dumps(
                {"project": self.project, "checkpoint": dict(checkpoint)}
            )
        if self.deadline is not None:
            exec_environment[_FOLLOWER_LIFETIME_ENV] = repr(self.deadline)
        if self.owner is not None:
            exec_environment[runs._FOLLOWER_OWNER_ENV] = runs._format_follower_owner(
                self.owner
            )
        if self.seat:
            # The producer's seat is an open advisory lock, which the image
            # replacement would otherwise close and release; naming the held
            # descriptor in the replacement's environment keeps the seat held
            # across the exec rather than re-entered against a race.
            self._clear_reload_deferral()
            seat_fd = runs.prepare_watch_seat_reexec(self.project)
            if seat_fd is not None:
                exec_environment[runs._WATCH_SEAT_ENV] = str(seat_fd)
                _echo_follow_line(
                    "reckon crew watch started its reload; awaiting replacement",
                    stream=self.stream,
                )
        # Ignoring the alarm before disarming drops one that became pending while
        # the import proof ran. The ignored disposition crosses exec until the
        # replacement installs its handler; a caught disposition would reset to
        # the fatal default there.
        previous_alarm = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            os.execve(  # noqa: S606 - replacement preserves descriptors and stdout
                sys.executable,
                image_reexec_argv(self.replacement_root),
                exec_environment,
            )
        except OSError as exc:
            signal.signal(signal.SIGALRM, previous_alarm)
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            if self.seat:
                runs.cancel_watch_seat_reexec(self.project)
            if self.registration is not None:
                self.registration.cancel_reexec()
            self.failed = True
            command = shlex.join(["reckon", *sys.argv[1:]])
            _echo_follow_line(
                f"reckon {self.label} is stale and could not reload itself "
                f"({exc}); cycle it with: {command}",
                stream=self.stream,
            )
        finally:
            if self.replacement_pin is not None:
                os.close(self.replacement_pin)
                self.replacement_pin = None


class _StampPoll:
    """Poll the producer's code stamp on the follower's cadence.

    A follower reaches its reload check through the per-wait hook its own
    stream loop runs. The producer does not own that loop — ``watch_follow``
    sleeps inside a generator it does not hand ticks to — so it needs its own
    cadence, and a signal timer is the one that keeps the check in the main
    thread: an image replacement is well defined only from the thread that
    performs it, and ``os.execve`` from a helper thread would race the main
    thread's own stream writes.
    """

    def __init__(self, reloader: _FollowerReloader, interval: float) -> None:
        self.reloader = reloader
        self.interval = interval
        self.previous = None

    def _tick(self, signum, frame) -> None:
        # The import proof may outlast the interval. Disarm before entering it so
        # the signal handler cannot recursively start another proof forever.
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            self.reloader.poll({})
        finally:
            if not self.reloader.failed:
                signal.setitimer(signal.ITIMER_REAL, self.interval, self.interval)

    def start(self) -> None:
        self.previous = signal.signal(signal.SIGALRM, self._tick)
        signal.setitimer(signal.ITIMER_REAL, self.interval, self.interval)

    def stop(self) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self.previous is not None:
            signal.signal(signal.SIGALRM, self.previous)
            self.previous = None
