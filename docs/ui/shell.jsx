// Reckon shell composition root. Feature modules own the routed surfaces.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

function App() {
  const [route, nav] = window.ReckonShell.route.useHashRoute();
  const canvasView = window.ReckonShell.route.canvasViewForRoute(route);
  const PROJECT_VISIBILITY_STORAGE = window.ReckonShell.topbar.PROJECT_VISIBILITY_STORAGE;
  const attachmentGroups = window.ReckonShell.plans.attachmentGroups;
  // Storage keys are project-scoped to prevent cross-project filter contamination.
  const PROJECT = window.STATE?.project || "default";
  const SK = {
    filters:   `reckon:${PROJECT}:filters`,
    shipped:   `reckon:${PROJECT}:showShipped`,
    collapsed: `reckon:${PROJECT}:filtersCollapsed`,
    groupBy:   `reckon:${PROJECT}:groupBy`,
    sortDirs:  `reckon:${PROJECT}:sortDirs`,
    archived:  `reckon:${PROJECT}:showArchived`,
    graphHidden: `reckon:${PROJECT}:planGraphHidden`,
  };
  const [filters, setFilters] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SK.filters) || "{}"); } catch { return {}; }
  });
  const [showShipped, setShowShipped] = useState(() => {
    try { return localStorage.getItem(SK.shipped) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.filters, JSON.stringify(filters)); } catch {}
  }, [filters]);
  useEffect(() => {
    try { localStorage.setItem(SK.shipped, showShipped ? "1" : "0"); } catch {}
  }, [showShipped]);

  // Allow other components (e.g. cockpit milestone tiles) to set filters.
  useEffect(() => {
    const onSet = (e) => setFilters(e.detail || {});
    window.addEventListener("reckon:set-filters", onSet);
    return () => window.removeEventListener("reckon:set-filters", onSet);
  }, []);
  const [promptOpen, setPromptOpen] = useState(false);
  const [filtersHidden, setFiltersHidden] = useState(() => {
    try { return localStorage.getItem(SK.collapsed) === "1"; } catch { return false; }
  });
  const [graphFocal, setGraphFocal] = useState(null);
  // When viewing graph and a plan is clicked in the sidebar, also set graphFocal
  useEffect(() => {
    if (route.view === "plan" && route.slug) setGraphFocal(route.slug);
  }, [route.view, route.slug]);
  // Inline plan-graph: persisted hidden/shown state. Replaces the older
  // Reading/Graph tab toggle — the graph now lives above the report.
  const [graphHidden, setGraphHidden] = useState(() => {
    try { return localStorage.getItem(SK.graphHidden) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.graphHidden, graphHidden ? "1" : "0"); } catch {}
  }, [graphHidden]);
  const [showArchived, setShowArchived] = useState(() => {
    try { return localStorage.getItem(SK.archived) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.archived, showArchived ? "1" : "0"); } catch {}
  }, [showArchived]);
  // Inventory revision — bumped whenever a plan is mutated locally so memoised
  // views (filter list, graph) recompute against the updated inventory record.
  const [invRev, setInvRev] = useState(0);
  const bumpInv = useCallback(() => setInvRev(r => r + 1), []);
  const refreshProjectState = useCallback(async () => {
    await window.revalidateProjectState?.();
    bumpInv();
  }, [bumpInv]);
  useEffect(() => {
    const changes = window.watchProjectStateChanges?.(refreshProjectState);
    return () => changes?.close();
  }, [refreshProjectState]);
  useEffect(() => {
    try { localStorage.setItem(SK.collapsed, filtersHidden ? "1" : "0"); } catch {}
  }, [filtersHidden]);
  const [groupBy, setGroupBy] = useState(() => {
    try {
      const stored = localStorage.getItem(SK.groupBy);
      if (!window.ReckonShell.plans.SORT_OPTIONS.some(option => option.value === stored)) return "edited";
      return stored;
    } catch { return "edited"; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.groupBy, groupBy); } catch {}
  }, [groupBy]);
  const [sortDirs, setSortDirs] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SK.sortDirs) || "{}"); } catch { return {}; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.sortDirs, JSON.stringify(sortDirs)); } catch {}
  }, [sortDirs]);
  const sortDir = sortDirs[groupBy] ?? window.ReckonShell.plans.SORT_DIR_DEFAULTS[groupBy] ?? "asc";
  const toggleSortDir = () => {
    const next = sortDir === "asc" ? "desc" : "asc";
    setSortDirs(prev => ({ ...prev, [groupBy]: next }));
  };
  const [cmdKOpen, setCmdKOpen] = useState(false);
  const [readingMode, setReadingMode] = useState(false);

  const [projects, setProjects] = useState([]);
  const [fleetRuns, setFleetRuns] = useState([]);
  const [hiddenProjects, setHiddenProjects] = useState(() => {
    try {
      const raw = localStorage.getItem(PROJECT_VISIBILITY_STORAGE);
      if (raw === null) return null;
      const stored = JSON.parse(raw);
      return Array.isArray(stored) ? stored : null;
    } catch { return null; }
  });
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetch("/_projects/index.json").then(response => response.ok ? response.json() : null),
      fetch("/crew")
        .then(response => response.ok ? response.json() : null)
        .catch(() => ({ runs: [] })),
    ])
      .then(async ([data, crew]) => {
        if (cancelled) return;
        setFleetRuns(Array.isArray(crew?.runs) ? crew.runs : []);
        if (!data?.projects) return;
        const discoveries = await Promise.all(data.projects.map(project =>
          fetch(`/_discover/${project.project}`)
            .then(response => response.ok ? response.json() : {})
            .catch(() => ({}))
        ));
        if (cancelled) return;
        const liveCounts = (crew?.runs || []).reduce((counts, run) => {
          counts.set(run.project, (counts.get(run.project) || 0) + 1);
          return counts;
        }, new Map());
        setProjects(data.projects.map((project, index) => {
          const state = project.data || {};
          const summary = Array.isArray(state.projects) ? state.projects[0] : null;
          const discovery = discoveries[index] || {};
          const inventory = Array.isArray(discovery.inventory) ? discovery.inventory : [];
          const plans = Array.isArray(state.plans) ? state.plans : inventory;
          return {
            project: project.project,
            accent: summary?.accent || state.accent || window.ACCENTS?.[project.project] || "var(--accent)",
            plans_count: Number(summary?.plans_count ?? state.counts?.total ?? plans.length ?? 0),
            live: liveCounts.has(project.project),
            live_count: liveCounts.get(project.project) || 0,
            active: Number(summary?.active || 0),
            blocked: Number(summary?.blocked || 0),
            pending: Number(summary?.pending || 0),
            shipped: Number(summary?.shipped || 0),
            last_edited: summary?.last_edited || summary?.last_modified || "",
            activity30: Array.isArray(summary?.activity30) ? summary.activity30 : [],
            active_sprint: summary?.active_sprint || null,
            artifacts: inventory,
            state: discovery,
          };
        }));
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const refreshRuns = () => fetch("/crew")
      .then(response => response.ok ? response.json() : null)
      .then(crew => { if (Array.isArray(crew?.runs)) setFleetRuns(crew.runs); })
      .catch(() => {});
    const timer = window.setInterval(refreshRuns, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("reckon:theme") || "light"; } catch { return "light"; }
  });
  const [density, setDensity] = useState(() => {
    try { return localStorage.getItem("reckon:density") || "comfortable"; } catch { return "comfortable"; }
  });
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("reckon:theme", theme); } catch {}
  }, [theme]);
  useEffect(() => {
    document.documentElement.setAttribute("data-density", density);
    try { localStorage.setItem("reckon:density", density); } catch {}
  }, [density]);

  const navProject = useCallback((destProject) => {
    if (!destProject) {
      nav({ view: "home" });
      return;
    }
    const M = window.STATE;
    const currentProject = M?.project || null;
    const isFromProject = !!currentProject;
    let hash = "#home";
    if (isFromProject) {
      if (route.view === "graph") hash = "#graph";
      else if (route.view === "crew") hash = "#crew";
      else if (route.view === "plan") hash = "#plans";
      else if (route.view === "sprint") hash = "#sprints";
      else if (route.view === "home") hash = "#home";
      else hash = "#cockpit";
    }
    window.location.href = `/${destProject}/${hash}`;
  }, [route]);

  const toggleProject = useCallback((targetProject) => {
    const currentProject = window.STATE?.project || null;
    const change = window.ReckonShell.topbar.projectVisibilityChange(projects, hiddenProjects, currentProject, targetProject);
    if (!change.changed) return;
    setHiddenProjects(change.hidden);
    try { localStorage.setItem(PROJECT_VISIBILITY_STORAGE, JSON.stringify(change.hidden)); } catch {}
    if (change.focus && change.focus !== currentProject) navProject(change.focus);
  }, [projects, hiddenProjects, navProject]);

  const M = window.STATE;
  const items = useMemo(() => {
    if (!M) return [];
    let list = M.inventory.filter(item => (item.type || "plan") === "plan");
    // Archived axis is orthogonal to status. Hide by default unless the user
    // toggled it on OR is explicitly filtering for a specific status.
    if (!showArchived) {
      list = list.filter(p => !(p.archived === "1" || p.archived === true || p.archived === "true"));
    }
    if (filters.status?.length) list = list.filter(p => filters.status.includes(p.effective_status || p.status));
    if (filters.ms?.length) list = list.filter(p => filters.ms.includes(p.ms));
    if (filters.sprint?.length) list = list.filter(p => filters.sprint.includes(p.sprint));
    if ((M.north_stars || []).length && filters.north_star?.length) list = list.filter(p => filters.north_star.includes(p.north_star));
    if (filters.context) {
      const ctx = M.inventory.find(p => p.slug === filters.context);
      if (ctx) {
        const related = new Set([ctx.slug, ...(ctx.depends_on || []), ...(ctx.blocks || [])]);
        list = list.filter(p => related.has(p.slug));
      }
    }
    return list;
    // invRev is included to recompute when a plan's archived/status flips
    // without requiring a full inventory reload.
  }, [M, filters, showArchived, invRev]);

  const readQueue = useMemo(
    () => window.ReckonShell.plans.readingQueue(M, items, groupBy, sortDir),
    [M, items, groupBy, sortDir]
  );
  const searchItems = useMemo(() => window.ReckonShell.plans.paletteItems(M, projects), [M, projects]);
  const shownProjects = useMemo(() => window.ReckonShell.topbar.visibleProjectRows(projects, hiddenProjects), [projects, hiddenProjects]);
  const shownProjectNames = useMemo(() => shownProjects.map(project => project.project), [shownProjects]);
  const readPosition = Math.max(0, readQueue.indexOf(route.slug));

  const onSelectPlan = useCallback((slug) => {
    nav({ view: "plan", slug });
  }, [nav]);

  const onSetContext = useCallback((slug) => {
    setFilters(f => ({ ...f, context: slug }));
  }, []);

  useEffect(() => {
    if (!promptOpen) return;
    setPromptOpen(false);
    window.dispatchEvent(new CustomEvent("r-open-prompt"));
  }, [promptOpen]);

  // Cmd/Ctrl+B hides both list columns. Focus keys share this lifecycle so
  // selection and palette state stay owned by App.
  useEffect(() => {
    const onKey = (e) => {
      const editable = e.target?.matches?.("input, textarea, select, [contenteditable='true']");
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setCmdKOpen(true);
        return;
      }
      if (e.key === "b" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (route.view === "plan") setFiltersHidden(c => !c); // Plans only — hides the plan list.
        return;
      }
      const canRead = route.view === "plan" && !!route.slug;
      if (e.key === "Escape" && readingMode) {
        e.preventDefault();
        setReadingMode(current => window.ReckonShell.plans.nextReadingMode(current, e.key, canRead));
        return;
      }
      if (editable || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key.toLowerCase() === "f" && canRead) {
        e.preventDefault();
        setReadingMode(current => window.ReckonShell.plans.nextReadingMode(current, e.key, canRead));
        return;
      }
      if (readingMode && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
        e.preventDefault();
        const next = window.ReckonShell.plans.readingQueueStep(readQueue, route.slug, e.key === "ArrowRight" ? 1 : -1);
        if (next) nav({ view: "plan", slug: next });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [nav, readQueue, readingMode, route.slug, route.view]);

  useEffect(() => {
    if (route.view !== "plan" || !route.slug) setReadingMode(false);
  }, [route.view, route.slug]);

  return (
    <div className={`r-app ${readingMode ? "r-focus-mode" : ""}`}>
      {!readingMode && <window.ReckonShell.topbar.TopBar
          route={route}
          onNav={to => nav(to.view === "cockpit" ? { view: "home" } : to)}
          navProject={navProject}
          onOpenCmdK={() => setCmdKOpen(true)}
          filtersHidden={filtersHidden}
          onToggleFilters={() => setFiltersHidden(c => !c)}
          theme={theme}
          setTheme={setTheme}
          density={density}
          setDensity={setDensity}
          projects={projects}
          hiddenProjects={hiddenProjects}
          onToggleProject={toggleProject}
          onRefresh={refreshProjectState}
        />}
      {canvasView === "plan" ? (
        <div className={`r-canvas-view r-plans-view ${filtersHidden || readingMode ? "filters-collapsed" : ""} ${readingMode ? "reading-mode" : ""}`}>
          {!readingMode && <window.ReckonShell.plans.ListCol route={route} onNav={nav} onSelectPlan={onSelectPlan} items={items} sortBy={groupBy} setSortBy={setGroupBy} sortDir={sortDir} toggleSortDir={toggleSortDir} filters={filters} setFilters={setFilters} onClearFilters={() => setFilters({})} onClearContext={() => setFilters(f => { const next = {...f}; delete next.context; return next; })} onSetContext={onSetContext} />}
          <div className="r-content" style={readingMode ? { height: "100vh", overflow: "auto" } : undefined}>
            {!readingMode && <window.ReckonShell.title.TitleBar route={route} onNav={nav} onOpenPrompt={() => setPromptOpen(true)} onPlanMutated={bumpInv} />}
            <div className="r-reader-with-attachments" style={readingMode ? { display: "block" } : undefined}>
              <div className="r-body">
                {!readingMode && <window.ReckonShell.plans.PlanGraphStrip slug={route.slug} onNav={nav} hidden={graphHidden} setHidden={setGraphHidden} />}
                <window.Plan
                slug={route.slug}
                onNav={nav}
                attachmentGroups={attachmentGroups(M, route.slug)}
                focusMode={readingMode}
                onToggleFocus={() => setReadingMode(current => !current)}
                focusPosition={{ current: readPosition + 1, total: readQueue.length }}
                onPage={(direction) => {
                  const next = window.ReckonShell.plans.readingQueueStep(readQueue, route.slug, direction);
                  if (next) nav({ view: "plan", slug: next });
                }}
                />
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className={`r-canvas-view r-${canvasView}-view`}>
          <div className="r-content">
            <window.ReckonShell.title.TitleBar route={route} onNav={nav} onOpenPrompt={() => setPromptOpen(true)} onPlanMutated={bumpInv} />
            <div className={`r-reader-with-attachments ${route.view === "cockpit" ? "r-overview-container" : ""}`}>
              <div className={`r-body ${route.view === "cockpit" ? "r-overview-view" : ""}`}>
                {canvasView === "home" && <window.ReckonShell.home.FleetHome projects={shownProjects} fleetRuns={window.ReckonShell.home.homeVisibleRuns(fleetRuns, shownProjects)} mountedProjectCount={projects.length} onConfigureVisibility={() => document.querySelector(".r-project-configure")?.click()} />}
                {canvasView === "cockpit" && <window.ReckonShell.overview.CockpitBody onNav={nav} projects={shownProjects} fleetRuns={fleetRuns} mountedProjectCount={projects.length} />}
                {canvasView === "sprint" && <><window.ReckonShell.prompt.FleetPrompt sprintId={route.sprint} /><window.Sprint sprintId={route.sprint} onNav={nav} /></>}
                {canvasView === "graph" && <window.GraphView onNav={nav} items={items} focal={graphFocal} setFocal={setGraphFocal} />}
                {canvasView === "crew" && <window.CrewView visibleProjects={shownProjectNames} mountedProjectCount={projects.length} selectedProject={M?.project || null} />}
              </div>
            </div>
          </div>
        </div>
      )}
      {cmdKOpen && <window.ReckonShell.palette.CmdKPalette items={searchItems} onClose={() => setCmdKOpen(false)} onPick={(result) => {
        setCmdKOpen(false);
        if (result.repository && result.repository !== M?.project) {
          window.location.href = `/${result.repository}/#plan/${encodeURIComponent(result.nav_key)}`;
        } else {
          nav({ view: "plan", slug: result.nav_key });
        }
      }} />}
    </div>
  );
}


const ShellReadyGate = window.ReckonShell?.ready?.ReadyGate;
const shellRoot = ShellReadyGate
  ? React.createElement(ShellReadyGate, null, React.createElement(App))
  : React.createElement("main", { "data-shell-modules": "unavailable" });

ReactDOM.createRoot(document.getElementById("root")).render(shellRoot);
