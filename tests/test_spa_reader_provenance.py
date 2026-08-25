import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


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
    assert "run.model" not in band
    assert "run.effort" not in band
    assert 'run.role || "worker"' in band
    assert "worker-hours" in band
    assert "effortHours={P.effort_hours}" in PLAN_SOURCE
    assert 'run.member || "unassigned"' in band
    assert 'run.section || "whole plan"' in band
    assert "elapsed_seconds" in band
    assert "time_budget" in band
    assert "r-inflight-track" in band
    assert "run.phase" in band
    assert band.count("Copy run command") == 1
    assert "reckon crew observe --run ${run.run_id}" in band


def test_inflight_band_fits_reader_at_minimum_width(tmp_path):
    chrome = shutil.which("google-chrome")
    if chrome is None:
        pytest.skip("browser-backed geometry check requires google-chrome")

    reader_css = (REPO_ROOT / "docs" / "ui" / "reader.css").read_text()
    fixture = tmp_path / "inflight-band.html"
    fixture.write_text(
        f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; }}
:root {{ --line: #ddd; --line-2: #ccc; --border: #ccc; --accent: #2867c7; --muted: #666; }}
body {{ margin: 0; width: 1374px; font: 12px Arial, sans-serif; }}
.workspace {{ display: flex; width: 1374px; }}
.rail {{ flex: 0 0 178px; }}
.list {{ flex: 0 0 390px; }}
.r-reading {{ flex: 1; min-width: 0; padding: 22px 26px 34px; }}
.attachments {{ flex: 0 0 300px; }}
.r-inflight-band {{ margin-bottom: 18px; padding: 12px 14px; border: 1px solid var(--border); font-size: 12px; }}
.r-inflight-heading {{ margin-bottom: 8px; }}
.r-inflight-runs {{ display: grid; gap: 9px; margin: 0; padding: 0; list-style: none; }}
.r-inflight-identity {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }}
.r-inflight-progress {{ display: grid; gap: 4px; font-size: 10px; }}
.r-inflight-action {{ white-space: nowrap; }}
{reader_css}
</style></head><body>
<div class="workspace"><div class="rail"></div><div class="list"></div><main class="r-reading">
  <aside class="r-inflight-band"><div class="r-inflight-heading">In flight</div>
    <ul class="r-inflight-runs"><li class="r-inflight-run">
      <div class="r-inflight-identity"><strong>codex</strong><span>worker · 3.25 worker-hours</span><span>@runner</span><span>reader</span></div>
      <div class="r-inflight-progress"><span>4m / 25m</span><span class="r-inflight-track"></span></div>
      <span class="r-inflight-phase">working</span><button class="r-inflight-action">Copy run command</button>
    </li></ul>
  </aside><output id="measure"></output>
</main><div class="attachments"></div></div>
<script>
const band = document.querySelector('.r-inflight-band');
const text = band.textContent;
document.querySelector('#measure').textContent = JSON.stringify({{
  clientWidth: band.clientWidth,
  scrollWidth: band.scrollWidth,
  fits: band.scrollWidth <= band.clientWidth,
  hasWorkerHours: text.includes('3.25 worker-hours'),
  hasRuntimeModel: text.includes('gpt-5.6-sol'),
}});
</script></body></html>"""
    )

    result = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox", "--dump-dom", fixture.as_uri()],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    rendered = result.stdout
    match = re.search(r'<output id="measure">(\{.*?\})</output>', rendered)
    assert match is not None
    measurement = json.loads(match.group(1))
    assert measurement["scrollWidth"] <= measurement["clientWidth"]
    assert measurement["fits"] is True
    assert measurement["hasWorkerHours"] is True
    assert measurement["hasRuntimeModel"] is False


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
