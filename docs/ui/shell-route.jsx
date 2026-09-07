// Reckon shell route module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

const ARTIFACT_ROUTES = [
  { key: "plan", label: "Plans", indexHash: "plans", readerHash: "plan", reader: "document" },
  { key: "research", label: "Research", indexHash: "research", readerHash: "research", reader: "document" },
  { key: "evidence", label: "Evidence", indexHash: "evidence", readerHash: "evidence", reader: "document" },
  { key: "figure", label: "Figures", indexHash: "figures", readerHash: "figure", reader: "image" },
  { key: "gif", label: "Animations", indexHash: "animations", readerHash: "gif", reader: "image", initialSize: "natural" },
];

const ARTIFACT_CANVAS_VIEWS = new Set(ARTIFACT_ROUTES.map(route => route.key));
const SHELL_CANVAS_VIEWS = new Set([
  "home",
  "sprint",
  "graph",
  "crew",
  ...ARTIFACT_CANVAS_VIEWS,
]);

function parseHash() {
  const h = (window.location.hash || "").replace(/^#/, "");
  if (!h || h === "home") return { view: "home" };
  if (h === "cockpit") return { view: "home" };
  for (const route of ARTIFACT_ROUTES) {
    if (h === route.indexHash) return { view: route.key, slug: null };
    const readerPrefix = `${route.readerHash}/`;
    if (h.startsWith(readerPrefix) && h.length > readerPrefix.length) {
      return { view: route.key, slug: decodeURIComponent(h.slice(readerPrefix.length)) };
    }
  }
  if (h.startsWith("sprint/")) return { view: "sprint", sprint: decodeURIComponent(h.slice(7)) };
  if (h === "graph") return { view: "graph" };
  if (h === "crew") return { view: "crew" };
  if (h === "sprints") return { view: "sprint", sprint: null };
  return { view: "home" };
}

function canvasViewForRoute(route) {
  const view = route?.view;
  return SHELL_CANVAS_VIEWS.has(view)
    ? view
    : "home";
}

const ARTIFACT_TABS = ARTIFACT_ROUTES.map(route => ({
  key: route.key,
  label: route.label,
  index: { view: route.key, slug: null },
}));

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
    else if (ARTIFACT_ROUTES.some(route => route.key === to.view)) {
      const route = ARTIFACT_ROUTES.find(candidate => candidate.key === to.view);
      window.location.hash = to.slug
        ? `#${route.readerHash}/${encodeURIComponent(to.slug)}`
        : `#${route.indexHash}`;
    }
    else if (to.view === "sprint") window.location.hash = `#sprint/${encodeURIComponent(to.sprint)}`;
    else if (to.view === "graph") window.location.hash = "#graph";
    else if (to.view === "crew") window.location.hash = "#crew";
  }, []);
  return [route, nav];
}

// ─── Top bar ────────────────────────────────────────────────────────────


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.route = { parseHash, canvasViewForRoute, useHashRoute, ARTIFACT_ROUTES, ARTIFACT_CANVAS_VIEWS, ARTIFACT_TABS, WORK_TABS };
