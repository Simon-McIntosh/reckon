// Plan view — reading-first document with inline decisions and select-to-comment.
// No internal topbar/breadcrumb because the sidebar handles nav.
//
// Content source priority:
//   1. Fetch /<project>/<slug>.html and render the <article class="plan-doc">
//      body directly — this preserves all HTML formatting (tables, code, lists).
//   2. Fall back to GenericBody (data-driven from state sections[]) if no HTML.
//
// Interactive overlay: after rendering the HTML, locked decision values from
// state JSON are injected into .dec-choice[data-key] cells in the decisions
// table. Followups and the resolved-log always come from state JSON (below the
// HTML body).

function Plan({ slug, onNav }) {
  const M = window.STATE;
  if (!M) return null;
  const PG = M.plans[slug];
  if (!PG) return <div className="r-page">Plan "{slug}" not found.</div>;

  const P = PG;
  const isResearch = PG.type === "research";

  const stored = planLoad(slug) || {};
  // The inventory is lightweight; full per-doc state (decisions, followups) is
  // fetched from /plan/<project>/<slug> when the doc opens.
  const [decs, setDecs] = useState([]);
  const [fullState, setFullState] = useState(null);
  const [showPrompt, setShowPrompt] = useState(false);
  const [composingAt, setComposingAt] = useState(null);
  const [viewMode, setViewMode] = useState("reading");
  useEffect(() => { setViewMode("reading"); }, [slug]);

  // ── Fetch HTML plan body ────────────────────────────────────────────────
  const [planHtml, setPlanHtml] = useState(null);
  const [htmlReady, setHtmlReady] = useState(false);
  const htmlRef = useRef(null);

  useEffect(() => {
    setHtmlReady(false);
    setPlanHtml(null);
    const project = M.project || document.querySelector('meta[name="docs-project"]')?.content || "";
    if (!project) { setHtmlReady(true); return; }

    // Use plan's href (may include subdir e.g. "curated/slug") if available
    const href = PG.href || slug;
    fetch(`/${project}/${href}.html`, { cache: "no-store" })
      .then(r => r.ok ? r.text() : null)
      .then(html => {
        if (!html) { setHtmlReady(true); return; }
        const doc = new DOMParser().parseFromString(html, "text/html");
        // Accept both reckon-style (.plan-doc) and legacy project-style (main.page / main)
        const article = doc.querySelector(".plan-doc") || doc.querySelector("main.page") || doc.querySelector("main");
        if (!article) { setHtmlReady(true); return; }
        // Remove chrome that the SPA provides itself
        article.querySelector(".topbar")?.remove();
        article.querySelector("header.plan-header")?.remove();
        article.querySelector("nav.plan-nav")?.remove();
        // The decisions section is re-rendered interactively below from parsed
        // state; drop the static copy so it isn't shown twice. Followups /
        // questions / comments stay as readable prose.
        article.querySelector('section[data-reckon="decisions"]')?.remove();
        // Strip inline scripts — they target the standalone page, not the SPA
        article.querySelectorAll("script").forEach(s => s.remove());
        setPlanHtml(article.innerHTML);
        setHtmlReady(true);
      })
      .catch(() => setHtmlReady(true));
  }, [slug]);

  // ── Fetch full doc state (decisions, followups) ─────────────────────────
  useEffect(() => {
    setFullState(null);
    setDecs([]);
    const project = M.project || document.querySelector('meta[name="docs-project"]')?.content || "";
    if (!project) return;
    fetch(`/plan/${project}/${encodeURIComponent(slug)}`, { cache: "no-store" })
      .then(r => r.ok ? r.json() : null)
      .then(rec => {
        if (!rec) return;
        setFullState(rec);
        const overlay = (planLoad(slug) || {}).decisions || {};
        setDecs((rec.decisions || []).map(d => {
          const o = overlay[d.key];
          return o && o.choice
            ? { ...d, chosen: o.choice, choice: o.choice, rationale: o.rationale, when: o.when, by: o.by }
            : d;
        }));
      })
      .catch(() => {});
  }, [slug]);

  // ── Comment / prompt wiring ─────────────────────────────────────────────
  useEffect(() => {
    const open = () => {
      const openDecs = decs.filter(d => !d.chosen && !d.choice);
      if (openDecs.length > 0) {
        if (window.flashSaved) window.flashSaved(`✗ ${openDecs.length} open decision${openDecs.length === 1 ? "" : "s"} — resolve them first`);
        return;
      }
      setShowPrompt(true);
    };
    window.addEventListener("r-open-prompt", open);
    return () => window.removeEventListener("r-open-prompt", open);
  }, [decs]);

  const initialComments = (stored.comments) || P.comments || {};
  const [comments, setComments] = useState(initialComments);

  const articleRef = useRef(null);
  const [sel, clearSel] = window.reckon.useSelectionToComment(articleRef, slug);

  const author = window.STATE?.projects?.[0]?.owner || "user";

  const onUpdateDec = (key, choice, rationale) => {
    const now = new Date().toISOString().slice(0, 16).replace("T", " ");
    setDecs(arr => arr.map(x => x.key === key ? { ...x, chosen: choice || "", choice: choice || "", rationale, when: now, by: author } : x));
    // Dotted sub-keys so the server merges into the decision WITHOUT dropping
    // its authored title/context/choices.
    planSave(slug, {
      [`decisions.${key}.choice`]:    choice || "",
      [`decisions.${key}.rationale`]: rationale,
      [`decisions.${key}.when`]:      now,
      [`decisions.${key}.by`]:        author,
    });
    if (window.flashSaved) window.flashSaved(`${slug}.${key} → ${choice || "rationale saved"}`);
  };

  const addComment = (sectionId, body, quote) => {
    if (!body || !body.trim()) return;
    const now = new Date().toISOString().slice(0, 16).replace("T", " ");
    const c = { id: `c-${Date.now()}`, who: author, when: now, body: body.trim(), ...(quote ? { quote } : {}) };
    const next = { ...comments, [sectionId]: [...(comments[sectionId] || []), c] };
    setComments(next);
    planSave(slug, { [`comments.${sectionId}`]: next[sectionId] });
    if (window.flashSaved) window.flashSaved(`${slug}.comments.${sectionId} +1`);
  };

  return (
    <div className="r-page">
      <div className="r-plan-mode-toggle">
        <button className={`r-mode-pill${viewMode === "reading" ? " active" : ""}`} onClick={() => setViewMode("reading")}>Reading</button>
        <button className={`r-mode-pill${viewMode === "graph" ? " active" : ""}`} onClick={() => setViewMode("graph")}>Graph</button>
      </div>

      {viewMode === "graph" ? (
        window.RadialFan
          ? <window.RadialFan focalSlug={slug} onNav={onNav} />
          : <div style={{ padding: 24, color: "var(--muted)" }}>Graph view loading…</div>
      ) : (
        <article className="r-reading" ref={articleRef}>
          {isResearch && (
            <div className="r-research-banner">
              <span className="r-type-tag research">research</span>
              {(PG.informs || []).length > 0 && (
                <span className="informs">informs&nbsp;
                  {PG.informs.map((s, i) => (
                    <React.Fragment key={s}>
                      {i > 0 && ", "}
                      <a href={`#plan/${s}`}>{s}</a>
                    </React.Fragment>
                  ))}
                </span>
              )}
            </div>
          )}
          {!htmlReady ? (
            <div style={{ padding: 24, color: "var(--muted)", fontSize: 13 }}>Loading…</div>
          ) : planHtml ? (
            <>
              {/* Render HTML plan body — preserves all formatting from the authored doc */}
              <div ref={htmlRef} className="r-plan-html" dangerouslySetInnerHTML={{ __html: planHtml }} />

              {/* Interactive decisions — rendered from the doc's parsed state */}
              {decs.length > 0 && (
                <>
                  <h2 id="decisions"><span className="sec">§</span>Decisions</h2>
                  {decs.map(d => (
                    <Decision key={d.key} d={d} onUpdate={(choice, rat) => onUpdateDec(d.key, choice, rat)} />
                  ))}
                  <SectionComments comments={comments["decisions"]} />
                </>
              )}
            </>
          ) : (
            /* Fallback: no HTML file — render from state JSON sections */
            <GenericBody PG={PG} decs={decs} onUpdateDec={onUpdateDec} comments={comments} />
          )}

          {/* Followups — from the doc's parsed state */}
          {(fullState?.followups || []).length > 0 && (
            <>
              <h2 id="log"><span className="sec">§</span>Followups</h2>
              <div className="followup-log">
                {fullState.followups.map(f => {
                  const done = !!(f.resolved_at || f.status === "resolved");
                  return (
                    <div className={`item ${done ? "done" : "open"}`} key={f.id}>
                      <div className="mark">{done ? "✓" : "○"}</div>
                      <div>
                        <div className="title">{f.title}</div>
                        <div className="meta">{f.written_by}{f.resolved_by ? ` → ${f.resolved_by}` : ""} · {f.written_at}{f.resolved_at ? ` → ${f.resolved_at}` : ""}</div>
                        {f.outcome && <div className="outcome">{f.outcome}</div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </article>
      )}

      {viewMode === "reading" && sel && !composingAt && (
        <button
          className="r-float-btn"
          style={{ top: sel.top + 6, left: Math.min(sel.left - 60, window.innerWidth - 140) }}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => { setComposingAt(sel); clearSel(); window.getSelection()?.removeAllRanges(); }}
        >¶ Comment</button>
      )}

      {composingAt && (
        <CommentPopover
          anchor={composingAt}
          onClose={() => setComposingAt(null)}
          onPost={(body) => { addComment(composingAt.sectionId, body, composingAt.quote); setComposingAt(null); }}
        />
      )}

      {showPrompt && (
        <window.reckon.PromptModal
          planSlug={slug}
          initialPrompt={
            window.buildFleetPrompt
              ? window.buildFleetPrompt(
                  [Object.assign({}, P, { decisions: decs, followups: fullState?.followups || [] })],
                  window.STATE
                )
              : "(prompts.js not loaded)"
          }
          onClose={() => setShowPrompt(false)}
        />
      )}
    </div>
  );
}

// ─── Locked decisions display (object-map format, no def array) ──────────
// Used for plans like reckon-mcp-gaps where decisions are recorded as a locked
// map keyed by decision ID but there is no decisions_def[] to drive a form.

function LockedDecisionsBlock({ lockedMap }) {
  const entries = Object.entries(lockedMap);
  if (entries.length === 0) return null;
  return (
    <>
      <h2 id="decisions-state"><span className="sec">§</span>Decisions (locked)</h2>
      <table className="decisions-tbl">
        <thead><tr><th>Key</th><th>Choice</th><th>Rationale</th><th>When</th></tr></thead>
        <tbody>
          {entries.map(([key, val]) => (
            <tr key={key}>
              <td><code>{key}</code></td>
              <td className="dec-choice chosen">{val.choice || "—"}</td>
              <td>{val.rationale || ""}</td>
              <td style={{ whiteSpace: "nowrap" }}>{val.by ? `${val.by} · ` : ""}{val.when || ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

// ─── Generic data-driven body (fallback when no HTML file) ───────────────

function GenericBody({ PG, decs, onUpdateDec, comments }) {
  return (
    <>
      <p style={{ color: "var(--muted)", fontSize: 14 }}>{PG.summary}</p>
      {(PG.sections || []).map(s => (
        <React.Fragment key={s.id}>
          <h2 id={s.id}><span className="sec">{s.sec}</span>{s.h}</h2>
          <p>{s.body}</p>
          <SectionComments comments={comments[s.id]} />
        </React.Fragment>
      ))}

      {decs.length > 0 && (
        <>
          <h2 id="decisions"><span className="sec">§</span>Decisions</h2>
          {decs.map(d => (
            <Decision key={d.key} d={d} onUpdate={(choice, rat) => onUpdateDec(d.key, choice, rat)} />
          ))}
          <SectionComments comments={comments["decisions"]} />
        </>
      )}

      {(PG.followups_done || []).length > 0 && (
        <>
          <h2 id="log"><span className="sec">§</span>Log</h2>
          <div className="followup-log">
            {PG.followups_done.map(f => (
              <div className="item" key={f.id}>
                <div className="mark">✓</div>
                <div>
                  <div className="title">{f.title}</div>
                  <div className="meta">{f.written_by} → {f.resolved_by} · {f.written_at} → {f.resolved_at}</div>
                  <div className="outcome">{f.outcome}</div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}

window.Plan = Plan;
window.GenericBody = GenericBody;
