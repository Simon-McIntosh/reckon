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

// Walk the rendered plan HTML for the first text node whose textContent
// contains `comment.quote`, and wrap that range in a <mark class="r-cm-anchor">
// + tiny <sup class="r-cm-badge">¶</sup>. No-op if the comment has no quote,
// no id, or has already been injected (looked up via [data-cm="<id>"]).
//
// Accepts an optional directRange (a cloned live Range from selection time) so
// that cross-element selections — which surroundContents cannot handle — work
// correctly via extractContents+insertNode.
//
// Module-level (not a hook) so the function identity is stable across renders.
function injectCommentMark(htmlRef, comment, directRange) {
  const root = htmlRef.current;
  if (!root || !comment || !comment.id) return;
  // Avoid double-injection
  if (root.querySelector(`[data-cm="${comment.id}"]`)) return;

  const attachBadge = (mark) => {
    const badge = document.createElement("sup");
    badge.className = "r-cm-badge";
    badge.dataset.cm = comment.id;
    badge.textContent = "¶";
    mark.after(badge);
  };

  // ── Fast path: inject using the preserved live Range ─────────────────────
  if (directRange) {
    try {
      const mark = document.createElement("mark");
      mark.className = "r-cm-anchor";
      mark.dataset.cm = comment.id;
      if (comment.body) mark.title = comment.body;
      // extractContents handles cross-element selections correctly
      mark.appendChild(directRange.extractContents());
      directRange.insertNode(mark);
      attachBadge(mark);
      return;
    } catch (_) { /* fall through to text-walk */ }
  }

  // ── Fallback: text-walk (used on page reload when Range is gone) ──────────
  if (!comment.quote) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.parentElement?.closest(".r-cm-anchor")) continue;
    const idx = node.textContent.indexOf(comment.quote);
    if (idx < 0) continue;
    try {
      const r = document.createRange();
      r.setStart(node, idx);
      r.setEnd(node, idx + comment.quote.length);
      const mark = document.createElement("mark");
      mark.className = "r-cm-anchor";
      mark.dataset.cm = comment.id;
      if (comment.body) mark.title = comment.body;
      mark.appendChild(r.extractContents()); // also handles cross-element
      r.insertNode(mark);
      attachBadge(mark);
    } catch (_) { /* skip silently — mark will still show in §Comments panel */ }
    break;
  }
}

// Comment list with inline Edit/Delete buttons — used in plan body and §Comments panel.
function ActionableCommentList({ sectionId, arr, onEdit, onDelete }) {
  if (!arr || arr.length === 0) return null;
  return (
    <div className="r-section-comments">
      {arr.map(c => (
        <div key={c.id} className="r-inline-comment">
          <div className="meta">{c.who} · {c.when}</div>
          {c.quote && <div className="quote">"{c.quote}"</div>}
          <div>{c.body}</div>
          <div className="r-comment-actions">
            <button className="r-cr-edit" onClick={() => onEdit(c, sectionId)}>Edit</button>
            <button className="r-cr-del" onClick={() => onDelete(c.id)}>Delete</button>
          </div>
        </div>
      ))}
    </div>
  );
}

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
  const [reviewing, setReviewing] = useState(null); // { id, comment, x, y }
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
        // Load comments from server — canonical source, overrides stale localStorage
        if (rec.comments && typeof rec.comments === "object") {
          setComments(prev => {
            // Per section: keep the longer array (local may have unsaved additions)
            const merged = { ...rec.comments };
            for (const [sid, arr] of Object.entries(prev)) {
              if ((arr || []).length > (merged[sid] || []).length) merged[sid] = arr;
            }
            return merged;
          });
        }
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

  const addComment = (sectionId, body, quote, range = null) => {
    if (!body || !body.trim()) return;
    const now = new Date().toISOString().slice(0, 16).replace("T", " ");
    // If composing was opened in "edit" mode, replace the existing comment
    // with the same id in the matching section array.
    if (composingAt && composingAt.editing && composingAt.id) {
      const arr = comments[sectionId] || [];
      const replaced = arr.map(c => c.id === composingAt.id
        ? { ...c, body: body.trim(), when: now, ...(quote ? { quote } : {}) }
        : c
      );
      const next = { ...comments, [sectionId]: replaced };
      setComments(next);
      planSave(slug, { [`comments.${sectionId}`]: replaced });
      if (window.flashSaved) window.flashSaved(`${slug}.comments.${sectionId} updated`);
      return;
    }
    const c = { id: `c-${Date.now()}`, who: author, when: now, body: body.trim(), ...(quote ? { quote } : {}) };
    // Inject anchor immediately using the live range (before React re-renders)
    if (range && htmlRef.current) {
      injectCommentMark(htmlRef, c, range);
    }
    const next = { ...comments, [sectionId]: [...(comments[sectionId] || []), c] };
    setComments(next);
    planSave(slug, { [`comments.${sectionId}`]: next[sectionId] });
    if (window.flashSaved) window.flashSaved(`${slug}.comments.${sectionId} +1`);
  };

  // ── Shared comment action helpers ──────────────────────────────────────
  const deleteComment = (id) => {
    const next = {};
    for (const [sid, arr] of Object.entries(comments)) {
      const filtered = (arr || []).filter(c => c.id !== id);
      next[sid] = filtered;
      if ((arr || []).length !== filtered.length) {
        planSave(slug, { [`comments.${sid}`]: filtered });
      }
    }
    if (htmlRef.current) {
      htmlRef.current.querySelectorAll(`[data-cm="${id}"]`).forEach(el => {
        if (el.tagName === "MARK") {
          const p = el.parentNode;
          while (el.firstChild) p.insertBefore(el.firstChild, el);
          p.removeChild(el);
        } else { el.remove(); }
      });
    }
    setComments(next);
    setReviewing(null);
  };

  const editComment = (c, sectionId) => {
    setReviewing(null);
    setComposingAt({ ...c, sectionId, planSlug: slug, editing: true });
  };

  // Populate the plan HTML div imperatively so React never touches it after
  // initial set, preventing clobbering of injected comment marks.
  // viewMode is included so toggling graph→reading re-populates the remounted div.
  useLayoutEffect(() => {
    if (!htmlRef.current) return;
    if (!planHtml) { htmlRef.current.innerHTML = ""; return; }
    htmlRef.current.innerHTML = planHtml;
    // Re-inject existing comment marks after innerHTML reset
    if (viewMode === "reading") {
      Object.values(comments).flat().forEach(c => {
        if (c && c.quote) injectCommentMark(htmlRef, c);
      });
    }
  }, [planHtml, viewMode]); // comments intentionally NOT in deps; marks managed by effect below

  // Re-inject marks when comments change (new save, delete, reload).
  // planHtml removed from deps — innerHTML is managed by the effect above.
  useLayoutEffect(() => {
    if (viewMode !== "reading") return;
    if (!htmlRef.current || !planHtml) return;
    Object.values(comments).flat().forEach(c => {
      if (c && c.quote) injectCommentMark(htmlRef, c);
    });
  }, [comments, viewMode]);

  // Click-to-review: any click on an injected [data-cm] element opens
  // CommentReviewPopover anchored to that element's viewport rect.
  useEffect(() => {
    const el = articleRef.current;
    if (!el) return;
    const handleClick = (e) => {
      const target = e.target.closest("[data-cm]");
      if (!target) return;
      const id = target.dataset.cm;
      const all = Object.values(comments).flat();
      const c = all.find(x => x.id === id);
      if (!c) return;
      const rect = target.getBoundingClientRect();
      e.stopPropagation();
      setReviewing({ id, comment: c, x: rect.left, y: rect.bottom + 6 });
    };
    el.addEventListener("click", handleClick);
    return () => el.removeEventListener("click", handleClick);
  }, [comments, viewMode]);

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
              <div ref={htmlRef} className="r-plan-html" />

              {/* Interactive decisions — rendered from the doc's parsed state */}
              {decs.length > 0 && (
                <>
                  <h2 id="decisions"><span className="sec">§</span>Decisions</h2>
                  {decs.map(d => (
                    <Decision key={d.key} d={d} onUpdate={(choice, rat) => onUpdateDec(d.key, choice, rat)} />
                  ))}
                  <ActionableCommentList sectionId="decisions" arr={comments["decisions"] || []} onEdit={editComment} onDelete={deleteComment} />
                </>
              )}

              {/* Comments on non-decisions HTML body sections */}
              {Object.entries(comments).filter(([id, arr]) => id !== "decisions" && (arr || []).length > 0).length > 0 && (
                <>
                  <h2 id="comments-panel"><span className="sec">§</span>Comments</h2>
                  {Object.entries(comments)
                    .filter(([id, arr]) => id !== "decisions" && (arr || []).length > 0)
                    .map(([id, arr]) => (
                      <div key={id} className="r-comment-section">
                        <div className="r-comment-section-h">↳ {id === "_top" ? "top of page" : `#${id}`}</div>
                        <ActionableCommentList sectionId={id} arr={arr} onEdit={editComment} onDelete={deleteComment} />
                      </div>
                    ))
                  }
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
          style={{
            top:  sel.top - 1,
            left: Math.min(sel.left + 8, window.innerWidth - 110),
          }}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => { setComposingAt(sel); clearSel(); window.getSelection()?.removeAllRanges(); }}
          title="Add comment"
        >Comment</button>
      )}

      {composingAt && (
        <CommentPopover
          anchor={composingAt}
          onClose={() => setComposingAt(null)}
          onPost={(body) => { addComment(composingAt.sectionId, body, composingAt.quote, composingAt.range); setComposingAt(null); }}
        />
      )}

      {reviewing && (
        <window.reckon.CommentReviewPopover
          reviewing={reviewing}
          onClose={() => setReviewing(null)}
          onDelete={deleteComment}
          onEdit={(c) => {
            let sectionId = "_top";
            for (const [sid, arr] of Object.entries(comments)) {
              if ((arr || []).some(x => x.id === c.id)) { sectionId = sid; break; }
            }
            editComment(c, sectionId);
          }}
        />
      )}

      {showPrompt && (
        <window.reckon.PromptModal
          planSlug={slug}
          initialPrompt={
            window.buildFleetPrompt
              ? window.buildFleetPrompt(
                  [Object.assign({}, P, {
                    decisions: decs,
                    followups: fullState?.followups || [],
                    comments:  fullState?.comments  || (planLoad(slug)?.comments) || {},
                  })],
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
