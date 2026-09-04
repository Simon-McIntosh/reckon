// Reckon shell route module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

function parseHash() {
  const h = (window.location.hash || "").replace(/^#/, "");
  if (!h || h === "home") return { view: "home" };
  if (h === "cockpit") return { view: "home" };
  if (h.startsWith("plan/")) return { view: "plan", slug: decodeURIComponent(h.slice(5)) };
  if (h.startsWith("sprint/")) return { view: "sprint", sprint: decodeURIComponent(h.slice(7)) };
  if (h === "graph") return { view: "graph" };
  if (h === "crew") return { view: "crew" };
  if (h === "plans") return { view: "plan", slug: null };
  if (h === "sprints") return { view: "sprint", sprint: null };
  return { view: "home" };
}

function canvasViewForRoute(route) {
  const view = route?.view;
  return ["home", "plan", "sprint", "graph", "crew"].includes(view)
    ? view
    : "home";
}

// The tab groups a later plan can extend without touching the topbar itself.
const ARTIFACT_TABS = [
  { key: "plan", label: "Plans", index: { view: "plan", slug: null } },
];

const WORK_TABS = [
  { key: "sprint", label: "Sprints", index: { view: "sprint", sprint: null } },
  { key: "graph", label: "Graph", index: { view: "graph" } },
  { key: "crew", label: "Crew", index: { view: "crew" } },
];

function useHashRoute() {
  const [route, setRoute] = useState(parseHash());
  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const nav = useCallback((to) => {
    if (to.view === "home" || to.view === "cockpit") window.location.hash = "#home";
    else if (to.view === "plan") window.location.hash = `#plan/${encodeURIComponent(to.slug)}`;
    else if (to.view === "sprint") window.location.hash = `#sprint/${encodeURIComponent(to.sprint)}`;
    else if (to.view === "graph") window.location.hash = "#graph";
    else if (to.view === "crew") window.location.hash = "#crew";
  }, []);
  return [route, nav];
}

// ─── Top bar ────────────────────────────────────────────────────────────


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.route = { parseHash, canvasViewForRoute, useHashRoute, ARTIFACT_TABS, WORK_TABS };
