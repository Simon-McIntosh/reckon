"""Shadow configuration overrides remain visible through routing overlays."""

from reckon import crew
from reckon.crew.dispatch import _shadow_dispatch_config

PRIMARY_AGENT = {
    "backend": "codex",
    "launch": "cli",
    "model": "primary-model",
    "effort": "medium",
    "sandbox": "worktree-full",
}


def _node() -> crew.TaskNode:
    return crew.TaskNode(
        id="comparison",
        goal="compare one candidate configuration",
        plan="comparison-plan",
        role="implement",
        spec_level="guided",
        done_when="the resolved configurations differ only as declared",
        write_paths=["result.json"],
    )


def _config() -> dict:
    return {
        "default_backend": "codex",
        "backends": {
            "codex": {
                "launch": "cli",
                "model": "primary-model",
                "effort": "medium",
                "sandbox": "worktree-full",
            },
            "clive": {
                "launch": "cli",
                "model": "candidate-model",
                "effort": "xhigh",
                "sandbox": "worktree-full",
            },
        },
        "roles": {
            "implement": {
                "backend": "clive",
                "by_spec_level": {"guided": {"effort": "medium"}},
            }
        },
    }


def test_candidate_backend_effort_override_survives_role_overlay() -> None:
    resolved, lineage = _shadow_dispatch_config(
        config=_config(),
        node=_node(),
        primary_agent=PRIMARY_AGENT,
        candidate_backend="clive",
        configuration_overrides={"effort"},
    )

    backend, agent = crew.resolve_role(resolved, "implement", "guided")
    assert backend == "clive"
    assert agent["effort"] == "xhigh"
    assert lineage["substituted"]["effort"] == {
        "primary": "medium",
        "shadow": "xhigh",
        "via": "override",
    }
    assert "effort" not in lineage["inherited"]


def test_shadow_without_effort_override_inherits_primary_effort() -> None:
    resolved, lineage = _shadow_dispatch_config(
        config=_config(),
        node=_node(),
        primary_agent=PRIMARY_AGENT,
        candidate_backend="clive",
        configuration_overrides=set(),
    )

    _backend, agent = crew.resolve_role(resolved, "implement", "guided")
    assert agent["effort"] == "medium"
    assert lineage["inherited"]["effort"] == "medium"
    assert "effort" not in lineage["substituted"]


def test_direct_role_effort_override_is_honoured() -> None:
    config = _config()
    config["backends"]["clive"]["effort"] = "medium"
    config["roles"]["implement"]["effort"] = "xhigh"

    resolved, lineage = _shadow_dispatch_config(
        config=config,
        node=_node(),
        primary_agent=PRIMARY_AGENT,
        candidate_backend="clive",
        configuration_overrides={"effort"},
    )

    _backend, agent = crew.resolve_role(resolved, "implement", "guided")
    assert agent["effort"] == "xhigh"
    assert lineage["substituted"]["effort"] == {
        "primary": "medium",
        "shadow": "xhigh",
        "via": "override",
    }


def test_backend_and_model_change_while_effort_is_inherited() -> None:
    resolved, lineage = _shadow_dispatch_config(
        config=_config(),
        node=_node(),
        primary_agent=PRIMARY_AGENT,
        candidate_backend="clive",
        configuration_overrides=set(),
    )

    _backend, agent = crew.resolve_role(resolved, "implement", "guided")
    assert agent["effort"] == "medium"
    assert lineage["substituted"] == {
        "backend": {"primary": "codex", "shadow": "clive", "via": "backend"},
        "model": {
            "primary": "primary-model",
            "shadow": "candidate-model",
            "via": "backend",
        },
    }
    assert lineage["inherited"]["effort"] == "medium"
