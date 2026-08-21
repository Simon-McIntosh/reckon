// Sprint view — one focused execution surface.
// One clear goal block, a switcher, and a read-only kanban.
// No "what is a sprint" explainer, no past-sprints accordion.

function Sprint({ sprintId, onNav }) {
  const M = window.STATE;
  if (!M) return null;
  const allSprints = M.sprints || [];

  const idx = useMemo(() => {
    const i = allSprints.findIndex(s => s.id === sprintId);
    return i >= 0 ? i : allSprints.findIndex(s => s.id === M.active_sprint_id);
  }, [sprintId, allSprints]);

  const sprint = allSprints[idx];
  if (!sprint) return <div className="r-page">No sprint.</div>;

  const [showSprintPrompt, setShowSprintPrompt] = useState(false);
  const [sprintPromptText, setSprintPromptText] = useState(null);
  const [liveRuns, setLiveRuns] = useState([]);
  const project = M.project || document.querySelector('meta[name="docs-project"]')?.content || "";

  useEffect(() => {
    if (!project) { setLiveRuns([]); return; }
    let active = true;
    const poll = async () => {
      try {
        const response = await fetch(`/crew/${encodeURIComponent(project)}`, { cache: "no-store" });
        if (!response.ok) return;
        const payload = await response.json();
        if (active && Array.isArray(payload.runs)) setLiveRuns(payload.runs);
      } catch (_) { /* Sprint navigation remains available without live state. */ }
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [project]);

  // Materialise items with their plan info and sprint-level contract.
  const items = sprint.items.map(it => {
    const slug = typeof it === "string" ? it : it.slug;
    const whyNow = typeof it === "object" ? it.why_now : null;
    const doneWhen = typeof it === "object" ? it.done_when : null;
    const plan = M.inventory.find(p => p.slug === slug);
    return plan ? { ...plan, whyNow, doneWhen } : null;
  }).filter(Boolean);

  // Build the fleet prompt via the shared builder, hydrating each plan's live
  // state first (the inventory has no decisions/followups).
  useEffect(() => {
    if (!showSprintPrompt) { setSprintPromptText(null); return; }
    let alive = true;
    const win = (sprint.starts || "") + (sprint.ends ? " → " + sprint.ends : "");
    const opts = { sprint: { id: sprint.id, window: win } };
    Promise.resolve(
      window.buildFleetPromptAsync
        ? window.buildFleetPromptAsync(items, window.STATE, sprint.theme, opts)
        : window.buildFleetPrompt(items, window.STATE, sprint.theme, opts)
    ).then(t => { if (alive) setSprintPromptText(t); });
    return () => { alive = false; };
  }, [showSprintPrompt, sprint.id]);

  const STATUS_TO_COL = { pending: "todo", draft: "todo", active: "doing", blocked: "doing", in_progress: "doing", shipped: "done", done: "done" };
  const gateSummary = (gates) => {
    const rows = gates || [];
    if (rows.length === 0) return "—";
    const passed = rows.filter(g => g.passed || g.verdict === "passed").length;
    return `${passed}/${rows.length} passed`;
  };

  const cols = useMemo(() => {
    const g = { todo: [], doing: [], done: [] };
    for (const p of items) {
      const col = STATUS_TO_COL[p.status] || "doing";
      g[col].push(p);
    }
    return g;
  }, [items]);
  const runsByPlan = useMemo(() => {
    const grouped = {};
    for (const run of liveRuns) {
      if (!run.plan) continue;
      (grouped[run.plan] ||= []).push(run);
    }
    return grouped;
  }, [liveRuns]);

  return (
    <div className="r-page wide">
      <div className="r-sp-head">
        <div className="r-eyebrow">Sprint</div>
        <div className="r-sp-switcher">
          <button className="nav-btn" disabled={idx <= 0} onClick={() => onNav({ view: "sprint", sprint: allSprints[idx - 1].id })}>←</button>
          <div className="current">
            <span className="id">{sprint.id}</span>
            <span className={`st ${sprint.status}`}>{sprint.status}</span>
          </div>
          <button className="nav-btn" disabled={idx >= allSprints.length - 1} onClick={() => onNav({ view: "sprint", sprint: allSprints[idx + 1].id })}>→</button>
          <span className="range">{sprint.starts} → {sprint.ends}</span>
        </div>
        <button
          className="gen-prompt"
          onClick={() => setShowSprintPrompt(true)}
          title="Generate fleet prompt for this sprint"
          style={{ marginLeft: "auto" }}
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M4 3h6l2 2v8H4z"/>
            <path d="M10 3v2h2"/>
            <path d="M6 7h4M6 9h4M6 11h2"/>
          </svg>
          Generate prompt
        </button>
      </div>

      <div className="r-sp-goal">
        <div className="lbl">Goal</div>
        <div className="theme">{sprint.theme}</div>
        {sprint.summary && <div className="summary">{sprint.summary}</div>}
      </div>

      <div className="r-kanban">
        {[
          { id: "todo",  title: "To do",  cards: cols.todo  },
          { id: "doing", title: "Doing",  cards: cols.doing },
          { id: "done",  title: "Done",   cards: cols.done  },
        ].map(col => (
          <div
            key={col.id}
            className="r-col"
          >
            <div className="col-h">
              <span>{col.title}</span>
              <span className="n">{col.cards.length}</span>
            </div>
            {col.cards.map(p => {
              const itemRuns = runsByPlan[p.slug] || [];
              const liveSummary = itemRuns
                .map(run => `${run.member || "unassigned"} · ${run.section || "whole plan"}`)
                .join("; ");
              return (
                <a
                  key={p.slug}
                  className={`r-kcard ${p.status === "blocked" ? "blocked" : ""}`}
                  href={`#plan/${p.slug}`}
                >
                <div className="t">
                  {p.title}
                  {itemRuns.length > 0 && (
                    <span
                      className="r-inflight-badge"
                      aria-label={`${itemRuns.length} live ${itemRuns.length === 1 ? "run" : "runs"}`}
                      title={liveSummary}
                      style={{
                        float: "right",
                        marginLeft: 8,
                        color: "var(--accent)",
                        fontFamily: "var(--mono)",
                        fontSize: 10,
                        fontWeight: 500,
                      }}
                    >
                      ● in flight{itemRuns.length > 1 ? ` · ${itemRuns.length}` : ""}
                    </span>
                  )}
                </div>
                <div className="slug">/{p.slug}</div>
                {p.whyNow && <div className="just"><strong>Why now:</strong> {p.whyNow}</div>}
                {p.doneWhen && <div className="just"><strong>Done when:</strong> {p.doneWhen}</div>}
                <div className="progress">
                  <span className="bar"><i className={p.status === "shipped" ? "shipped" : p.status === "blocked" ? "blocked" : ""} style={{ width: `${Math.round((p.impl || 0) * 100)}%` }}></i></span>
                  <span style={{ minWidth: 28, textAlign: "right" }}>{Math.round((p.impl || 0) * 100)}%</span>
                </div>
                <div className="row">
                  <span className={`status ${p.status}`}><span className="dot"></span>{p.status}</span>
                  <span>·</span>
                  <span>{p.ms}</span>
                  {(p.dec_open || 0) > 0 && <><span>·</span><span style={{ color: "var(--warn)" }}>D {p.dec_open}</span></>}
                  {(p.blockers || 0) > 0 && <><span>·</span><span style={{ color: "var(--bad)" }}>! {p.blockers}</span></>}
                </div>
                <div className="row r-gate-column">
                  <span>Gates</span>
                  <span>{gateSummary(p.gates)}</span>
                </div>
                </a>
              );
            })}
            {col.cards.length === 0 && (
              <div style={{ textAlign: "center", padding: "20px 0", color: "var(--muted)", fontSize: 12 }}>
                No items
              </div>
            )}
          </div>
        ))}
      </div>

      {showSprintPrompt && window.reckon?.PromptModal && sprintPromptText != null && (
        <window.reckon.PromptModal
          planSlug={`sprint-${sprint.id}`}
          initialPrompt={sprintPromptText}
          onClose={() => setShowSprintPrompt(false)}
        />
      )}
    </div>
  );
}

// SprintView is an alias for backward-compat with shell.jsx which calls <SprintView>.
const SprintView = Sprint;

window.Sprint = Sprint;
window.SprintView = SprintView;
