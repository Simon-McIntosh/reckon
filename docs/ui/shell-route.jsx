// Reckon shell route module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

function parseHash() {
  const h = (window.location.hash || "").replace(/^#/, "");
  if (!h || h === "cockpit") return { view: "cockpit" };
  if (h.startsWith("plan/")) return { view: "plan", slug: decodeURIComponent(h.slice(5)) };
  if (h.startsWith("sprint/")) return { view: "sprint", sprint: decodeURIComponent(h.slice(7)) };
  if (h === "graph") return { view: "graph" };
  if (h === "crew") return { view: "crew" };
  if (h === "plans") return { view: "plan", slug: null };
  if (h === "sprints") return { view: "sprint", sprint: null };
  return { view: "cockpit" };
}

function canvasViewForRoute(route) {
  const view = route?.view;
  return ["cockpit", "plan", "sprint", "graph", "crew"].includes(view)
    ? view
    : "cockpit";
}

function useHashRoute() {
  const [route, setRoute] = useState(parseHash());
  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const nav = useCallback((to) => {
    if (to.view === "cockpit") window.location.hash = "#cockpit";
    else if (to.view === "plan") window.location.hash = `#plan/${encodeURIComponent(to.slug)}`;
    else if (to.view === "sprint") window.location.hash = `#sprint/${encodeURIComponent(to.sprint)}`;
    else if (to.view === "graph") window.location.hash = "#graph";
    else if (to.view === "crew") window.location.hash = "#crew";
  }, []);
  return [route, nav];
}

// ─── Top bar ────────────────────────────────────────────────────────────


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.route = { parseHash, canvasViewForRoute, useHashRoute };
