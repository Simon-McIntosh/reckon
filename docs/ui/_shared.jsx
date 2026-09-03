// Shared UI primitives — project picker, sparkline, chips, project card.
// Depends on window.GLYPHS and window.ACCENTS from glyphs.jsx.
// Exposes: window.ProjectPicker, window.ProjectVisibilitySheet, window.Sparkline, window.Chip, window.ProjectCard, window.SettingsMenu

const {useState, useEffect, useRef, useMemo} = React;

function ProjectPicker({current, projects, onNav}) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const cur = projects.find(p => p.project === current);
  const accent = cur ? (cur.accent || window.ACCENTS?.[current] || "var(--accent)") : "var(--ink-2)";
  return (
    <div className={`pick ${open ? "open" : ""}`} ref={ref} onClick={() => setOpen(v => !v)}>
      <span className="pick-mark" style={{color: accent, background: `color-mix(in oklch,${accent} 12%,transparent)`}}>
        {current ? (window.GLYPHS?.[current] || window.GLYPHS?._default) : window.GLYPHS?.fleet}
      </span>
      <span className={`pick-name${current ? "" : " fleet"}`}>{current || "fleet"}</span>
      <span className="pick-caret">▾</span>
      {open && (
        <div className="pick-menu" onClick={e => e.stopPropagation()}>
          <button className={`pick-item${!current ? " active" : ""}`}
                  onClick={() => { setOpen(false); onNav(null); }}>
            <span className="pick-mark" style={{color: "var(--ink-2)", background: "var(--bg-2)"}}>
              {window.GLYPHS?.fleet}
            </span>
            fleet
          </button>
          {projects.length > 0 && <div className="pick-divider"/>}
          {projects.map(p => {
            const pacc = p.accent || window.ACCENTS?.[p.project] || "var(--accent)";
            return (
              <button key={p.project} className={`pick-item${p.project === current ? " active" : ""}`}
                      onClick={() => { setOpen(false); onNav(p.project); }}>
                <span className="pick-mark" style={{color: pacc, background: `color-mix(in oklch,${pacc} 12%,transparent)`}}>
                  {window.GLYPHS?.[p.project] || window.GLYPHS?._default}
                </span>
                {p.project}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProjectVisibilitySheet({open, projects, visibleProjects, onToggleProject, onClose}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose?.(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  const rows = projects || [];
  const visible = new Set((visibleProjects || []).map(project => project.project));
  return (
    <div className="r-visibility-overlay" onClick={onClose}>
      <div className="r-visibility-sheet" role="dialog" aria-labelledby="r-visibility-sheet-title" onClick={e => e.stopPropagation()}>
        <div className="r-visibility-sheet-header">
          <h3 id="r-visibility-sheet-title">Project visibility</h3>
          <span className="settings-project-count">{visible.size} shown · {(projects || []).length} mounted</span>
          <button type="button" className="r-visibility-sheet-close" onClick={onClose}>esc</button>
        </div>
        <p className="r-visibility-sheet-copy">
          Hidden projects leave the picker, the crew feed and the fleet roll-up; registration is unaffected.
        </p>
        <div className="r-visibility-sheet-rows">
          {rows.map(project => {
            const isVisible = visible.has(project.project);
            const locked = isVisible && visible.size === 1;
            const state = locked ? "locked" : isVisible ? "visible" : "hidden";
            return (
              <button
                type="button"
                key={project.project}
                className={`r-visibility-row ${state}`}
                disabled={locked}
                aria-pressed={isVisible}
                onClick={() => onToggleProject(project.project)}
              >
                <span className={`r-visibility-switch ${isVisible ? "on" : ""}`} aria-hidden="true"><i/></span>
                <span className="r-visibility-name">{project.project}</span>
                <span className="r-visibility-counts">{project.plans_count} plans · {project.live_count || 0} live</span>
                <span className="r-visibility-state">{state}</span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function SettingsMenu({theme, setTheme, density, setDensity, projects, visibleProjects, onOpenVisibility, snapshot, onRefresh}) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  return (
    <div className="settings" ref={ref}>
      <button className="icon-btn" onClick={() => setOpen(v => !v)} title="Settings">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <circle cx="12" cy="12" r="3"/>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
      </button>
      {open && (
        <div className="settings-menu">
          <div className="settings-title">Theme</div>
          <button className={`settings-item ${theme === "light" ? "on" : ""}`} onClick={() => setTheme("light")}>Light</button>
          <button className={`settings-item ${theme === "dark" ? "on" : ""}`} onClick={() => setTheme("dark")}>Dark</button>
          <div className="settings-title" style={{marginTop: 6}}>Density</div>
          {["comfortable", "compact", "dense"].map(d => (
            <button key={d} className={`settings-item ${density === d ? "on" : ""}`}
                    onClick={() => setDensity(d)} style={{textTransform: "capitalize"}}>{d}</button>
          ))}
          <div className="settings-title" style={{marginTop: 6}}>Projects</div>
          <div className="settings-project-count">
            {(visibleProjects || []).length} shown · {(projects || []).length} mounted
          </div>
          <button
            type="button"
            className="settings-item"
            onClick={() => { setOpen(false); onOpenVisibility?.(); }}
          >Project visibility…</button>
          {snapshot && (
            <section className="settings-snapshot" aria-labelledby="settings-snapshot-title">
              <div className="settings-title" id="settings-snapshot-title">Snapshot</div>
              <div className="r-snapshot-receipt" role="status">
                <div className="settings-snapshot-field">
                  <span>Source</span>
                  <span>{snapshot.sourceFormat}</span>
                </div>
                <div className="settings-snapshot-field">
                  <span>Resources</span>
                  <span>{snapshot.resourceCount} resources</span>
                </div>
                <div className="settings-snapshot-field">
                  <span>Loaded</span>
                  <span>loaded {snapshot.loadedAt}</span>
                </div>
                <button type="button" className="settings-item" onClick={onRefresh}>Refresh</button>
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function Sparkline({data, accent}) {
  if (!data || data.length < 2) return <svg className="spark" viewBox="0 0 240 32"/>;
  const W = 240, H = 32;
  const max = Math.max(1, ...data);
  const sx = (i) => (i / (data.length - 1)) * W;
  const sy = (v) => H - 2 - (v / max) * (H - 6);
  const path = data.map((v, i) => `${i === 0 ? "M" : "L"}${sx(i)},${sy(v)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="spark" preserveAspectRatio="none">
      <path d={path} stroke={accent} strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

function Chip({count, kind, label}) {
  return (
    <span className={`chip ${kind} ${count === 0 ? "zero" : ""}`}>
      <span className="dot"/>
      <span>{count} {label}</span>
    </span>
  );
}

function ProjectCard({p, onOpen}) {
  const accent = p.accent || window.ACCENTS?.[p.project] || window.ACCENTS?._default || "var(--accent)";
  const implPct = p.plans_count ? Math.round(p.shipped / p.plans_count * 100) : 0;
  const tot = (p.activity30 || []).reduce((a, b) => a + b, 0);
  const last3 = (p.activity30 || []).slice(-3).reduce((a, b) => a + b, 0);
  return (
    <a className="pcard" onClick={() => onOpen(p.project)}>
      <span className="pgl" style={{color: accent, background: `color-mix(in oklch,${accent} 10%,white)`}}>
        {window.GLYPHS?.[p.project] || window.GLYPHS?._default}
      </span>
      <div>
        <div className="pname">{p.project}</div>
        <div className="ptag">
          {p.plans_count} plans · {implPct}% shipped
          {p.last_modified ? ` · last edited ${new Date(p.last_modified).toLocaleDateString("en-US", {month: "short", day: "numeric"})}` : ""}
        </div>
        <div className="chips">
          <Chip count={p.active} kind="active" label="active"/>
          <Chip count={p.blocked} kind="blocked" label="blocked"/>
          <Chip count={p.pending} kind="pending" label="pending"/>
          <Chip count={p.shipped} kind="shipped" label="shipped"/>
        </div>
      </div>
      <div className="spark-wrap">
        <Sparkline data={p.activity30 || []} accent={accent}/>
        <div className="spark-foot">
          <span>30-day activity</span>
          <span>{tot} updates · {last3} last 3d</span>
        </div>
      </div>
      <span className="arr">→</span>
    </a>
  );
}

window.ProjectPicker = ProjectPicker;
window.ProjectVisibilitySheet = ProjectVisibilitySheet;
window.SettingsMenu = SettingsMenu;
window.Sparkline = Sparkline;
window.Chip = Chip;
window.ProjectCard = ProjectCard;
