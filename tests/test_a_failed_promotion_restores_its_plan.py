"""A failed landing preserves the plan's bytes and its own index entry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew import promotion, routing
from reckon.crew.node import CrewError, TaskNode
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "plan-rollback-fixture"
PLAN = "promotion-target"
RUN_ID = "r-plan-rollback"
PLAN_PATH = f"docs/plans/{PLAN}.html"


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    plan = root / PLAN_PATH
    plan.parent.mkdir(parents=True)
    plan.write_text(
        _plan_html.write_state(
            '<!doctype html><html><head></head><body><main class="plan-doc">'
            '<h2 id="delivery">Delivery</h2></main></body></html>\n',
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Promotion target",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "--", PLAN_PATH),
        ("commit", "-q", "-m", "test: seed committed plan"),
    ):
        git(root, *args)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-11-16T08:00:00Z",
            "node": {
                "id": "delivery",
                "plan": PLAN,
                "section": "delivery",
                "write_paths": [],
                "time_budget": "20m",
            },
        },
    )
    return root


def _dispatch_visibility(repository: Path) -> str:
    return routing.require_plan_section_visible(
        node=TaskNode(
            id="next-delivery", goal="Continue delivery", plan=PLAN, section="delivery"
        ),
        project=PROJECT,
        repo=repository,
        base="HEAD",
        authority=routing.resolve_dispatch_authority(PROJECT, repository),
    )


def _plan_snapshot(repository: Path) -> tuple:
    path = repository / PLAN_PATH
    _state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        version,
        path.stat().st_mode,
        git(repository, "ls-files", "--stage", "--", PLAN_PATH),
        git(repository, "status", "--porcelain", "--", PLAN_PATH),
        git(repository, "rev-parse", "HEAD"),
    )


def _pending_state(repository: Path, state: str) -> None:
    path = repository / PLAN_PATH
    if state == "clean":
        return
    text = path.read_text()
    path.write_text(
        text.replace(
            'name="plan-version" content="0"', 'name="plan-version" content="1"'
        )
    )
    if state in {"staged", "split"}:
        git(repository, "add", "--", PLAN_PATH)
    if state == "split":
        path.write_text(
            text.replace(
                'name="plan-version" content="0"', 'name="plan-version" content="2"'
            )
        )
    assert _plan_snapshot(repository)[1] > 0


def _promote(repository: Path) -> dict:
    return crew.complete(
        RUN_ID, gate="passed", outcome="Delivered the change", root=repository
    )


def _force_failure(repository: Path, monkeypatch: pytest.MonkeyPatch, boundary: str):
    before = (repository / PLAN_PATH).read_bytes()

    def refuse(*args, **kwargs):
        assert (repository / PLAN_PATH).read_bytes() != before
        assert "Delivered the change" in (repository / PLAN_PATH).read_text()
        raise CrewError(f"injected {boundary} refusal after plan comment")

    if boundary == "profile":
        monkeypatch.setattr(promotion, "_profile_fold_anchor", refuse)
    elif boundary == "store":

        def refuse_store(*args, **kwargs):
            refuse()

        monkeypatch.setattr(ledger, "append_run", refuse_store)
    elif boundary == "commit":

        def refuse_commit(*args, **kwargs):
            git(repository, "add", "--", PLAN_PATH)
            refuse()

        monkeypatch.setattr(promotion, "_commit_landing_writes", refuse_commit)
    elif boundary == "stop":

        def stop(stage):
            if stage == "the promotion commit":
                assert (repository / PLAN_PATH).read_bytes() != before
                raise promotion._PromotionStopped("injected stop after plan comment")

        monkeypatch.setattr(promotion, "_stop_at_stage_boundary", stop)


@pytest.mark.parametrize("boundary", ["profile", "store", "commit", "stop"])
@pytest.mark.parametrize("state", ["clean", "unstaged", "staged", "split"])
def test_failed_promotion_restores_the_plan_and_its_index(
    repository: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, state: str
) -> None:
    _pending_state(repository, state)
    unrelated = repository / "unrelated.txt"
    unrelated.write_text("pending peer edit\n")
    git(repository, "add", "--", "unrelated.txt")
    unrelated_index = git(repository, "ls-files", "--stage", "--", "unrelated.txt")
    before = _plan_snapshot(repository)
    _force_failure(repository, monkeypatch, boundary)

    with pytest.raises((CrewError, promotion._PromotionStopped)) as error:
        _promote(repository)

    assert _plan_snapshot(repository) == before
    assert "restored" in str(error.value)
    assert "index" in str(error.value)
    assert pointer_path(RUN_ID).exists()
    assert (
        git(repository, "ls-files", "--stage", "--", "unrelated.txt") == unrelated_index
    )


def test_dispatch_validates_the_same_plan_after_a_failed_promotion(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _dispatch_visibility(repository)
    _force_failure(repository, monkeypatch, "profile")
    with pytest.raises(CrewError, match="injected profile refusal"):
        _promote(repository)
    # Try dispatch before comparing bytes, so the refusal itself is measured.
    assert _dispatch_visibility(repository) == head


def test_successful_promotion_commits_the_comment(repository: Path) -> None:
    before = _plan_snapshot(repository)
    result = _promote(repository)
    after = _plan_snapshot(repository)
    assert result["plan_comment"]["recorded"] is True
    assert "Delivered the change" in git(repository, "show", f"HEAD:{PLAN_PATH}")
    assert after[1] == before[1] + 1
    assert after[4] == ""
    assert _dispatch_visibility(repository) == after[5]


def test_cleanup_failure_preserves_the_committed_comment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args, **kwargs):
        raise CrewError("injected cleanup refusal")

    monkeypatch.setattr(promotion, "capture_run_session", refuse)
    with pytest.raises(CrewError, match="injected cleanup refusal"):
        _promote(repository)
    assert "Delivered the change" in git(repository, "show", f"HEAD:{PLAN_PATH}")
    assert git(repository, "status", "--porcelain", "--", PLAN_PATH) == ""


@pytest.mark.parametrize("edit", ["state", "text", "comment", "staged", "index_only"])
@pytest.mark.parametrize("boundary", ["profile", "store", "commit"])
def test_a_refusal_preserves_an_unserialized_same_plan_edit(
    repository: Path, monkeypatch: pytest.MonkeyPatch, edit: str, boundary: str
) -> None:
    path = repository / PLAN_PATH
    before = _plan_snapshot(repository)
    peer_snapshot = []

    # A writer that bypasses the plan lock must still fail the ownership check.
    def edit_then_refuse(*args, **kwargs):
        assert "Delivered the change" in path.read_text()
        if edit in {"state", "staged"}:
            state, version = _store.read_plan(
                PROJECT, PLAN, repository, artifact_type="plan"
            )
            _store._write_state_locked(
                PROJECT,
                PLAN,
                {**state, "summary": "Peer edit remains"},
                version,
                repository,
                artifact_type="plan",
            )
        elif edit == "text":
            path.write_text(
                path.read_text().replace(
                    '<h2 id="delivery">Delivery</h2>',
                    '<h2 id="delivery">Peer delivery</h2>',
                )
            )
        elif edit == "comment":
            path.write_text(
                path.read_text().replace("Delivered the change", "Peer correction")
            )
        else:
            working = path.read_bytes()
            path.write_bytes(working.replace(b"Delivery</h2>", b"Peer delivery</h2>"))
            git(repository, "add", "--", PLAN_PATH)
            path.write_bytes(working)
        if edit == "staged":
            git(repository, "add", "--", PLAN_PATH)
        peer_snapshot.append(_plan_snapshot(repository))
        assert peer_snapshot[-1] != before
        if boundary == "store":
            raise ledger.LedgerError("injected refusal after peer edit")
        raise CrewError("injected refusal after peer edit")

    if boundary == "store":
        monkeypatch.setattr(ledger, "append_run", edit_then_refuse)
    elif boundary == "commit":

        def before_commit(stage):
            if stage == "the promotion commit":
                edit_then_refuse()

        monkeypatch.setattr(promotion, "_stop_at_stage_boundary", before_commit)
    else:
        monkeypatch.setattr(promotion, "_profile_fold_anchor", edit_then_refuse)
    with pytest.raises((CrewError, ledger.LedgerError)) as error:
        _promote(repository)
    assert peer_snapshot
    assert _plan_snapshot(repository) == peer_snapshot[-1]
    assert "preserv" in str(error.value).lower()
    assert "restored plan document" not in str(error.value)


def test_a_refusal_preserves_an_edit_that_precedes_the_comment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_comment = promotion._record_landing_comment
    peer_snapshot = []

    def edit_then_comment(*args, **kwargs):
        state, version = _store.read_plan(
            PROJECT, PLAN, repository, artifact_type="plan"
        )
        _store._write_state_locked(
            PROJECT,
            PLAN,
            {**state, "summary": "Peer edit remains"},
            version,
            repository,
            artifact_type="plan",
        )
        return write_comment(*args, **kwargs)

    def refuse(*args, **kwargs):
        peer_snapshot.append(_plan_snapshot(repository))
        raise CrewError("injected refusal after interleaved edit and comment")

    monkeypatch.setattr(promotion, "_record_landing_comment", edit_then_comment)
    monkeypatch.setattr(promotion, "_profile_fold_anchor", refuse)
    with pytest.raises(CrewError):
        _promote(repository)
    assert _plan_snapshot(repository) == peer_snapshot[0]


def test_an_editor_waiting_on_the_commit_keeps_its_plan_and_staging(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import threading

    from reckon.crew import promotion_scope

    path = repository / PLAN_PATH
    start = threading.Event()
    attempted = threading.Event()
    finished = threading.Event()
    errors = []

    def editor():
        try:
            assert start.wait(5)
            attempted.set()
            with _store._serialized_path_lock(path, "plans"):
                path.write_text(
                    path.read_text().replace(
                        '<h2 id="delivery">Delivery</h2>',
                        '<h2 id="delivery">Peer delivery</h2>',
                    )
                )
                git(repository, "add", "--", PLAN_PATH)
        except (
            AssertionError,
            CrewError,
            OSError,
            subprocess.CalledProcessError,
        ) as error:
            errors.append(error)
        finally:
            finished.set()

    def refuse_staging(*args, **kwargs):
        start.set()
        assert attempted.wait(5)
        finished.wait(0.2)
        return subprocess.CompletedProcess(
            ["git", "add"], 128, "", "injected staging refusal"
        )

    monkeypatch.setattr(promotion_scope, "_stage_landing_writes", refuse_staging)
    worker = threading.Thread(target=editor)
    worker.start()
    try:
        with pytest.raises(CrewError, match="injected staging refusal"):
            _promote(repository)
    finally:
        start.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert "Peer delivery" in path.read_text()
    assert "Peer delivery" in git(repository, "show", f":{PLAN_PATH}")


def test_a_merged_peer_comment_survives_a_failed_commit(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from reckon.crew import promotion_scope

    path = repository / PLAN_PATH
    write_comment = promotion._record_landing_comment
    merged_bytes = []

    def peer_then_comment(*args, **kwargs):
        state, version = _store.read_plan(
            PROJECT, PLAN, repository, artifact_type="plan"
        )
        comments = {key: list(entries) for key, entries in state["comments"].items()}
        comments.setdefault("delivery", []).append(
            {
                "id": "c-peer-append",
                "who": "peer",
                "when": "2026-11-17T00:00:00Z",
                "body": "<p>A peer's independently delivered comment</p>",
            }
        )
        _store._write_state_locked(
            PROJECT,
            PLAN,
            {**state, "comments": comments},
            version,
            repository,
            artifact_type="plan",
        )
        result = write_comment(*args, **kwargs)
        merged_bytes.append(path.read_bytes())
        return result

    monkeypatch.setattr(promotion, "_record_landing_comment", peer_then_comment)
    monkeypatch.setattr(
        promotion_scope,
        "_stage_landing_writes",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["git", "add"], 128, "", "injected staging refusal"
        ),
    )
    with pytest.raises(CrewError, match="injected staging refusal"):
        crew.complete(
            RUN_ID, gate="passed", outcome="Delivered the run's change", root=repository
        )
    assert path.read_bytes() == merged_bytes[0]
    assert "A peer's independently delivered comment" in path.read_text()
    assert git(repository, "diff", "--cached", "--name-only", "--", PLAN_PATH) == ""


def test_snapshot_uses_the_comment_owners_transformation(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon.crew import promotion_scope

    before = _plan_snapshot(repository)
    monkeypatch.setattr(promotion_scope, "RUN_COMMENT_PREFIX", "c-delivered-")

    def refuse(*args, **kwargs):
        state, _version = _store.read_plan(
            PROJECT, PLAN, repository, artifact_type="plan"
        )
        assert state["comments"]["delivery"][0]["id"] == f"c-delivered-{RUN_ID}"
        raise CrewError("injected refusal with the owner's comment identity")

    monkeypatch.setattr(promotion, "_profile_fold_anchor", refuse)
    with pytest.raises(CrewError, match="restored plan document"):
        _promote(repository)
    assert _plan_snapshot(repository) == before


@pytest.mark.parametrize("boundary", ["before_comment", "profile", "store", "commit"])
def test_the_plan_lock_covers_the_snapshot_through_restoration(
    repository: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import subprocess
    import threading

    path = repository / PLAN_PATH
    before = path.read_bytes()
    start = threading.Event()
    attempted = threading.Event()
    finished = threading.Event()
    observed = []
    errors = []

    def editor():
        try:
            assert start.wait(5)
            attempted.set()
            with _store._serialized_path_lock(path, "plans"):
                observed.append(path.read_bytes())
                path.write_bytes(
                    observed[-1].replace(b"Delivery</h2>", b"Peer delivery</h2>")
                )
                git(repository, "add", "--", PLAN_PATH)
        except (
            AssertionError,
            CrewError,
            OSError,
            subprocess.CalledProcessError,
        ) as error:
            errors.append(error)
        finally:
            finished.set()

    def refuse(*args, **kwargs):
        start.set()
        assert attempted.wait(5)
        assert not finished.wait(0.2), "editor entered the snapshot transaction"
        if boundary == "store":
            raise ledger.LedgerError("injected serialized promotion refusal")
        raise CrewError("injected serialized promotion refusal")

    if boundary == "before_comment":
        monkeypatch.setattr(promotion, "_record_landing_comment", refuse)
    elif boundary == "profile":
        monkeypatch.setattr(promotion, "_profile_fold_anchor", refuse)
    elif boundary == "store":
        monkeypatch.setattr(ledger, "append_run", refuse)
    else:

        def stop(stage):
            if stage == "the promotion commit":
                refuse()

        monkeypatch.setattr(promotion, "_stop_at_stage_boundary", stop)
    worker = threading.Thread(target=editor)
    worker.start()
    try:
        with pytest.raises(
            (CrewError, ledger.LedgerError), match="restored plan document"
        ):
            _promote(repository)
    finally:
        start.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert observed == [before]
    assert "Peer delivery" in path.read_text()
    assert "Peer delivery" in git(repository, "show", f":{PLAN_PATH}")


def test_a_copied_context_cannot_lend_its_plan_lock_to_another_thread(
    repository: Path,
) -> None:
    import subprocess
    import threading
    from contextvars import copy_context

    path = repository / PLAN_PATH
    attempted = threading.Event()
    acquired = threading.Event()
    errors = []

    def editor():
        try:
            attempted.set()
            with _store._serialized_path_lock(path, "plans"):
                acquired.set()
                path.write_bytes(
                    path.read_bytes().replace(b"Delivery</h2>", b"Peer delivery</h2>")
                )
                git(repository, "add", "--", PLAN_PATH)
        except (
            AssertionError,
            CrewError,
            OSError,
            subprocess.CalledProcessError,
        ) as error:
            errors.append(error)

    worker = None
    try:
        with _store._serialized_path_lock(path, "plans"):
            worker = threading.Thread(target=copy_context().run, args=(editor,))
            worker.start()
            assert attempted.wait(5)
            assert not acquired.wait(0.2), (
                "a copied context borrowed another thread's lock"
            )
    finally:
        if worker is not None:
            worker.join(10)
    assert not worker.is_alive()
    assert not errors
    assert acquired.is_set()
    assert "Peer delivery" in path.read_text()
    assert "Peer delivery" in git(repository, "show", f":{PLAN_PATH}")


def test_a_copied_context_cannot_revive_a_released_plan_lock(
    repository: Path,
) -> None:
    import subprocess
    import threading
    from contextvars import copy_context

    path = repository / PLAN_PATH
    with _store._serialized_path_lock(path, "plans"):
        stale = copy_context()
    holding = threading.Event()
    release = threading.Event()
    written = threading.Event()
    errors = []

    def editor():
        try:
            with _store._serialized_path_lock(path, "plans"):
                holding.set()
                assert release.wait(5)
                path.write_bytes(
                    path.read_bytes().replace(b"Delivery</h2>", b"Peer delivery</h2>")
                )
                written.set()
        except (
            AssertionError,
            CrewError,
            OSError,
            subprocess.CalledProcessError,
        ) as error:
            errors.append(error)

    def read_after_editor():
        with _store._serialized_path_lock(path, "plans"):
            assert written.is_set(), "a copied context revived its released lock"
            assert "Peer delivery" in path.read_text()

    worker = threading.Thread(target=editor)
    worker.start()
    timer = threading.Timer(0.2, release.set)
    try:
        assert holding.wait(5)
        timer.start()
        stale.run(read_after_editor)
    finally:
        release.set()
        worker.join(10)
        timer.cancel()
        if timer.ident is not None:
            timer.join(5)
    assert not worker.is_alive()
    assert not errors
