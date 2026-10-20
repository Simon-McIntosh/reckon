"""The fleet home waits for its summary and defers full inventories."""

from pathlib import Path

from tests.spa_browser_harness import file_spa, installed_browser_or_skip, served_spa


def test_home_waits_for_summary_and_discovers_only_after_palette_opens(
    tmp_path: Path,
) -> None:
    state = {"project": "reckon", "inventory": [], "plans": {}, "sprints": []}
    preload = """(() => {
      window.__requests = [];
      window.__summaryResolvers = [];
      const response = payload => new Response(JSON.stringify(payload), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
      window.__deliverSummary = (index, count) => window.__summaryResolvers[index](response({
        projects: [{ project: 'alpha', data: { projects: [{
          project: 'alpha', plans_count: count, active: 2, blocked: 0,
          pending: 0, shipped: 1, progress: 0.5, activity30: [1],
          last_edited: '2026-10-19T12:00:00Z', active_sprint: null,
        }] } }],
      }));
      window.fetch = resource => {
        const url = String(resource);
        window.__requests.push(url);
        if (url.endsWith('/_projects/mounts.json'))
          return Promise.resolve(response({ alpha: '/srv/alpha/docs' }));
        if (url.endsWith('/_projects/index.json'))
          return new Promise(resolve => { window.__summaryResolvers.push(resolve); });
        if (url.includes('/_discover/')) return Promise.resolve(response({ inventory: [] }));
        if (url.endsWith('/crew')) return Promise.resolve(response({ runs: [] }));
        return Promise.reject(new Error(`unmocked request: ${url}`));
      };
    })()"""
    probe = """(async () => {
      const waitFor = async predicate => {
        const deadline = performance.now() + 3000;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await new Promise(resolve => setTimeout(resolve, 25));
        }
        return false;
      };
      await waitFor(() => Boolean(window.__summaryResolvers.length === 1 && document.querySelector('.r-home')));
      const before = {
        discover: window.__requests.filter(url => url.includes('/_discover/')),
        stats: document.querySelector('.r-home-stats')?.textContent || null,
        text: document.querySelector('.r-home').textContent,
      };
      window.dispatchEvent(new CustomEvent('reckon-fleet', { detail: { key: 'rollup' } }));
      await waitFor(() => window.__summaryResolvers.length === 2);
      window.__deliverSummary(1, 3);
      await waitFor(() => document.querySelector('.r-home-stats')?.textContent.includes('3'));
      window.__deliverSummary(0, 99);
      await new Promise(resolve => setTimeout(resolve, 50));
      const after = {
        discover: window.__requests.filter(url => url.includes('/_discover/')),
        text: document.querySelector('.r-home').textContent,
        plans: [...document.querySelectorAll('.r-home-stat')]
          .find(stat => stat.querySelector('span')?.textContent === 'plans')?.querySelector('strong')?.textContent,
      };
      document.querySelector('.r-topbar-search').click();
      await waitFor(() => Boolean(document.querySelector('.r-cmdk')));
      await waitFor(() => window.__requests.some(url => url.endsWith('/_discover/alpha')));
      return {
        before, after,
        requests: window.__requests.filter(url => url.includes('/_discover/')),
        summaryRequests: window.__requests.filter(url => url.endsWith('/_projects/index.json')).length,
      };
    })()"""
    with file_spa(tmp_path, installed_browser_or_skip(), state, route="#home") as spa:
        result = spa.run_probe(
            probe,
            preload_expression=preload,
            ready_expression="Boolean(document.querySelector('.r-home'))",
        )

    assert result["before"]["discover"] == []
    assert result["before"]["stats"] is None
    assert "Loading" in result["before"]["text"]
    assert result["after"]["discover"] == []
    assert result["after"]["plans"] == "3"
    assert "50% progress" in result["after"]["text"]
    assert result["summaryRequests"] == 2
    assert result["requests"] == ["/_discover/alpha"]


def test_served_home_defers_the_current_project_inventory(tmp_path: Path) -> None:
    preload = """(() => {
      const nativeFetch = window.fetch.bind(window);
      window.__requests = [];
      const response = payload => new Response(JSON.stringify(payload), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
      window.fetch = (resource, options) => {
        const url = String(resource);
        window.__requests.push(url);
        if (url.endsWith('/_index/reckon')) return Promise.resolve(response([]));
        if (url.endsWith('/_projects/mounts.json'))
          return Promise.resolve(response({ alpha: '/srv/alpha/docs', reckon: '/srv/reckon/docs' }));
        if (url.endsWith('/_projects/index.json'))
          return Promise.resolve(response({ projects: [{ project: 'alpha', data: { projects: [{
            project: 'alpha', plans_count: 3, active: 2, blocked: 0,
            pending: 0, shipped: 1, progress: 0.5, activity30: [1],
          }] } }] }));
        if (url.includes('/_discover/')) return Promise.resolve(response({ inventory: [] }));
        if (url.endsWith('/crew')) return Promise.resolve(response({ runs: [] }));
        return nativeFetch(resource, options);
      };
    })()"""
    probe = """(async () => {
      const waitFor = async predicate => {
        const deadline = performance.now() + 3000;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await new Promise(resolve => setTimeout(resolve, 25));
        }
        return false;
      };
      const before = window.__requests.filter(url => url.includes('/_discover/'));
      document.querySelector('.r-topbar-search').click();
      await waitFor(() => window.__requests.filter(url => url.includes('/_discover/')).length === 2);
      return { before, after: window.__requests.filter(url => url.includes('/_discover/')) };
    })()"""
    with served_spa(tmp_path, installed_browser_or_skip(), route="#home") as spa:
        result = spa.run_probe(
            probe,
            preload_expression=preload,
            ready_expression="Boolean(document.querySelector('.r-home-stats'))",
        )

    assert result["before"] == []
    assert sorted(result["after"]) == ["/_discover/alpha", "/_discover/reckon"]


def test_failed_summary_shows_retry_without_counts(tmp_path: Path) -> None:
    state = {"project": "reckon", "inventory": [], "plans": {}, "sprints": []}
    preload = """(() => {
      window.__summaryRequests = 0;
      const response = payload => new Response(JSON.stringify(payload), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
      const summary = count => ({ projects: [{ project: 'alpha', data: { projects: [{
        project: 'alpha', plans_count: count, active: 1, blocked: 0,
        pending: 0, shipped: count - 1, progress: 0.5, activity30: [1],
      }] } }] });
      window.fetch = resource => {
        const url = String(resource);
        if (url.endsWith('/_projects/mounts.json'))
          return Promise.resolve(response({ alpha: '/srv/alpha/docs' }));
        if (url.endsWith('/_projects/index.json')) {
          const request = ++window.__summaryRequests;
          if (request === 1) return Promise.resolve(new Response('{}', { status: 404 }));
          if (request === 3) return Promise.reject(new Error('offline'));
          return Promise.resolve(response(summary(request === 2 ? 3 : 4)));
        }
        if (url.endsWith('/crew')) return Promise.resolve(response({ runs: [] }));
        if (url.includes('/_discover/')) return Promise.resolve(response({ inventory: [] }));
        return Promise.reject(new Error(`unmocked request: ${url}`));
      };
    })()"""
    probe = """(async () => {
      const waitFor = async predicate => {
        const deadline = performance.now() + 3000;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await new Promise(resolve => setTimeout(resolve, 25));
        }
        return false;
      };
      const firstError = await waitFor(() => Boolean(document.querySelector('.r-home-error')));
      const initial = {
        errorVisible: firstError,
        text: document.querySelector('.r-home')?.textContent || '',
        statsVisible: Boolean(document.querySelector('.r-home-stats')),
        retryVisible: Boolean(document.querySelector('.r-home-error button')),
      };
      if (!firstError) return { initial };
      document.querySelector('.r-home-error button').click();
      await waitFor(() => document.querySelector('.r-home-stats')?.textContent.includes('3'));
      const retried = document.querySelector('.r-home-stats')?.textContent || '';
      window.dispatchEvent(new CustomEvent('reckon-fleet', { detail: { key: 'rollup' } }));
      const eventError = await waitFor(() => Boolean(document.querySelector('.r-home-error')));
      window.dispatchEvent(new CustomEvent('reckon-fleet', { detail: { key: 'rollup' } }));
      await waitFor(() => document.querySelector('.r-home-stats')?.textContent.includes('4'));
      return {
        initial, retried, eventError,
        recovered: document.querySelector('.r-home-stats')?.textContent || '',
        requests: window.__summaryRequests,
      };
    })()"""
    with file_spa(tmp_path, installed_browser_or_skip(), state, route="#home") as spa:
        result = spa.run_probe(
            probe,
            preload_expression=preload,
            ready_expression="Boolean(document.querySelector('.r-home'))",
        )

    assert result["initial"]["errorVisible"] is True
    assert "Fleet summary unavailable" in result["initial"]["text"]
    assert result["initial"]["statsVisible"] is False
    assert result["initial"]["retryVisible"] is True
    assert "plans3" in result["retried"]
    assert result["eventError"] is True
    assert "plans4" in result["recovered"]
    assert result["requests"] == 4


def test_project_missing_plan_count_has_a_visible_pending_row(tmp_path: Path) -> None:
    state = {"project": "reckon", "inventory": [], "plans": {}, "sprints": []}
    preload = """(() => {
      const response = payload => new Response(JSON.stringify(payload), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
      window.fetch = resource => {
        const url = String(resource);
        if (url.endsWith('/_projects/mounts.json'))
          return Promise.resolve(response({ alpha: '/srv/alpha/docs', beta: '/srv/beta/docs' }));
        if (url.endsWith('/_projects/index.json'))
          return Promise.resolve(response({ projects: [
            { project: 'alpha', data: { projects: [{ project: 'alpha', plans_count: 2, active: 1, shipped: 1 }] } },
            { project: 'beta', data: { projects: [{ project: 'beta', active: 1 }] } },
          ] }));
        if (url.endsWith('/crew')) return Promise.resolve(response({ runs: [] }));
        if (url.includes('/_discover/')) return Promise.resolve(response({ inventory: [] }));
        return Promise.reject(new Error(`unmocked request: ${url}`));
      };
    })()"""
    probe = """(() => ({
      scope: document.querySelector('.r-home-scope')?.textContent || '',
      rows: [...document.querySelectorAll('.r-home-project')]
        .map(row => row.querySelector('.r-home-project-id strong')?.textContent),
      pending: document.querySelector('.r-home-project-pending')?.textContent || '',
    }))()"""
    with file_spa(tmp_path, installed_browser_or_skip(), state, route="#home") as spa:
        result = spa.run_probe(
            probe,
            preload_expression=preload,
            ready_expression="Boolean(document.querySelector('.r-home-stats'))",
        )

    assert result["scope"] == "2 of 2 shown"
    assert len(result["rows"]) == 2
    assert result["rows"] == ["alpha", "beta"]
    assert "plan count pending" in result["pending"]


def test_summary_landed_rows_keep_artifact_types_without_discovery(
    tmp_path: Path,
) -> None:
    state = {"project": "reckon", "inventory": [], "plans": {}, "sprints": []}
    preload = """(() => {
      window.__requests = [];
      const response = payload => new Response(JSON.stringify(payload), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
      window.fetch = resource => {
        const url = String(resource);
        window.__requests.push(url);
        if (url.endsWith('/_projects/mounts.json'))
          return Promise.resolve(response({ alpha: '/srv/alpha/docs' }));
        if (url.endsWith('/_projects/index.json'))
          return Promise.resolve(response({ projects: [{ project: 'alpha', data: { projects: [{
            project: 'alpha', plans_count: 2, active: 0, blocked: 0,
            pending: 0, shipped: 2, recent_landed: [
              { slug: 'proof', title: 'Proof', type: 'evidence', edited: new Date().toISOString() },
              { slug: 'release', title: 'Release', edited: new Date().toISOString() },
            ],
          }] } }] }));
        if (url.endsWith('/crew')) return Promise.resolve(response({ runs: [] }));
        if (url.includes('/_discover/')) return Promise.resolve(response({ inventory: [] }));
        return Promise.reject(new Error(`unmocked request: ${url}`));
      };
    })()"""
    probe = """(() => ({
      rows: [...document.querySelectorAll('.r-home-landed > button')].map(row => ({
        kind: row.querySelector('.r-home-kind')?.textContent,
        className: row.querySelector('.r-home-kind')?.className,
        title: row.querySelector('strong')?.textContent,
      })),
      discoveries: window.__requests.filter(url => url.includes('/_discover/')),
    }))()"""
    with file_spa(tmp_path, installed_browser_or_skip(), state, route="#home") as spa:
        result = spa.run_probe(
            probe,
            preload_expression=preload,
            ready_expression="document.querySelectorAll('.r-home-landed > button').length === 2",
        )

    assert result["rows"] == [
        {"kind": "evidence", "className": "r-home-kind evidence", "title": "Proof"},
        {"kind": "plan", "className": "r-home-kind plan", "title": "Release"},
    ]
    assert result["discoveries"] == []
