// Reckon shell topbar module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

const PROJECT_VISIBILITY_STORAGE = "reckon:hidden-projects";

function mountedProjectRows(projects) {
  return projects || [];
}

function manageableProjectRows(projects) {
  return projects || [];
}

function effectiveHiddenProjects(projects, hiddenProjects) {
  if (Array.isArray(hiddenProjects)) return hiddenProjects;
  return [];
}

function visibleProjectRows(projects, hiddenProjects) {
  const hidden = new Set(effectiveHiddenProjects(projects, hiddenProjects));
  const mounted = mountedProjectRows(projects);
  if (mounted.length && mounted.every(project => hidden.has(project.project))) {
    hidden.delete(mounted[0].project);
  }
  return mounted.filter(project => !hidden.has(project.project));
}

function projectVisibilityChange(projects, hiddenProjects, focusedProject, targetProject) {
  const hidden = new Set(effectiveHiddenProjects(projects, hiddenProjects));
  if (hidden.has(targetProject)) {
    hidden.delete(targetProject);
    return { changed: true, locked: false, hidden: [...hidden], focus: focusedProject };
  }
  const survivors = visibleProjectRows(projects, hiddenProjects)
    .filter(project => project.project !== targetProject);
  if (!survivors.length) {
    return { changed: false, locked: true, hidden: [...hidden], focus: focusedProject };
  }
  hidden.add(targetProject);
  return {
    changed: true,
    locked: false,
    hidden: [...hidden],
    focus: focusedProject === targetProject ? survivors[0].project : focusedProject,
  };
}

function snapshotTime(loadedAt) {
  if (!loadedAt) return "unknown time";
  const loaded = new Date(loadedAt);
  if (Number.isNaN(loaded.getTime())) return "unknown time";
  return loaded.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function snapshotReceipt(state) {
  return {
    sourceFormat: state?.source_format || "unknown source",
    resourceCount: Object.keys(state?.resource_versions || {}).length,
    loadedAt: snapshotTime(state?.loaded_at),
  };
}

function TopBar({ route, onNav, navProject, onOpenCmdK, filtersHidden, onToggleFilters, theme, setTheme, density, setDensity, projects, hiddenProjects, onToggleProject, onRefresh }) {
  const M = window.STATE;
  const view = route.view;
  const currentProject = M?.project
    || (typeof document !== "undefined" && document.querySelector('meta[name="docs-project"]')?.content)
    || null;

  const [visibilitySheetOpen, setVisibilitySheetOpen] = useState(false);
  const mountedProjects = mountedProjectRows(projects);
  const manageableProjects = manageableProjectRows(projects);
  const visibleProjects = visibleProjectRows(projects, hiddenProjects);
  const current = manageableProjects.find(project => project.project === currentProject);
  const snapshot = snapshotReceipt(M);

  // Assign the window globals to local vars so JSX can use them as components.
  const SM = window.SettingsMenu;
  const VS = window.ProjectVisibilitySheet;
  const openVisibilitySheet = () => {
    document.querySelector(".r-project-manage")?.removeAttribute("open");
    setVisibilitySheetOpen(true);
  };

  const goPlans = () => {
    const target = M?.inventory?.find(p => p.status === "active") || M?.inventory?.[0];
    if (target) onNav({ view: "plan", slug: target.nav_key || target.slug });
  };
  const goSprints = () => {
    const id = M?.active_sprint_id || M?.sprint?.id || M?.sprints?.[0]?.id;
    if (id) onNav({ view: "sprint", sprint: id });
  };

  return (
    <>
    <div className="r-topbar">
      <button className="r-topbar-brand" onClick={() => navProject(null)} title="All projects">
        <span className="r-topbar-mark">r</span>
        <span>reckon</span>
      </button>
      <details className="r-project-manage">
        <summary>
          <span className={`r-live-dot ${current?.live ? "is-live" : ""}`} aria-hidden="true"></span>
          <span>{currentProject || "Fleet"}</span>
          <span className="r-project-caret" aria-hidden="true">▾</span>
        </summary>
        <div className="r-project-menu">
          <div className="r-project-menu-count">{visibleProjects.length} shown · {manageableProjects.length} mounted</div>
          {visibleProjects.map(project => (
            <button
              type="button"
              key={project.project}
              className={project.project === currentProject ? "active" : ""}
              onClick={() => navProject(project.project)}
              aria-current={project.project === currentProject ? "page" : undefined}
            >
              <span className={`r-live-dot ${project.live ? "is-live" : ""}`} aria-hidden="true"></span>
              <strong>{project.project}</strong>
              <span>{project.plans_count} plans</span>
              <span>{project.live_count || 0} live</span>
            </button>
          ))}
          <button
            type="button"
            className="r-project-configure"
            onClick={openVisibilitySheet}
          >Configure visibility…</button>
        </div>
      </details>
      <div className="r-glyph-tabs">
        <button className={`r-glyph ${view === "plan" ? "active" : ""}`} onClick={goPlans} title="Plans">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M3 4h10M3 8h10M3 12h7"/>
          </svg>
          Plans
        </button>
        <button className={`r-glyph ${view === "sprint" ? "active" : ""}`} onClick={goSprints} title="Sprints">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <rect x="2.5" y="3" width="3" height="10" rx="0.6"/>
            <rect x="6.5" y="3" width="3" height="10" rx="0.6"/>
            <rect x="10.5" y="3" width="3" height="10" rx="0.6"/>
          </svg>
          Sprints
        </button>
        <button className={`r-glyph ${view === "graph" ? "active" : ""}`} onClick={() => onNav({ view: "graph" })} title="Graph">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="3.5" cy="4" r="1.5"/>
            <circle cx="3.5" cy="12" r="1.5"/>
            <circle cx="12.5" cy="8" r="1.5"/>
            <path d="M5 4l6 3.5M5 12l6-3.5"/>
          </svg>
          Graph
        </button>
        <button className={`r-glyph ${view === "crew" ? "active" : ""}`} onClick={() => onNav({ view: "crew" })} title="Crew">
          {window.GLYPHS?.crew}
          Crew
        </button>
      </div>
      <div className="top-r">
        <button className="r-cmdk-trigger" onClick={onOpenCmdK} title="Search plans · ⌘K">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <circle cx="7" cy="7" r="4.5"/>
            <path d="M13 13l-2.5-2.5"/>
          </svg>
          <span>Search</span>
          <span className="kbd">⌘K</span>
        </button>
        {SM ? (
          <SM
            theme={theme}
            setTheme={setTheme}
            density={density}
            setDensity={setDensity}
            projects={manageableProjects.map(project => project)}
            visibleProjects={visibleProjects}
            onOpenVisibility={openVisibilitySheet}
            snapshot={snapshot}
            onRefresh={onRefresh}
          />
        ) : null}
        {view === "plan" && (
          <button
            className="icon-btn"
            onClick={onToggleFilters}
            title={`${filtersHidden ? "Show" : "Hide"} plan list · ⌘B`}
            aria-pressed={!filtersHidden}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <rect x="2" y="3" width="12" height="10" rx="1.5"/>
              <path d="M6 3v10"/>
            </svg>
          </button>
        )}
      </div>
    </div>
    {VS ? (
      <VS
        open={visibilitySheetOpen}
        projects={manageableProjects}
        visibleProjects={visibleProjects}
        onToggleProject={onToggleProject}
        onClose={() => setVisibilitySheetOpen(false)}
      />
    ) : null}
    </>
  );
}

// ─── Plan-list filters ──────────────────────────────────────────────────


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.topbar = { PROJECT_VISIBILITY_STORAGE, mountedProjectRows, manageableProjectRows, effectiveHiddenProjects, visibleProjectRows, projectVisibilityChange, snapshotTime, snapshotReceipt, TopBar };
