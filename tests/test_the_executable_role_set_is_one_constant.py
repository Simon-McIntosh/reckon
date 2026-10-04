"""Section attempts, closure and promotion use the same run population."""

from reckon import evidence, mcp_views
from reckon.crew import promotion


def test_section_readers_agree_on_executable_runs(monkeypatch):
    roles = ("implement", "test", "review", "investigate")
    rows = [
        {
            "run_id": role,
            "role": role,
            "plan": "fixture",
            "section": "work",
            "gate": "passed",
        }
        for role in roles
    ]
    pointers = [
        {
            "run_id": f"live-{role}",
            "project": "sample",
            "role": role,
            "node": {"plan": "fixture", "section": "work"},
        }
        for role in roles
    ]
    verdict_roles = []
    original_verdict = evidence._run_verdict

    def record_verdict(row):
        verdict_roles.append(row["role"])
        return original_verdict(row)

    monkeypatch.setattr(evidence, "_run_verdict", record_verdict)
    assert evidence._overall_verdict(rows) == "pass"
    attempts = mcp_views._group_section_attempts("sample", rows, pointers)
    assert attempts["fixture"]["work"]["attempts"] == 2 * len(verdict_roles)
    promoted_roles = [
        role
        for role in roles
        if promotion._require_impl_moved(
            role,
            {"role": role, "node": {"plan": "fixture"}, "plan_impl_at_dispatch": 0.1},
            gate="passed",
            failure_classification="",
            no_impl_change="",
            plan_state={"type": "plan", "impl": 0.2},
        )["verdict"]
        == "moved"
    ]
    assert verdict_roles == promoted_roles == ["implement", "test"]
