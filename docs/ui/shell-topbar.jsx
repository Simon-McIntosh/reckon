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

  const ARTIFACT_TABS = window.ReckonShell.route.ARTIFACT_TABS;
  const WORK_TABS = window.ReckonShell.route.WORK_TABS;
  const tabIsActive = (tab) => tab.index.view === view;

  return (
    <>
    <div className="r-topbar">
      <button className="r-topbar-brand" onClick={() => onNav({ view: "cockpit" })} title="reckon · fleet" aria-label="reckon · fleet">
        {window.GLYPHS?.brand}
      </button>
      <button className="r-topbar-search" onClick={onOpenCmdK} title="Search everything · ⌘K">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <circle cx="7" cy="7" r="4.5"/>
          <path d="M13 13l-2.5-2.5"/>
        </svg>
        <span>Search everything</span>
        <span className="kbd">⌘K</span>
      </button>
      <div className="r-tabs-artifact">
        {ARTIFACT_TABS.map(tab => (
          <button key={tab.key} className={`r-tab ${tabIsActive(tab) ? "active" : ""}`} onClick={() => onNav(tab.index)}>
            {tab.label}
          </button>
        ))}
      </div>
      <div className="r-tabs-work">
        {WORK_TABS.map(tab => (
          <button key={tab.key} className={`r-tab ${tabIsActive(tab) ? "active" : ""}`} onClick={() => onNav(tab.index)}>
            {tab.label}
          </button>
        ))}
      </div>
      <div className="r-topbar-spacer"></div>
      <span className="r-live-receipt" title="Live stream from the served state">
        <span className="r-live-receipt-dot" aria-hidden="true"></span>
        <span>live</span>
      </span>
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
