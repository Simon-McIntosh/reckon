// Reckon shell title module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

const LIFECYCLE_STATUSES = [
  { value: "active",     label: "Active",     group: "workflow" },
  { value: "blocked",    label: "Blocked",    group: "workflow" },
  { value: "pending",    label: "Pending",    group: "workflow" },
  { value: "on-hold",    label: "On hold",    group: "paused"   },
  { value: "shipped",    label: "Shipped",    group: "done"     },
  { value: "superseded", label: "Superseded", group: "done"     },
  { value: "abandoned",  label: "Abandoned",  group: "done"     },
];

function canonicalReaderKind(value) {
  const kind = String(value || "plan").toLowerCase();
  return kind === "doc" ? "research" : kind;
}

function readerReferenceSlug(value) {
  return String(value || "").split("#", 1)[0].split(":").pop();
}

function readerPlanFor(item, state) {
  const kind = canonicalReaderKind(item?.type);
  const parentRef = kind === "plan"
    ? item?.slug
    : kind === "research"
      ? (item?.informs || [])[0]
      : kind === "evidence"
        ? item?.evidence_for?.[0] || item?.verifies?.[0]
        : item?.for_plan || item?.forPlan;
  const slug = readerReferenceSlug(parentRef);
  return (state?.inventory || []).find(candidate =>
    canonicalReaderKind(candidate.type) === "plan" && candidate.slug === slug
  ) || null;
}

function readerSourceTrail(item, state) {
  if (!item) return [];
  const kind = canonicalReaderKind(item.type);
  const plan = readerPlanFor(item, state);
  const trail = [];
  if (plan?.sprint) {
    trail.push({ key: `sprint:${plan.sprint}`, label: plan.sprint, view: "sprint", sprint: plan.sprint });
  }
  if (plan && kind !== "plan") {
    trail.push({ key: `plan:${plan.slug}`, label: plan.title || plan.slug, view: "plan", slug: plan.slug });
  }
  trail.push({
    key: `${kind}:${item.nav_key || item.slug}`,
    label: item.title || item.slug,
    view: kind,
    slug: item.slug,
  });
  return trail.map((segment, index) => ({
    ...segment,
    navigates: index < trail.length - 1,
  }));
}

function readerMetadataRows(item, project) {
  if (!item) return [];
  const kind = canonicalReaderKind(item.type);
  const rows = kind === "plan"
    ? [
        ["status", item.effective_status || item.status],
        ["impl", item.impl !== null && item.impl !== undefined && String(item.impl).trim() !== "" && Number.isFinite(Number(item.impl)) ? `${Math.round(Number(item.impl) * 100)}%` : ""],
        ["effort", item.effort_hours == null ? "" : `${item.effort_hours} worker-h`],
        ["wall", item.wall_clock_hours == null ? "" : `${item.wall_clock_hours}h`],
        ["sprint", item.sprint],
        ["repo", item.repository || project],
      ]
    : [
        [kind === "research" ? "verdict" : kind === "evidence" ? "gate" : "dimensions", kind === "research" ? item.verdict : kind === "evidence" ? (item.gate || item.verdict) : item.dims],
        ["created", item.created],
        ["edited", item.edited || item.last],
        ["repo", item.repository || project],
      ];
  return rows.filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== "");
}

const renderedReaderLists = new Map();

function publishRenderedReaderList(kind, items) {
  const canonicalKind = canonicalReaderKind(kind);
  const list = Array.isArray(items) ? items.filter(Boolean).map(item =>
    typeof item === "string" ? { key: item, slug: readerReferenceSlug(item), type: canonicalKind } : {
      key: item.nav_key || item.key || item.slug,
      slug: item.slug || readerReferenceSlug(item.nav_key || item.key),
      type: canonicalReaderKind(item.type || canonicalKind),
    }
  ).filter(item => item.key && item.slug) : [];
  renderedReaderLists.set(canonicalKind, list);
  window.dispatchEvent(new CustomEvent("reckon:reader-list-published", {
    detail: { kind: canonicalKind, count: list.length },
  }));
  return list;
}

function readerListFor(kind) {
  return renderedReaderLists.get(canonicalReaderKind(kind)) || [];
}

window.addEventListener("reckon:rendered-reader-list", event => {
  publishRenderedReaderList(event.detail?.kind, event.detail?.items);
});

function ReaderChrome({ item, state, project, focusMode, position, onNav, onBack, onStep, onToggleFocus }) {
  const kind = canonicalReaderKind(item?.type);
  const trail = readerSourceTrail(item, state);
  const metadata = readerMetadataRows(item, project);
  const copyValue = kind === "plan" ? `/reckon-ship ${item.slug}` : item.slug;
  const copy = () => {
    navigator.clipboard?.writeText(copyValue);
    window.flashSaved?.("reader source copied");
  };
  return (
    <>
      <nav className="r-reading-controls" aria-label="Reader controls">
        <button type="button" className="r-reading-back" onClick={onBack}>← {kind === "figure" ? "Figures" : `${kind[0].toUpperCase()}${kind.slice(1)}`}</button>
        <span className="r-reading-paging">
          <button type="button" onClick={() => onStep(-1)} aria-label="Previous item in rendered list" disabled={position.current <= 1}>‹</button>
          <span className="r-reading-position" role="status">{position.current} / {position.total}</span>
          <button type="button" onClick={() => onStep(1)} aria-label="Next item in rendered list" disabled={position.current < 1 || position.current >= position.total}>›</button>
        </span>
        <span className="r-reading-trail" aria-label="Source trail">
          {trail.map((segment, index) => (
            <React.Fragment key={segment.key}>
              {index > 0 && <span className="r-reading-trail-separator" aria-hidden="true">/</span>}
              {segment.navigates ? (
                <button type="button" className="r-reading-trail-link" onClick={() => onNav(segment)}>{segment.label}</button>
              ) : (
                <span className="r-reading-trail-current" aria-current="page">{segment.label}</span>
              )}
            </React.Fragment>
          ))}
        </span>
        <button type="button" className="r-reading-focus" onClick={onToggleFocus} aria-pressed={focusMode} title={focusMode ? "Leave full screen (Escape)" : "Read full screen (f)"}>{focusMode ? "esc" : "f"}</button>
        <button type="button" className="r-reading-copy" onClick={copy} title={`Copy ${copyValue}`}>Copy</button>
      </nav>
      <div className="r-reading-metadata" aria-label="Reader metadata">
        {metadata.map(([key, value]) => <span key={key}><span>{key}</span> {value}</span>)}
      </div>
    </>
  );
}

function statusWriteNotice(slug, result) {
  if (result?.persistence === "canonical" && result.ok) {
    return { state: "saved", text: `${slug} · saved to plan HTML`, version: result.version };
  }
  if (result?.persistence === "conflict") {
    return { state: "conflict", text: `${slug} · conflict; not saved · refresh and retry` };
  }
  if (result?.persistence === "failed") {
    return { state: "failed", text: `${slug} · ${result.where || "not saved"}` };
  }
  return {
    state: "local-only",
    text: `${slug} · local only · ${result?.where || "canonical save unavailable"}`,
  };
}

async function persistStatusPatch({ slug, plan, patch, onAfterChange, save, notify }) {
  const previous = Object.fromEntries(Object.keys(patch).map(key => [key, plan[key]]));
  Object.assign(plan, patch);
  if (onAfterChange) onAfterChange();

  let result;
  try {
    result = save
      ? await save(slug, patch)
      : { ok: false, persistence: "failed", local_ok: false, where: "not saved (persistence unavailable)", version: null };
  } catch (error) {
    console.warn("StatusMenu: persistence failed", error);
    result = { ok: false, persistence: "failed", local_ok: false, where: "not saved (persistence failed)", version: null };
  }

  if (result?.persistence === "conflict" || result?.persistence === "failed") {
    Object.assign(plan, previous);
    if (onAfterChange) onAfterChange();
  }

  if (notify) notify(statusWriteNotice(slug, result));
  return result;
}

function StatusMenu({ slug, plan, onAfterChange }) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!open) return;
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const isResearch = plan.type === "research" || plan.type === "doc";
  const isArchived = plan.archived === "1" || plan.archived === true || plan.archived === "true";
  const isRead = plan.read === "1" || plan.read === true || plan.read === "true";

  const apply = async (patch) => {
    // Update local inventory immediately so the UI reflects the change
    // before the server round-trips.
    const save = window.planSave || window.reckon?.planSave;
    return persistStatusPatch({
      slug,
      plan,
      patch,
      onAfterChange,
      save,
      notify: window.flashSaved,
    });
  };

  const setStatus = async (s) => { setOpen(false); await apply({ status: s }); };
  const toggleArchive = async () => { setOpen(false); await apply({ archived: isArchived ? "" : "1" }); };
  const toggleRead = async () => { setOpen(false); await apply({ read: isRead ? "" : "1" }); };

  return (
    <div className="r-status-menu-wrap" ref={ref}>
      <button
        type="button"
        className={`status-pill clickable ${plan.status} ${isArchived ? "archived" : ""}`}
        onClick={() => setOpen(o => !o)}
        title="Change status"
      >
        <span className="dot"></span>
        <span>{plan.status}</span>
        {isArchived && <span className="r-status-tag">archived</span>}
        {isResearch && isRead && <span className="r-status-tag read">read</span>}
        <svg className="r-status-caret" width="8" height="6" viewBox="0 0 8 6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M1 1.5l3 3 3-3"/>
        </svg>
      </button>
      {open && (
        <div className="r-status-popover" role="menu">
          <div className="r-status-section">
            <div className="r-status-section-h">Workflow</div>
            {LIFECYCLE_STATUSES.filter(s => s.group === "workflow").map(s => (
              <button key={s.value} type="button" className={`r-status-item ${plan.status === s.value ? "current" : ""}`} onClick={() => setStatus(s.value)}>
                <span className={`r-status-dot ${s.value}`}></span>{s.label}
              </button>
            ))}
          </div>
          <div className="r-status-section">
            <div className="r-status-section-h">Paused</div>
            {LIFECYCLE_STATUSES.filter(s => s.group === "paused").map(s => (
              <button key={s.value} type="button" className={`r-status-item ${plan.status === s.value ? "current" : ""}`} onClick={() => setStatus(s.value)}>
                <span className={`r-status-dot ${s.value}`}></span>{s.label}
              </button>
            ))}
          </div>
          <div className="r-status-section">
            <div className="r-status-section-h">Closed</div>
            {LIFECYCLE_STATUSES.filter(s => s.group === "done").map(s => (
              <button key={s.value} type="button" className={`r-status-item ${plan.status === s.value ? "current" : ""}`} onClick={() => setStatus(s.value)}>
                <span className={`r-status-dot ${s.value}`}></span>{s.label}
              </button>
            ))}
          </div>
          <div className="r-status-section r-status-actions">
            <button type="button" className={`r-status-item r-status-action ${isArchived ? "on" : ""}`} onClick={toggleArchive} title="Archive removes the plan from the default list — it still exists, just out of the way.">
              <span className="r-status-action-glyph">{isArchived ? "↺" : "▦"}</span>
              {isArchived ? "Unarchive" : "Archive"}
            </button>
            {isResearch && (
              <button type="button" className={`r-status-item r-status-action ${isRead ? "on" : ""}`} onClick={toggleRead} title="Mark this research/doc as reviewed.">
                <span className="r-status-action-glyph">{isRead ? "↺" : "✓"}</span>
                {isRead ? "Mark unread" : "Mark read"}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function TitleBar({ route, onNav, onOpenPrompt, onPlanMutated }) {
  const M = window.STATE;
  if (route.view === "cockpit") {
    return null;
  }
  if (route.view === "plan") {
    if (route.slug) return null;
    const p = M.inventory.find(x => (x.nav_key || x.slug) === route.slug);
    if (!p) return null;
    const isPlan = (p.type || "plan") === "plan";
    const direction = (M.north_stars || []).find(item => item.id === p.north_star);
    const openDecs = p.dec_open || 0;
    const blockedByDecisions = isPlan && openDecs > 0;
    const hasMetadataValue = value => {
      const text = value === null || value === undefined ? "" : String(value).trim();
      return text !== "" && text !== "-" && text !== "—";
    };
    const hasImplementation = hasMetadataValue(p.impl) && Number.isFinite(Number(p.impl));
    return (
      <div className="r-titlebar">
        <div className="row1">
          <span className="crumbs"><code>/{route.slug}</code></span>
          <span className="title">{p.title}</span>
          <div className="actions">
            {blockedByDecisions && (
              <button className="sig dec" data-target="decisions" title="Take the next open decision" aria-label={`${p.title}: ${openDecs} open decisions`} onClick={() => {
                  const section = document.getElementById("decisions");
                  if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
              >
                Resolve <span className="resolve-badge">{openDecs}</span>
              </button>
            )}
            {isPlan && <button
              className="gen-prompt"
              onClick={onOpenPrompt}
              title="Generate handoff prompt"
            >
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4 3h6l2 2v8H4z"/>
                <path d="M10 3v2h2"/>
                <path d="M6 7h4M6 9h4M6 11h2"/>
              </svg>
              Generate prompt
            </button>}
          </div>
        </div>
        <div className="row2">
          {isPlan ? <>
            <StatusMenu slug={route.slug} plan={p} onAfterChange={onPlanMutated} />
            {hasMetadataValue(p.ms) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">ms</span><span className="v">{p.ms}</span></span>
            </>}
            {hasMetadataValue(p.sprint) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">sprint</span><a className="v" href={`#sprint/${p.sprint}`} style={{ borderBottom: "1px dotted var(--line)" }}>{p.sprint}</a></span>
            </>}
            {hasImplementation && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">progress</span><span className="v">{Math.round(Number(p.impl) * 100)}%</span></span>
            </>}
            {hasMetadataValue(p.north_star) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item r-north-star-badge" title={direction?.statement || p.north_star}><span className="k">north star</span><span className="v">{direction?.name || p.north_star}</span></span>
            </>}
            {hasMetadataValue(p.capability?.class) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">capability</span><span className="v">{p.capability.class}</span></span>
            </>}
          </> : <>
            <span className={`r-type-pill ${p.type}`}>{p.type}</span>
            {p.type === "research" && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">informs</span><span className="v">{(p.informs || []).join(", ") || "unlinked"}</span></span>
              {p.reviewed_at && <><span className="dot-sep">·</span><span className="meta-item"><span className="k">reviewed</span><span className="v">{p.reviewed_at}</span></span></>}
            </>}
            {p.type === "evidence" && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">verdict</span><span className="v">{p.verdict || "unreviewed"}</span></span>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">evidence for</span><span className="v">{(p.evidence_for || []).join(", ") || "unlinked"}</span></span>
            </>}
          </>}
          {hasMetadataValue(p.last) && <>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">last</span><span className="v">{p.last}</span></span>
          </>}
          {hasMetadataValue(p.owner) && <>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">owner</span><span className="v">{p.owner}</span></span>
          </>}
        </div>
      </div>
    );
  }
  if (route.view === "sprint") {
    const sprints = M.sprints || [];
    const idx = sprints.findIndex(s => s.id === route.sprint);
    const s = sprints[idx];
    const slugSet = new Set((s?.items || []).map(it => typeof it === "string" ? it : it.slug));
    const inv = [...slugSet].map(slug => M.inventory.find(x => x.slug === slug)).filter(Boolean);
    const totalOpen = inv.reduce((n, p) => n + (p.dec_open || 0), 0);
    const blocked = totalOpen > 0;
    const blockedPlans = inv.filter(p => (p.dec_open || 0) > 0);
    const handleResolve = () => {
      if (blockedPlans.length === 0) return;
      // Rotate: if currently on a plan in the list, go to next; otherwise first.
      onNav({ view: "plan", slug: blockedPlans[0].slug });
    };
    const handleGen = () => {
      window.dispatchEvent(new CustomEvent("r-open-fleet-prompt"));
    };
    const projectName = M.projects?.[0]?.project || M.project || "project";
    return (
      <div className="r-titlebar">
        <div className="row1">
          <span className="crumbs">sprint</span>
          <span className="title">{s ? `${s.id} · ${s.theme}` : route.sprint}</span>
          <div className="actions">
            <button className="r-nav-btn" disabled={idx <= 0} onClick={() => onNav({ view: "sprint", sprint: sprints[idx - 1].id })}>‹</button>
            <button className="r-nav-btn" disabled={idx >= sprints.length - 1} onClick={() => onNav({ view: "sprint", sprint: sprints[idx + 1].id })}>›</button>
            <button
              className="gen-prompt"
              onClick={handleGen}
              title="Generate fleet prompt"
            >
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4 3h6l2 2v8H4z"/><path d="M10 3v2h2"/><path d="M6 7h4M6 9h4M6 11h2"/>
              </svg>
              Generate prompt
            </button>
          </div>
        </div>
        {s && (
          <div className="row2">
            <span className={`status-pill ${s.status}`}><span className="dot"></span><span>{s.status}</span></span>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">starts</span><span className="v">{s.starts}</span></span>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">ends</span><span className="v">{s.ends}</span></span>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">items</span><span className="v">{s.items.length}</span></span>
            <span style={{ flex: 1 }}></span>
            {totalOpen > 0 && (
              <button className="resolve-btn" onClick={handleResolve} title="Take the next open decision">
                Resolve <span className="resolve-badge">{totalOpen}</span>
              </button>
            )}
          </div>
        )}
      </div>
    );
  }
  return null;
}

// ─── App ────────────────────────────────────────────────────────────────


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.title = {
  LIFECYCLE_STATUSES,
  statusWriteNotice,
  persistStatusPatch,
  StatusMenu,
  TitleBar,
  canonicalReaderKind,
  readerReferenceSlug,
  readerPlanFor,
  readerSourceTrail,
  readerMetadataRows,
  publishRenderedReaderList,
  readerListFor,
  ReaderChrome,
};
