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
