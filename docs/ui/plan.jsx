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
          {/* Comment bodies are authored HTML (see reckon/_plan_html._inner_html). */}
          <div dangerouslySetInnerHTML={{ __html: c.body || "" }} />
          <div className="r-comment-actions">
            <button className="r-cr-edit" onClick={() => onEdit(c, sectionId)}>Edit</button>
            <button className="r-cr-del" onClick={() => onDelete(c.id)}>Delete</button>
          </div>
        </div>
      ))}
    </div>
  );
}

function GateTable({ gates }) {
  if (!gates || gates.length === 0) return null;
  return (
    <section className="r-gates" aria-labelledby="gate-state-heading">
      <h2 id="gate-state-heading"><span className="sec">§</span>Evidence gates</h2>
      <table className="r-gates-table">
        <thead><tr><th>Measure</th><th>Status</th><th>Verdict</th><th>Evidence</th></tr></thead>
        <tbody>
          {gates.map(gate => (
            <tr key={gate.id} className={gate.passed ? "passed" : "closed"}>
              <td>{gate.measure || gate.id}</td>
              <td>{gate.status || "open"}</td>
              <td>{gate.verdict || "pending"}</td>
              <td>{gate.evidence ? <a href={gate.evidence}>Evidence</a> : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function PlanInFlightBand({ runs }) {
  if (!runs.length) return null;

  const durationSeconds = (value) => {
    if (typeof value === "number") return value;
    const match = String(value || "").trim().match(/^(\d+(?:\.\d+)?)\s*([smh])$/i);
    if (!match) return null;
    return Number(match[1]) * ({ s: 1, m: 60, h: 3600 })[match[2].toLowerCase()];
  };
  const durationLabel = (seconds) => {
    if (seconds == null) return "unknown";
    if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
    if (seconds >= 60) return `${Math.floor(seconds / 60)}m`;
    return `${Math.floor(seconds)}s`;
  };
  const copyRunCommand = (run) => {
    const command = `reckon crew observe --run ${run.run_id}`;
    navigator.clipboard?.writeText(command);
    if (window.flashSaved) window.flashSaved("run command copied");
  };
  const renderRun = (run) => {
    const elapsed = run.elapsed_seconds;
    const budget = durationSeconds(run.time_budget || run.budget_ceiling);
    const progress = budget && elapsed != null ? Math.min(100, Math.round((elapsed / budget) * 100)) : 0;
    return (
      <li className="r-inflight-run" style={{ display: "grid", gridTemplateColumns: "minmax(260px, 1fr) minmax(170px, .45fr) auto auto", alignItems: "center", gap: 14 }} key={`${run.member || "member"}:${run.section || "plan"}:${run.created_at || run.elapsed_seconds || "active"}`}>
        <div className="r-inflight-identity" style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "4px 10px" }}>
          <strong>{run.backend || run.harness || "harness"}</strong>
          {run.model && <span>{run.model}</span>}
          <span>{run.role || "worker"} · {run.effort || "default effort"}</span>
          <span>@{run.member || "unassigned"}</span>
          <span>{run.section || "whole plan"}</span>
        </div>
        <div className="r-inflight-progress" style={{ display: "grid", gap: 4, color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 10 }}>
          <span>{durationLabel(elapsed)} / {budget == null ? (run.time_budget || "budget unknown") : durationLabel(budget)}</span>
          <span className="r-inflight-track" aria-label={`${progress}% of time budget`} style={{ overflow: "hidden", height: 4, borderRadius: 2, background: "var(--border)" }}>
            <span style={{ display: "block", width: `${progress}%`, height: "100%", background: "var(--accent)" }} />
          </span>
        </div>
        <span className={`r-inflight-phase ${run.phase || "unknown"}`} style={{ color: run.phase === "working" ? "var(--accent)" : "var(--muted)", fontFamily: "var(--mono)", fontSize: 10, textTransform: "uppercase" }}>{run.phase || "unknown"}</span>
        <button className="btn ghost r-inflight-action" style={{ whiteSpace: "nowrap" }} onClick={() => copyRunCommand(run)}>Copy run command</button>
      </li>
    );
  };

  return (
    <aside className="r-inflight-band" role="status" aria-label="Work in flight" style={{ marginBottom: 18, padding: "12px 14px", border: "1px solid var(--border)", borderLeft: "3px solid var(--accent)", background: "var(--accent-2)", fontSize: 12 }}>
      <div className="r-inflight-heading" style={{ marginBottom: 8, color: "var(--muted)", fontSize: 10, fontWeight: 750, letterSpacing: ".12em", textTransform: "uppercase" }}>In flight</div>
      <ul className="r-inflight-runs" style={{ display: "grid", gap: 9, margin: 0, padding: 0, listStyle: "none" }}>
        {runs.map(run => (
          renderRun(run)
        ))}
      </ul>
    </aside>
  );
}

function ReaderSourceFailure({ source, status, missing, onRetry }) {
  return (
    <div className="r-reader-source-failure" role="alert" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 18, marginBottom: 12, padding: "11px 13px", border: "1px solid var(--border)", borderLeft: "3px solid var(--danger, #b42318)", background: "var(--surface)" }}>
      <div style={{ display: "grid", gap: 3 }}>
        <strong>Partial plan — {source} failed</strong>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>Status: {status}. Missing: {missing.join(", ")}.</span>
      </div>
      <button className="btn ghost" onClick={onRetry}>Retry</button>
    </div>
  );
}

function Plan({ slug, onNav }) {
  const M = window.STATE;
  if (!M) return null;
  const PG = M.plans[slug];
  if (!PG) return <div className="r-page">Artifact "{slug}" not found.</div>;

  const P = PG;
  const isResearch = PG.type === "research";
  const isEvidence = PG.type === "evidence";
  const refSlug = (ref) => String(ref || "").split("#", 1)[0].split(":").pop();
  const project = M.project || document.querySelector('meta[name="docs-project"]')?.content || "";
  const [liveRuns, setLiveRuns] = useState([]);

  useEffect(() => {
    if (!project || isResearch || isEvidence) { setLiveRuns([]); return; }
    let active = true;
    const poll = async () => {
      try {
        const response = await fetch(`/crew/${encodeURIComponent(project)}`, { cache: "no-store" });
        if (!response.ok) return;
        const payload = await response.json();
        if (active && Array.isArray(payload.runs)) {
          setLiveRuns(payload.runs.filter(run => run.plan === slug));
        }
      } catch (_) { /* The plan remains readable when live state is unavailable. */ }
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [project, slug, isResearch, isEvidence]);

  const stored = planLoad(slug) || {};
  // The inventory is lightweight; full per-doc state (decisions, followups) is
  // fetched from /plan/<project>/<slug> when the doc opens.
  const [decs, setDecs] = useState([]);
  const [fullState, setFullState] = useState(null);
  const [stateFailure, setStateFailure] = useState(null);
  const [stateRetry, setStateRetry] = useState(0);
  const [showPrompt, setShowPrompt] = useState(false);
  const [composingAt, setComposingAt] = useState(null);
  const [reviewing, setReviewing] = useState(null); // { id, comment, x, y }

  // ── Fetch HTML plan body ────────────────────────────────────────────────
  const [planHtml, setPlanHtml] = useState(null);
  const [htmlReady, setHtmlReady] = useState(false);
  const [htmlFailure, setHtmlFailure] = useState(null);
  const [htmlRetry, setHtmlRetry] = useState(0);
  const htmlRef = useRef(null);

  useEffect(() => {
    setHtmlReady(false);
    setPlanHtml(null);
    setHtmlFailure(null);
    if (!project) { setHtmlReady(true); return; }

    // Use plan's href (may include subdir e.g. "curated/slug") if available
    const href = PG.href || slug;
    fetch(`/${project}/${href}.html`, { cache: "no-store" })
      .then(r => {
        if (!r.ok) throw { responseStatus: `HTTP ${r.status}` };
        return r.text();
      })
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
        // questions / comments otherwise stay as authored HTML (passthrough is
        // their single render source — see the suppressed React panels below).
        article.querySelector('section[data-reckon="decisions"]')?.remove();
        // Gates are rendered from parsed state so status and verdict attributes
        // become visible text rather than remaining hidden HTML metadata.
        article.querySelector('section[data-reckon="gates"]')?.remove();
        // Decision-anchored comments are the one exception: they are rendered by
        // the interactive ActionableCommentList under the decision widgets (with
        // Edit/Delete). Strip them from the passthrough comments section so they
        // render once, not twice. All other comments render from passthrough.
        article
          .querySelectorAll('section[data-reckon="comments"] .r-comment[data-section="decisions"]')
          .forEach(el => el.remove());
        // Strip inline scripts — they target the standalone page, not the SPA
        article.querySelectorAll("script").forEach(s => s.remove());
        setPlanHtml(article.innerHTML);
        setHtmlReady(true);
      })
      .catch(error => {
        setHtmlFailure({ status: error?.responseStatus || "network error" });
        setHtmlReady(true);
      });
  }, [project, slug, htmlRetry]);

  // ── Fetch full doc state (decisions, followups) ─────────────────────────
  useEffect(() => {
    setFullState(null);
    setDecs([]);
    setStateFailure(null);
    if (!project) return;
    const stateRoot = { plan: "plans", research: "research", evidence: "evidence" }[PG.type || "plan"];
    const statePath = `${stateRoot}/${PG.slug}`;
    fetch(`/plan/${project}/${statePath}`, { cache: "no-store" })
      .then(r => {
        if (!r.ok) throw { responseStatus: `HTTP ${r.status}` };
        return r.json();
      })
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
      .catch(error => setStateFailure({ status: error?.responseStatus || "network error" }));
  }, [project, slug, PG.type, stateRetry]);

  // ── Comment / prompt wiring ─────────────────────────────────────────────
  useEffect(() => {
    const open = () => {
      // Don't generate a work prompt for a plan that has no work: finished /
      // reference / archived plans, or anything at 100%. Dispatching a fleet
      // against a done plan is the bug this guards.
      const NONACTIONABLE = ["shipped", "done", "archived", "superseded", "abandoned", "reference", "historical"];
      const impl = (fullState && fullState.impl != null) ? fullState.impl : (P.impl || 0);
      if ((P.type || "plan") !== "plan" || NONACTIONABLE.includes((P.status || "").toLowerCase()) || impl >= 1) {
        if (window.flashSaved) window.flashSaved(`ℹ ${slug} is ${P.status || "complete"} (${Math.round(impl * 100)}%) — no work to dispatch`);
        return;
      }
      // Open decisions are NOT a blocker. The prompt is built to carry them —
      // see prompts.js "Open decisions (surface, do not resolve)". Deferring a
      // decision to be resolved DURING the work (e.g. an I/O strategy decided
      // in the first task) is a valid state, so the prompt still generates.
      const openDecs = decs.filter(d => !d.chosen && !d.choice);
      if (openDecs.length > 0 && window.flashSaved) {
        window.flashSaved(`ℹ ${openDecs.length} open decision${openDecs.length === 1 ? "" : "s"} surfaced in the prompt (to resolve during the work)`);
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
  const loadedPlanVersion = fullState?.version ?? P.version ?? "unavailable";

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
  useLayoutEffect(() => {
    if (!htmlRef.current) return;
    if (!planHtml) { htmlRef.current.innerHTML = ""; return; }
    htmlRef.current.innerHTML = planHtml;
    Object.values(comments).flat().forEach(c => {
      if (c && c.quote) injectCommentMark(htmlRef, c);
    });
  }, [planHtml]); // comments intentionally NOT in deps; marks managed by effect below

  // Re-inject marks when comments change (new save, delete, reload).
  useLayoutEffect(() => {
    if (!htmlRef.current || !planHtml) return;
    Object.values(comments).flat().forEach(c => {
      if (c && c.quote) injectCommentMark(htmlRef, c);
    });
  }, [comments]);

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
  }, [comments]);

  return (
    <div className="r-page">
      <article className="r-reading" ref={articleRef}>
          <PlanInFlightBand runs={liveRuns} />
          {htmlFailure && (
            <ReaderSourceFailure
              source="authored plan HTML"
              status={htmlFailure.status}
              missing={["authored body", "authored followups", "authored comments"]}
              onRetry={() => setHtmlRetry(value => value + 1)}
            />
          )}
          {stateFailure && (
            <ReaderSourceFailure
              source="structured plan state"
              status={stateFailure.status}
              missing={["interactive decisions", "evidence gates", "structured followups", "loaded plan version"]}
              onRetry={() => setStateRetry(value => value + 1)}
            />
          )}
          {isResearch && (
            <div className="r-research-banner">
              <span className="r-type-tag research">research</span>
              {PG.source && <span>source&nbsp;<strong>{PG.source}</strong></span>}
              {PG.source_quality && <span>quality&nbsp;<strong>{PG.source_quality}</strong></span>}
              {PG.reviewed_at && <span>reviewed&nbsp;{PG.reviewed_at}</span>}
              {(PG.informs || []).length > 0 && (
                <span className="informs">informs&nbsp;
                  {PG.informs.map((s, i) => (
                    <React.Fragment key={s}>
                      {i > 0 && ", "}
                      <a href={`#plan/${refSlug(s)}`}>{s}</a>
                    </React.Fragment>
                  ))}
                </span>
              )}
            </div>
          )}
          {isEvidence && (
            <div className="r-research-banner">
              <span className="r-type-tag evidence">evidence</span>
              {PG.verdict && <span className="verdict">verdict&nbsp;<strong>{PG.verdict}</strong></span>}
              {(PG.evidence_for || []).length > 0 && (
                <span className="informs">evidence for&nbsp;
                  {PG.evidence_for.map((s, i) => (
                    <React.Fragment key={s}>
                      {i > 0 && ", "}
                      <a href={`#plan/${refSlug(s)}`}>{s}</a>
                    </React.Fragment>
                  ))}
                </span>
              )}
              {(PG.verifies || []).length > 0 && (
                <span className="informs">verifies&nbsp;{PG.verifies.join(", ")}</span>
              )}
              {PG.environment && <span>environment&nbsp;<code>{PG.environment}</code></span>}
              {(PG.commits || []).length > 0 && <span>commits&nbsp;{PG.commits.join(", ")}</span>}
              {(PG.artifacts || []).length > 0 && <span>artifacts&nbsp;{PG.artifacts.join(", ")}</span>}
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

              {/* Comments on non-decisions body sections are NOT re-rendered here.
                  They survive in the passthrough HTML (section[data-reckon="comments"])
                  and render once, faithfully, as authored HTML — avoiding the
                  escaped-text double-render. Inline quote-marks (injectCommentMark)
                  still annotate the passthrough body from `comments` state.
                  Decisions keep their interactive ActionableCommentList above
                  because the decisions section is stripped from passthrough. */}
            </>
          ) : (
            /* Fallback: no HTML file — render from state JSON sections */
            <GenericBody PG={PG} decs={decs} onUpdateDec={onUpdateDec} comments={comments} />
          )}

          <GateTable gates={fullState?.gates || []} />

          {/* Followups are NOT re-rendered here. When the authored HTML body is
              present it carries section[data-reckon="followups"] through the
              passthrough, which renders the title, HTML body, plain-text prompt
              and outcome once. Re-rendering from fullState here would escape the
              body and duplicate the section. The GenericBody fallback (no HTML
              file) still renders followups from state for legacy docs. */}
          {!planHtml && (fullState?.followups || []).length > 0 && (
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
                        {f.outcome && <div className="outcome" dangerouslySetInnerHTML={{ __html: f.outcome }} />}
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
      </article>

      {sel && !composingAt && (
        <button
          className="r-float-btn"
          style={{ top: sel.top, left: sel.left }}
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
          planVersion={loadedPlanVersion}
          initialPrompt={
            window.reckon.withHandoffProvenance(
              window.buildFleetPrompt
                ? window.buildFleetPrompt(
                  [Object.assign({}, P, {
                    decisions: decs,
                    followups: fullState?.followups || [],
                    comments,  // live state — always current; fullState snapshot can be stale
                  })],
                  window.STATE
                  // Single requested item → the builder emits fleet framing for
                  // this one plan; its dependencies are called out softly (never
                  // auto-dispatched). Same builder/format as the sprint surface.
                  )
                : "(prompts.js not loaded)",
              loadedPlanVersion
            )
          }
          onClose={() => setShowPrompt(false)}
        />
      )}
    </div>
  );
}

// ─── Locked decisions display (object-map format, no def array) ──────────
// Renders locked decision maps even when no form definition array is present.

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
                  <div className="outcome" dangerouslySetInnerHTML={{ __html: f.outcome || "" }} />
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
