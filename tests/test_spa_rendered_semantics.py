from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from tests.spa_browser_harness import (
    NODE_PROBE,  # noqa: F401 - compatibility re-export for adjacent browser checks
    BrowserProbeError,
    installed_browser,
    installed_browser_or_skip,
    run_browser_probe,
    served_spa,
)

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _skip_when_browser_is_unavailable():
    try:
        yield
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")


@pytest.fixture(scope="module")
def rendered_browser(tmp_path_factory) -> str:
    browser = installed_browser()
    if browser is None:
        pytest.skip("no supported browser binary is installed")
    with _skip_when_browser_is_unavailable():
        run_browser_probe(
            tmp_path_factory.mktemp("browser-capability"),
            browser,
            "<!doctype html><html><body>ready</body></html>",
            "document.body.textContent",
        )
    return browser


def test_browser_classifier_does_not_mask_assertion_failure() -> None:
    with (
        pytest.raises(AssertionError, match="rendered assertion is wrong"),
        _skip_when_browser_is_unavailable(),
    ):
        raise AssertionError("rendered assertion is wrong")


PLAN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="reckon">
  <meta name="reckon-type" content="plan">
  <meta name="plan-slug" content="rendered-contract">
  <meta name="plan-title" content="Rendered contract">
  <meta name="plan-summary" content="Exercise the composed semantic surface.">
  <meta name="plan-status" content="active">
  <meta name="plan-impl" content="0.42">
  <meta name="plan-version" content="7">
  <meta name="plan-roi" content="high">
  <meta name="plan-effort-hours" content="3.25">
  <meta name="plan-sprint" content="focus">
  <meta name="plan-modified" content="2026-08-25">
  <title>Rendered contract | reckon</title>
</head>
<body>
<main class="plan-doc">
  <h2 id="implementation">Implementation</h2>
  <p>The authored body remains visible when structured state is available.</p>
  <section data-reckon="gates" id="gates" class="r-gates">
    <h2 id="gate-state-heading"><span class="sec">§</span> Evidence gates</h2>
    <div class="r-gate" data-id="rendered-contract" data-section="implementation"
         data-gated-sections="implementation" data-status="open" data-verdict="">
      <h4 class="r-gate-measure">Rendered contract remains visible</h4>
      <p class="r-gate-required-evidence">A composed-page assertion</p>
    </div>
  </section>
  <section data-reckon="followups" id="followups" class="r-followups">
    <h2><span class="sec">§</span> Followups</h2>
    <article class="r-fu" data-id="next" data-status="open"
             data-written-by="tester" data-written-at="2026-08-25"
             data-recommends-skill="/reckon-ship rendered-contract"
             data-resolved-at="" data-resolved-by="">
      <h4 class="r-fu-title">Continue the rendered contract</h4>
      <div class="r-fu-body">Keep the semantic output observable.</div>
      <pre class="r-fu-prompt">/reckon-ship rendered-contract</pre>
    </article>
  </section>
</main>
</body>
</html>
"""

INDEX_STATE = {
    "updated": "2026-08-25T00:00:00",
    "project": "reckon",
    "doc": "index",
    "data": {
        "active_sprint_id": "focus",
        "projects": [
            {
                "project": "reckon",
                "owner": "Test owner",
                "plans_count": 1,
                "active": 1,
                "blocked": 0,
                "pending": 0,
                "shipped": 0,
                "milestones": [],
            }
        ],
        "sprints": [
            {
                "id": "focus",
                "theme": "Stored focus",
                "status": "active",
                "starts": "2026-08-25",
                "ends": "2026-08-26",
                "items": [{"slug": "rendered-contract"}],
                "metrics": {
                    "item_count": 1,
                    "by_effective_status": {"active": 1},
                    "mean_impl": 0.42,
                    "current_work": [
                        {
                            "slug": "rendered-contract",
                            "title": "Rendered contract",
                            "effective_status": "active",
                            "impl": 0.42,
                        }
                    ],
                },
            },
            {
                "id": "concurrent",
                "theme": "Concurrent work",
                "status": "active",
                "starts": "2026-08-25",
                "ends": "2026-08-27",
                "items": [],
                "metrics": {
                    "item_count": 0,
                    "by_effective_status": {},
                    "mean_impl": 0.0,
                    "current_work": [],
                },
            },
        ],
        "milestones": [],
        "blockers": [],
        "timeline": [],
        "north_stars": [],
    },
}

REVIEW_STATE = {
    "reviewed_at": "2026-08-26",
    "reviewed_by": "reviewer",
    "sprint_order": ["concurrent", "focus"],
    "findings": [
        {
            "id": "active-pointer-conflict",
            "code": "active-sprint-mismatch",
            "category": "sprint",
            "severity": "warn",
            "subject": {"kind": "sprint", "id": "concurrent"},
            "evidence": ["Stored focus differs from the active sprint set."],
            "resolved_at": "",
        },
        {
            "id": "plan-scope",
            "code": "scope-needs-review",
            "category": "sprint",
            "severity": "info",
            "subject": {"kind": "plan", "id": "rendered-contract"},
            "evidence": ["The plan remains broader than its current work."],
            "resolved_at": "",
        },
    ],
    "priority": [
        {
            "rank": 1,
            "ref": "rendered-contract",
            "reasons": ["critical-path", "decision-first"],
            "detail": "Resolve the live contract first.",
            "status": "active",
            "effective_status": "active",
            "impl": 0.42,
            "sprint": "focus",
            "landed": False,
        },
        {
            "rank": 2,
            "ref": "landed-contract",
            "reasons": ["roi"],
            "detail": "Retained as review history.",
            "status": "shipped",
            "effective_status": "shipped",
            "impl": 1.0,
            "sprint": "concurrent",
            "landed": True,
        },
    ],
}


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_fixture_docs(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    plans = docs / "plans"
    state = docs / "state" / "reckon"
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    (docs / "index.html").symlink_to(ROOT / "docs" / "index.html")
    (plans / "rendered-contract.html").write_text(PLAN_HTML, encoding="utf-8")
    (state / "index.json").write_text(
        json.dumps(INDEX_STATE),
        encoding="utf-8",
    )
    return docs


@contextmanager
def _served_fixture(tmp_path: Path, route: str):
    port = _unused_port()
    docs = _write_fixture_docs(tmp_path)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"reckon": str(docs)}), encoding="utf-8")
    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from reckon.serve import main; main(port=int(__import__('sys').argv[1]), "
                "host='127.0.0.1', "
                "mounts_file=__import__('pathlib').Path(__import__('sys').argv[2]))"
            ),
            str(port),
            str(mounts),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/reckon/{route}"
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                stderr = server.stderr.read() if server.stderr else ""
                raise AssertionError(f"reckon server exited before readiness: {stderr}")
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/reckon/", timeout=0.5
                ) as response:
                    if response.status == 200:
                        break
            except URLError:
                time.sleep(0.1)
        else:
            raise AssertionError("reckon server did not become ready within 15 seconds")
        yield url
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _rendered_probe(
    tmp_path: Path,
    *,
    route: str,
    wait_selector: str,
    probe: str,
    remove_signal: str,
    fail_plan_html: bool = False,
    prepare_signal: str = "undefined",
    review: dict[str, object] | None = None,
    refresh_probe: str | None = None,
    new_plan: dict[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    preload_expression = r"""{
      const fixtureIndex = __FIXTURE_INDEX__;
      const fixtureSprints = fixtureIndex.data.sprints;
      const fixtureReview = __FIXTURE_REVIEW__;
      const fixtureNewPlan = __FIXTURE_NEW_PLAN__;
      const nativeFetch = window.fetch.bind(window);
      window.__discoveryRequestCount = 0;
      window.fetch = (resource, options) => {
        const url = String(resource);
        if (url.endsWith("/state/reckon/index.json")) {
          return Promise.resolve(new Response(JSON.stringify(fixtureIndex), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }));
        }
        if (url.endsWith("/_discover/reckon")) {
          window.__discoveryRequestCount += 1;
          return nativeFetch(resource, options).then(response => response.json()).then(payload => {
            const inventory = fixtureNewPlan && window.__discoveryRequestCount > 1
              ? [...(payload.inventory || []), fixtureNewPlan]
              : payload.inventory;
            return new Response(JSON.stringify({
              ...payload,
              inventory,
              sprints: fixtureSprints,
              active_sprint_id: fixtureIndex.data.active_sprint_id,
              review: fixtureReview,
              source_format: "distributed",
              resource_versions: {
                "project:reckon": 4,
                "sprint:focus": 2,
                "sprint:concurrent": 1,
              },
            }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          });
        }
        if (__FAIL_PLAN_HTML__ && url.includes("/plans/") && url.endsWith(".html")) {
          return Promise.resolve(new Response("", { status: 503 }));
        }
        return nativeFetch(resource, options);
      };
    }"""
    replacements = {
        "__FIXTURE_INDEX__": INDEX_STATE,
        "__FIXTURE_REVIEW__": review,
        "__FIXTURE_NEW_PLAN__": new_plan,
        "__FAIL_PLAN_HTML__": fail_plan_html,
    }
    for marker, value in replacements.items():
        preload_expression = preload_expression.replace(marker, json.dumps(value))

    docs = _write_fixture_docs(tmp_path)
    ready_expression = (
        f"Boolean(window.STATE && document.querySelector({json.dumps(wait_selector)}))"
    )
    with served_spa(
        tmp_path,
        installed_browser_or_skip(),
        docs=docs,
        route=route,
    ) as context:
        result = context.run_probe(
            probe,
            ready_expression=ready_expression,
            preload_expression=preload_expression,
            prepare_expression=prepare_signal,
            remove_expression=None if refresh_probe is not None else remove_signal,
            refresh_expression=refresh_probe,
        )
    assert isinstance(result, dict)
    return result


def _assert_rendered_signal(observation: dict[str, object], name: str) -> None:
    assert observation["ok"] is True, f"rendered signal missing: {name}: {observation}"


def _assert_removal_is_detected(
    observation: dict[str, object],
    name: str,
) -> None:
    with pytest.raises(AssertionError, match=f"rendered signal missing: {name}"):
        _assert_rendered_signal(observation, name)


def test_overview_renders_every_active_sprint_and_focus_conflict(
    tmp_path: Path, rendered_browser: str
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#cockpit",
        wait_selector=".r-overview-project-row",
        probe="""(() => {
          const row = document.querySelector(".r-overview-project-row");
          const links = [...row.querySelectorAll(".r-overview-sprints > a")];
          const conflict = row.querySelector(".r-overview-conflict[role='alert']");
          const sprintNames = links.map(link => link.textContent.trim());
          return {
            ok: sprintNames.some(name => name.includes("focus"))
              && sprintNames.some(name => name.includes("concurrent"))
              && Boolean(conflict)
              && conflict.textContent.includes("focus")
              && conflict.textContent.includes("concurrent"),
            sprintNames,
            conflict: conflict?.textContent.trim() || null,
          };
        })()""",
        remove_signal="document.querySelector('.r-overview-conflict')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "active sprint conflict")
    _assert_removal_is_detected(result["removed"], "active sprint conflict")


def test_sprint_view_renders_composed_metrics_findings_order_and_priority(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#sprint/focus",
        wait_selector=".r-priority-panel",
        review=REVIEW_STATE,
        prepare_signal="document.querySelector('.r-overview-controls input')?.click()",
        probe="""(() => {
          const rows = [...document.querySelectorAll(".r-timeline-row")];
          const priorities = [...document.querySelectorAll(".r-priority-panel li")];
          const reasons = [...document.querySelectorAll(".r-reason-chips span")]
            .map(chip => chip.textContent.trim());
          const findingLinks = [...document.querySelectorAll(".r-review-badge")];
          const focus = rows.find(row => row.textContent.includes("focus"));
          return {
            ok: rows[0]?.textContent.includes("concurrent")
              && focus?.classList.contains("derived-active")
              && focus?.textContent.includes("1 items")
              && focus?.textContent.includes("mean 42%")
              && focus?.textContent.includes("Rendered contract")
              && priorities.length === 2
              && !priorities[0].classList.contains("landed")
              && priorities[1].classList.contains("landed")
              && reasons.includes("critical-path")
              && reasons.includes("decision-first")
              && findingLinks.some(link => link.textContent.includes("active-sprint-mismatch"))
              && findingLinks.every(link => link.getAttribute("href")?.startsWith("#review-finding-")),
            order: rows.map(row => row.querySelector("strong")?.textContent),
            priorities: priorities.map(row => row.textContent.replace(/\\s+/g, " ").trim()),
            reasons,
            findings: findingLinks.map(link => link.textContent.trim()),
          };
        })()""",
        remove_signal="document.querySelector('.r-priority-panel')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "composed sprint review")
    _assert_removal_is_detected(result["removed"], "composed sprint review")


def test_sprint_view_without_review_has_no_review_affordances(
    tmp_path: Path, rendered_browser: str
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#sprint/focus",
        wait_selector=".r-sprint-overview",
        probe="""(() => ({
          ok: Boolean(document.querySelector(".r-sprint-overview"))
            && !document.querySelector(".r-priority-panel")
            && !document.querySelector(".r-review-badge")
            && !document.querySelector(".r-review-findings"),
        }))()""",
        remove_signal="document.querySelector('.r-sprint-overview')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "review-optional sprint view")
    _assert_removal_is_detected(result["removed"], "review-optional sprint view")


def test_plan_row_renders_authored_to_effective_transition_and_open_gate_count(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plans",
        wait_selector=".r-status-transition",
        probe="""(() => {
          const transition = document.querySelector(".r-status-transition");
          const text = transition?.textContent.replace(/\\s+/g, " ").trim() || "";
          return {
            ok: text.includes("active")
              && text.includes("blocked")
              && text.includes("1 open gate"),
            text,
          };
        })()""",
        remove_signal="document.querySelector('.r-status-transition')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "status transition")
    _assert_removal_is_detected(result["removed"], "status transition")


def test_reader_renders_partial_source_status_missing_sections_and_retry(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plan/rendered-contract",
        wait_selector=".r-reader-source-failure",
        fail_plan_html=True,
        probe="""(() => {
          const banner = document.querySelector(".r-reader-source-failure[role='alert']");
          const text = banner?.textContent.replace(/\\s+/g, " ").trim() || "";
          return {
            ok: text.includes("authored plan HTML failed")
              && text.includes("HTTP 503")
              && text.includes("authored body")
              && text.includes("authored followups")
              && text.includes("authored comments")
              && [...(banner?.querySelectorAll("button") || [])]
                .some(button => button.textContent.trim() === "Retry"),
            text,
          };
        })()""",
        remove_signal="document.querySelector('.r-reader-source-failure')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "partial source failure")
    _assert_removal_is_detected(result["removed"], "partial source failure")


def test_shell_renders_snapshot_source_resource_count_load_time_and_refresh(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#cockpit",
        wait_selector=".r-topbar .settings",
        prepare_signal="""(async () => {
          document.querySelector('.r-topbar .settings > button')?.click();
          const deadline = Date.now() + 5000;
          while (Date.now() < deadline
              && !document.querySelector('.settings-menu .r-snapshot-receipt')) {
            await new Promise(resolve => setTimeout(resolve, 50));
          }
        })()""",
        probe="""(() => {
          const headerReceipt = document.querySelector(".r-topbar > .r-snapshot-receipt");
          const receipt = document.querySelector(".settings-menu .r-snapshot-receipt[role='status']");
          const text = receipt?.textContent.replace(/\\s+/g, " ").trim() || "";
          const count = Number(text.match(/(\\d+) resources/)?.[1] || 0);
          return {
            ok: !headerReceipt
              && !text.includes("unknown source")
              && count > 0
              && /loaded (?!unknown)/.test(text)
              && [...(receipt?.querySelectorAll("button") || [])]
                .some(button => button.textContent.trim() === "Refresh"),
            text,
            count,
          };
        })()""",
        remove_signal="document.querySelector('.settings-menu .r-snapshot-receipt')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "snapshot receipt")
    _assert_removal_is_detected(result["removed"], "snapshot receipt")


def test_refresh_revalidates_discovery_without_document_navigation(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plans",
        wait_selector=".r-list",
        probe="undefined",
        remove_signal="undefined",
        new_plan={
            "slug": "created-while-open",
            "nav_key": "created-while-open",
            "title": "Created while open",
            "type": "plan",
            "status": "pending",
            "effective_status": "pending",
            "path": "plans/created-while-open.html",
        },
        refresh_probe="""(async () => {
          const initialDocument = document;
          const initialLocation = location.href;
          const initialNavigations = performance.getEntriesByType("navigation").length;
          const initialRequests = window.__discoveryRequestCount;
          await new Promise(resolve => setTimeout(resolve, 1200));
          const steadyStateRequests = window.__discoveryRequestCount - initialRequests;

          document.querySelector(".r-topbar .settings > button")?.click();
          const refresh = [...document.querySelectorAll(".settings-menu button")]
            .find(button => button.textContent.trim() === "Refresh");
          refresh?.click();

          const deadline = Date.now() + 5000;
          while (Date.now() < deadline
              && !document.body.textContent.includes("Created while open")) {
            await new Promise(resolve => setTimeout(resolve, 50));
          }
          return {
            newPlanVisible: document.body.textContent.includes("Created while open"),
            sameDocument: document === initialDocument,
            sameLocation: location.href === initialLocation,
            navigationEntriesAdded:
              performance.getEntriesByType("navigation").length - initialNavigations,
            discoveryRequests: window.__discoveryRequestCount,
            steadyStateRequests,
            steadyStateWindowMs: 1200,
          };
        })()""",
    )["refreshed"]

    assert result == {
        "newPlanVisible": True,
        "sameDocument": True,
        "sameLocation": True,
        "navigationEntriesAdded": 0,
        "discoveryRequests": 2,
        "steadyStateRequests": 0,
        "steadyStateWindowMs": 1200,
    }


def test_handoff_renders_live_source_and_loaded_plan_version(
    tmp_path: Path, rendered_browser: str
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plan/rendered-contract",
        wait_selector=".r-titlebar .gen-prompt",
        probe="""(async () => {
          document.querySelector(".r-titlebar .gen-prompt")?.click();
          const deadline = Date.now() + 5000;
          while (Date.now() < deadline && !document.querySelector(".r-modal textarea")) {
            await new Promise(resolve => setTimeout(resolve, 50));
          }
          const modal = document.querySelector(".r-modal");
          const text = modal?.querySelector("textarea")?.value || "";
          const footer = modal?.querySelector(".foot")?.textContent.replace(/\\s+/g, " ").trim() || "";
          return {
            ok: text.includes("Built from live plan HTML and project discovery.")
              && text.includes("Loaded plan version: 7")
              && footer.includes("built from live plan HTML + project discovery")
              && footer.includes("plan version 7"),
            footer,
            provenance: text.split("Handoff provenance").pop()?.trim() || "",
          };
        })()""",
        remove_signal="document.querySelector('.r-titlebar .gen-prompt')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "handoff provenance")
    _assert_removal_is_detected(result["removed"], "handoff provenance")


def test_compact_progress_renders_label_tooltip_and_navigation_target(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plans",
        wait_selector=".r-compact-signal.pct",
        probe="""(async () => {
          const signal = document.querySelector(".r-compact-signal.pct");
          const label = signal?.getAttribute("aria-label") || "";
          const tooltip = signal?.getAttribute("title") || "";
          signal?.click();
          await new Promise(resolve => setTimeout(resolve, 100));
          return {
            ok: label.includes("42 percent complete")
              && tooltip.includes("42 percent complete")
              && location.hash.includes("#plan/rendered-contract"),
            label,
            tooltip,
            target: location.hash,
          };
        })()""",
        remove_signal="document.querySelector('.r-compact-signal.pct')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "compact progress navigation")
    _assert_removal_is_detected(
        result["removed"],
        "compact progress navigation",
    )
