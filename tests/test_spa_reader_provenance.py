from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
PLAN_SOURCE = (REPO_ROOT / "docs" / "ui" / "plan.jsx").read_text()
BITS_SOURCE = (REPO_ROOT / "docs" / "ui" / "bits.jsx").read_text()


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_inflight_band_identifies_work_without_rendering_a_run_hash():
    band = _between(PLAN_SOURCE, "function PlanInFlightBand", "function ReaderSourceFailure")

    assert "<code>{run.run_id" not in band
    assert "run.run_id ||" not in band
    assert "run.backend || run.harness" in band
    assert "run.model" in band
    assert 'run.role || "worker"' in band
    assert 'run.effort || "default effort"' in band
    assert 'run.member || "unassigned"' in band
    assert 'run.section || "whole plan"' in band
    assert "elapsed_seconds" in band
    assert "time_budget" in band
    assert "r-inflight-track" in band
    assert "run.phase" in band
    assert band.count("Copy run command") == 1
    assert "reckon crew observe --run ${run.run_id}" in band


def test_authored_html_failure_renders_status_missing_sections_and_retry():
    assert 'source="authored plan HTML"' in PLAN_SOURCE
    assert 'status={htmlFailure.status}' in PLAN_SOURCE
    assert 'missing={["authored body", "authored followups", "authored comments"]}' in PLAN_SOURCE
    assert 'if (!r.ok) throw { responseStatus: `HTTP ${r.status}` };' in PLAN_SOURCE
    assert "setHtmlFailure({ status: error?.responseStatus || \"network error\" })" in PLAN_SOURCE
    assert "setHtmlRetry(value => value + 1)" in PLAN_SOURCE


def test_structured_state_failure_renders_status_missing_sections_and_retry():
    assert 'source="structured plan state"' in PLAN_SOURCE
    assert 'status={stateFailure.status}' in PLAN_SOURCE
    assert 'missing={["interactive decisions", "evidence gates", "structured followups", "loaded plan version"]}' in PLAN_SOURCE
    assert "setStateFailure({ status: error?.responseStatus || \"network error\" })" in PLAN_SOURCE
    assert "setStateRetry(value => value + 1)" in PLAN_SOURCE
    assert "Status: {status}. Missing: {missing.join" in PLAN_SOURCE
    assert ">Retry</button>" in PLAN_SOURCE


def test_copied_handoff_carries_live_source_provenance_and_plan_version():
    provenance = _between(BITS_SOURCE, "function withHandoffProvenance", "function PromptModal")

    assert "Built from live plan HTML and project discovery." in provenance
    assert "Loaded plan version: ${version}" in provenance
    assert "withHandoffProvenance(" in PLAN_SOURCE
    assert "fullState?.version ?? P.version" in PLAN_SOURCE
    assert "planVersion={loadedPlanVersion}" in PLAN_SOURCE
    assert "built from live plan HTML + project discovery" in BITS_SOURCE
