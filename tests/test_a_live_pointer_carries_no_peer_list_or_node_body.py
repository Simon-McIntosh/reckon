"""A live pointer carries no peer list, brief, done-when or peer scopes.

Every fleet-state derivation on the node re-reads the live directory on every
pass, and the pointer a dispatch publishes carries the whole of what it knew:
the wired peer list, the copied node definition with its brief, the raw
done-when text, the peer scopes and the admission findings. Measured on 18
November, ``peer_channel`` was 8.05 MB across 87 pointers and the copied node
definition 4.85 MB. The durable copies already exist in the run's own
directory, so the pointer keeps only what its listing readers consult and names
the file holding the rest.

All checks use a temporary crew home and a throwaway checkout. A queued
dispatch carrying 40 peers and 292 peer scopes writes a pointer under 8 KB and
puts the peer list and the node definition in its run directory. A worker
launched from that pointer is the worker the inline pointer produced: the same
peers, brief and done-when. A pointer written before the move is listed
without its bodies and decodes under 16 KB. The fields a classification reads
survive the move unchanged. Two live runs whose write paths overlap stay as
adjacent to ``_adjacent_live_peers``, ``promotion_scope`` and ``review`` as
they were when the bodies were inline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import dispatch_peer, promotion_scope, runs
from reckon.crew.admission.launching import _node_from_pointer
from reckon.crew.dispatching.requests import _queued_request_record
from reckon.crew.node import TaskNode
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
QUEUED_AT = "2026-11-19T00:00:00+00:00"

PEER_COUNT = 40
PEER_SCOPE_COUNT = 292

#: The pointer budget: a queued dispatch's own record, less the bodies its
#: listing readers never read.
POINTER_BUDGET_BYTES = 8_192

#: The bound a legacy pointer's decode must stay under once its bodies are cut.
LEGACY_DECODE_BUDGET_BYTES = 16_000

#: The bodies a legacy pointer is built to carry: the figures the profile
#: measured on one plan's runs.
LEGACY_PEER_BYTES = 340_000
LEGACY_NODE_BYTES = 50_000

#: A done-when the node contract accepts: it names the file and the result.
DEMONSTRABLE_DONE_WHEN = (
    "tests/test_a_live_pointer_carries_no_peer_list_or_node_body.py passes; "
    "pytest reports 0 failed"
)


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc">'
        '<h2 id="s2">&sect;2 &mdash; Section two</h2>'
        "</main></body></html>\n"
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    (root / "package").mkdir()
    (root / "package" / "definition.py").write_text("x = 1\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs", "package")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _peer_scopes(count: int = PEER_SCOPE_COUNT) -> dict[str, list[str]]:
    """One full-width plan load's scopes: 292 peer declarations, each a tree."""
    return {
        f"peer-{index:03d}": [
            f"package/module_{index:03d}/part_{part:02d}.py" for part in range(8)
        ]
        for index in range(count)
    }


def _node(
    node_id: str,
    *,
    brief: str = "",
    done_when: str = DEMONSTRABLE_DONE_WHEN,
    write_paths: list[str] | None = None,
    peer_scopes: dict[str, list[str]] | None = None,
    manifest_path: str = "",
) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal="move the node body off the live pointer",
        plan=PLAN,
        section="s2",
        spec_level="guided",
        brief=brief,
        done_when=done_when,
        write_paths=write_paths or ["package/definition.py"],
        peer_scopes=peer_scopes or {},
        time_budget="20m",
        manifest_path=manifest_path,
    )


def _pointer_text(run_id: str) -> str:
    return runs.pointer_path(run_id).read_text(encoding="utf-8")


def _adjacent_peers(count: int = PEER_COUNT) -> list[dict]:
    return [
        {
            "run_id": f"r-20261118T000000000{index:03d}-peer-{index:03d}",
            "node": f"peer-03d-{index:03d}",
            "paths": [f"package/module_{index:03d}.py"],
        }
        for index in range(count)
    ]


def _queued_record(repository: Path, node: TaskNode, run_id: str) -> dict:
    """Store one queued request, wired to the declared adjacent peers."""
    record = _queued_request_record(
        run_id=run_id,
        project=PROJECT,
        node=node,
        repo_root=repository,
        session="body-session",
        backend_name="native",
        launch_kind="cli",
        lane_allowance={"reason": "held", "detail": "the project holds no seat"},
        dispatch_options={"project": PROJECT, "plan_slug": node.plan},
        queued_at=QUEUED_AT,
    )
    record["peer_channel"] = dispatch_peer._wire_peer_channels(
        record, _adjacent_peers()
    )
    queued, _position = runs.queue_dispatch(record, max_requests=100)
    return queued


def test_a_dispatch_writes_no_peer_list_or_node_body(
    repository: Path, tmp_path: Path
) -> None:
    """(a) 40 peers and 292 scopes cost the pointer under 8 KB."""
    brief = tmp_path / "brief.md"
    brief.write_text("the brief the worker reads")
    node = _node(
        "body-owner",
        brief=str(brief),
        peer_scopes=_peer_scopes(),
        manifest_path=str(tmp_path / "manifest.md"),
    )
    record = _queued_record(repository, node, "r-20261119T000000000000-body-owner")
    run_id = str(record["run_id"])

    written = _pointer_text(run_id)
    decoded = json.loads(written)
    print(f"case_a_pointer_bytes={len(written.encode('utf-8'))}")
    assert len(written.encode("utf-8")) < POINTER_BUDGET_BYTES
    assert "peers" not in decoded["peer_channel"]
    for name in runs.NODE_BODY_FIELDS:
        assert name not in decoded["node"], name

    body = json.loads(runs.node_body_path(run_id).read_text(encoding="utf-8"))
    assert body["brief"] == str(brief)
    assert body["done_when"] == DEMONSTRABLE_DONE_WHEN
    assert body["peer_scopes"] == _peer_scopes()

    peers = json.loads(
        (runs.run_dir(run_id) / "peer-channel" / "peers.json").read_text(
            encoding="utf-8"
        )
    )["peers"]
    assert sorted(peers) == sorted(peer["run_id"] for peer in _adjacent_peers())


def test_the_launch_from_the_pointer_is_the_same_worker(
    repository: Path, tmp_path: Path
) -> None:
    """(b) The rebuilt node is the inline node: same peers, brief, done-when."""
    brief = tmp_path / "brief.md"
    brief.write_text("the brief the worker reads")
    node = _node(
        "body-rebuilt",
        brief=str(brief),
        peer_scopes=_peer_scopes(),
        write_paths=["package/definition.py", "package/caller.py"],
        manifest_path=str(tmp_path / "manifest.md"),
    )
    inline_id = "r-20261119T000000000000-inline-body"
    split_id = "r-20261119T000000000000-split-body"
    record = _queued_record(repository, node, split_id)
    # The base revision's own shape: the fixture's node, every field inline.
    # Built from the node this test declared rather than from an accessor, so a
    # hydration that dropped a field cannot agree with itself and pass.
    inline_path = runs.pointer_path(inline_id)
    inline_path.parent.mkdir(parents=True, exist_ok=True)
    inline_path.write_text(
        json.dumps(
            {
                "run_id": inline_id,
                "project": PROJECT,
                "repo": str(repository),
                "phase": "queued",
                "node": node.as_dict(),
            }
        ),
        encoding="utf-8",
    )

    rebuilt = _node_from_pointer(runs.read_pointer(split_id))
    at_base = _node_from_pointer(runs.read_pointer(inline_id))
    assert rebuilt.as_dict() == node.as_dict()
    assert at_base.as_dict() == node.as_dict()
    assert rebuilt.as_dict() == at_base.as_dict()

    wired = record["peer_channel"]["peers"]
    assert runs.peer_channel_peers(runs.read_pointer(split_id)) == wired
    assert len(wired) == PEER_COUNT


def test_a_legacy_pointer_is_listed_without_bodies_and_decoded_small(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) A 340 KB peer list and 50 KB node cost a listing under 16 KB."""
    run_id = "r-20261119T000000000000-legacy-body"
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    peer_padding = "x" * (LEGACY_PEER_BYTES // 2)
    node_padding = "y" * LEGACY_NODE_BYTES
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "repo": str(repository),
                "phase": "queued",
                "node": {
                    "id": "legacy-body",
                    "plan": PLAN,
                    "section": "s2",
                    "write_paths": ["package/definition.py"],
                    "brief": node_padding,
                    "done_when": node_padding,
                    "peer_scopes": {"peer": ["package/other.py"]},
                    "admission_findings": [{"detail": node_padding}],
                },
                "peer_channel": {
                    "endpoint": str(runs.run_dir(run_id) / "peer-channel"),
                    "peers": {
                        f"peer-{index}": {"padding": peer_padding} for index in range(2)
                    },
                    "scope_transfer": False,
                },
            }
        ),
        encoding="utf-8",
    )
    original_bytes = len(path.read_bytes())

    decoded_sizes: list[int] = []

    class _RecordingJSON:
        """The stand-in for the json module ``runs`` decodes pointer text with."""

        def __init__(self, module: object) -> None:
            self._module = module

        def loads(self, value: str, *args: object, **kwargs: object) -> object:
            decoded_sizes.append(len(value.encode("utf-8")))
            return self._module.loads(value, *args, **kwargs)

        def __getattr__(self, name: str) -> object:
            return getattr(self._module, name)

    with monkeypatch.context() as recording:
        recording.setattr(runs, "json", _RecordingJSON(json))
        listed = [
            pointer for pointer in runs.list_live() if pointer["run_id"] == run_id
        ]
    assert len(listed) == 1
    pointer = listed[0]
    assert decoded_sizes, "the listing decoded nothing, so the bound proves nothing"
    # The figures are printed before the assertions so a base-revision run
    # records what it measured instead of stopping at the first refutation.
    print(
        f"case_c_legacy_bytes={original_bytes} case_c_decoded_bytes={max(decoded_sizes)}"
    )
    assert original_bytes > 390_000
    assert "peers" not in (pointer.get("peer_channel") or {})
    for name in runs.NODE_BODY_FIELDS:
        assert name not in (pointer.get("node") or {}), name
    assert max(decoded_sizes) < LEGACY_DECODE_BUDGET_BYTES
    assert runs.read_pointer(run_id)["node"]["brief"] == node_padding


def _full_node(node: TaskNode) -> dict:
    body = node.as_dict()
    body["admission_findings"] = [{"property": "scoped", "detail": "declared"}]
    return body


def test_every_field_a_classification_reads_is_unchanged(
    repository: Path, tmp_path: Path
) -> None:
    """(d) A classification reads the same record either way."""
    from reckon.crew import recovery_classification

    run_id = "r-20261119T000000000000-classified-body"
    log = tmp_path / "stream.jsonl"
    log.write_text("", encoding="utf-8")
    manifest = tmp_path / "manifest.md"
    manifest.write_text("status: in-progress\n", encoding="utf-8")
    node = _node(
        "classified-body",
        brief="the brief",
        peer_scopes={"peer": ["package/other.py"]},
        manifest_path=str(manifest),
    )
    payload = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": "0" * 40,
        "phase": "working",
        "log_path": str(log),
        "manifest_path": str(manifest),
        "launcher_host": "fixture-host",
        "node": _full_node(node),
    }
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    at_base = recovery_classification.classify_pointer(runs.read_pointer(run_id))

    runs._write_json(path, payload)
    at_head = recovery_classification.classify_pointer(runs.read_pointer(run_id))
    assert at_base == at_head

    listed = next(row for row in runs.list_live() if row["run_id"] == run_id)
    for name in ("id", "plan", "section", "write_paths"):
        assert listed["node"][name] == payload["node"][name], name
    assert listed["phase"] == payload["phase"]
    assert listed["base_sha"] == payload["base_sha"]


def _two_overlapping_runs(repository: Path, tmp_path: Path) -> tuple[str, str]:
    """Two live runs declaring one path, written through the pointer writer."""
    shared = "package/definition.py"
    first = "r-20261119T000000000000-overlap-a"
    second = "r-20261119T000000000000-overlap-b"
    trees = tmp_path / "trees"
    for run_id, node_id, peer_scopes in (
        (first, "overlap-a", {second: [shared]}),
        (second, "overlap-b", {}),
    ):
        (trees / run_id).mkdir(parents=True, exist_ok=True)
        node = _node(
            node_id,
            write_paths=[shared],
            peer_scopes=peer_scopes,
            manifest_path=str(tmp_path / f"{node_id}.md"),
        )
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "repo": str(repository),
                "worktree": str(trees / run_id),
                "phase": "working",
                "node": _full_node(node),
            },
        )
    return first, second


def _peer_adjacency(repository: Path, candidate: TaskNode) -> list[dict]:
    return dispatch_peer._adjacent_live_peers(
        candidate, project=PROJECT, repo=repository, explicitly_named=set()
    )


def _promotion_peer_scopes(repository: Path) -> dict[str, list[str]]:
    """The peer claims ``promotion_scope``'s boundary walk resolves."""
    resolved: dict[str, list[str]] = {}
    for pointer in runs.list_live():
        node = pointer.get("node") or {}
        tree = Path(str(pointer.get("worktree") or repository))
        resolved[str(pointer.get("run_id") or "")] = sorted(
            path.as_posix()
            for path in promotion_scope._repository_scope_paths(
                node.get("write_paths") or (),
                worktree=tree,
                repository=repository,
            )
        )
    return resolved


def _review_adjacency(project: str, source: str) -> list[str]:
    """The runs ``review`` names as holding one source path."""
    return sorted(
        str(pointer["run_id"])
        for pointer in runs.list_live(project=project)
        if source in (pointer.get("node") or {}).get("write_paths", ())
    )


def test_two_overlapping_runs_are_as_adjacent_as_they_were(
    repository: Path, tmp_path: Path
) -> None:
    """(e) Adjacency, promotion scope and review read the moved pointer alike."""
    shared = "package/definition.py"
    first, second = _two_overlapping_runs(repository, tmp_path)
    candidate = _node(
        "overlap-candidate", write_paths=[shared], peer_scopes={second: [shared]}
    )

    adjacent_slim = _peer_adjacency(repository, candidate)
    promotion_slim = _promotion_peer_scopes(repository)
    review_slim = _review_adjacency(PROJECT, shared)
    assert [row["run_id"] for row in adjacent_slim] == sorted([first, second])
    assert review_slim == sorted([first, second])
    assert promotion_slim[first] == [shared]
    assert promotion_slim[second] == [shared]

    for run_id in (first, second):
        stored = runs.read_pointer(run_id)
        runs.pointer_path(run_id).write_text(
            json.dumps({**stored, "node": runs.node_definition(stored)}),
            encoding="utf-8",
        )

    assert _peer_adjacency(repository, candidate) == adjacent_slim
    assert _promotion_peer_scopes(repository) == promotion_slim
    assert _review_adjacency(PROJECT, shared) == review_slim


def test_a_review_judgement_reads_the_done_when_through_the_pointer(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(f) A sweep's merged record still judges against the node's done-when."""
    from reckon.crew import recovery_review_dispatch as review_dispatch
    from reckon.crew import review_need
    from reckon.crew import runs as runs_module

    done_when = "the sweep's judgement reads the done-when now stored on disk"
    node = _node(
        "judged-body", done_when=done_when, manifest_path=str(tmp_path / "manifest.md")
    )
    run_id = "r-20261119T000000000000-judged-body"
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "phase": "working",
            "node": _full_node(node),
        },
    )
    lifecycle = runs.read_pointer(run_id)
    slim = next(row for row in runs.list_live() if row["run_id"] == run_id)
    assert "done_when" not in (slim.get("node") or {})
    # The composition merge: the sweep's slim snapshot wins over the hydrated
    # record, which is how the judge was reading an empty done-when.
    merged = {**lifecycle, **slim}
    assert "done_when" not in (merged.get("node") or {})

    judged: list[dict] = []
    monkeypatch.setattr(
        review_need, "judge", lambda changes, **kwargs: judged.append(kwargs) or {}
    )
    monkeypatch.setattr(review_dispatch, "_review_accepts_promotion", lambda _r: True)
    monkeypatch.setattr(review_dispatch, "_diff_between", lambda *_a, **_k: "+line")
    from reckon import flight

    monkeypatch.setattr(flight, "review_need_threshold", lambda _c: 0.5)
    monkeypatch.setattr(
        review_dispatch, "_prior_review", lambda *_a, **_k: {"status": "complete"}
    )
    monkeypatch.setattr(
        review_dispatch, "review_described_head", lambda *_a, **_k: "a" * 40
    )

    move = {"reviewed_head": "a" * 40, "head": "b" * 40}
    assert review_dispatch._judge_head_move(merged, move, config=None) is None
    assert judged[-1]["goals"]["done_when"] == done_when

    prediction = review_dispatch.re_review_prediction(
        merged, {"project": PROJECT, "run_id": run_id, "head": "b" * 40}
    )
    assert prediction is not None
    assert prediction["inputs"]["done_when"] == done_when

    # The inline record, the shape the base revision judged, reads the same.
    assert runs_module.node_field(lifecycle, "done_when") == done_when


def test_a_run_with_no_node_still_refuses_its_rebuild(repository: Path) -> None:
    """(g) A pointer carrying no node refuses, rather than rebuilding empty."""
    from reckon.crew import dispatch_sessions
    from reckon.crew.node import CrewError

    run_id = "r-20261119T000000000000-no-node"
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "repo": str(repository),
                "phase": "queued",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CrewError, match="records no node definition"):
        dispatch_sessions._recorded_task_node(runs.read_pointer(run_id))

    node = _node("rebuilt-body", done_when=DEMONSTRABLE_DONE_WHEN)
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "phase": "queued",
            "node": node.as_dict(),
        },
    )
    rebuilt = dispatch_sessions._recorded_task_node(runs.read_pointer(run_id))
    assert rebuilt.done_when == DEMONSTRABLE_DONE_WHEN
    assert rebuilt.brief == node.brief


def test_the_judge_sees_the_done_when_of_a_run_listed_once(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(h) Judged from the listing's own row, the judge reads the real done-when."""
    from reckon.crew import recovery_review_dispatch as review_dispatch
    from reckon.crew import review_need

    done_when = "the judge reads the listed row's done-when"
    node = _node(
        "listed-body", done_when=done_when, manifest_path=str(tmp_path / "manifest.md")
    )
    run_id = "r-20261119T000000000000-listed-body"
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "phase": "working",
            "node": _full_node(node),
        },
    )
    listed = next(row for row in runs.list_live() if row["run_id"] == run_id)
    assert "done_when" not in (listed.get("node") or {})

    judged: list[dict] = []
    monkeypatch.setattr(
        review_need, "judge", lambda changes, **kwargs: judged.append(kwargs) or {}
    )
    monkeypatch.setattr(review_dispatch, "_review_accepts_promotion", lambda _r: True)
    monkeypatch.setattr(review_dispatch, "_diff_between", lambda *_a, **_k: "+line")
    from reckon import flight

    monkeypatch.setattr(flight, "review_need_threshold", lambda _c: 0.5)
    assert (
        review_dispatch._judge_head_move(
            listed, {"reviewed_head": "a" * 40, "head": "b" * 40}, config=None
        )
        is None
    )
    assert judged[-1]["goals"]["done_when"] == done_when
