// Two graph components:
//   DependencyChainView — top-level Graph tab. An index of every dependency
//     endpoint in the project, named handles first, then the unnamed ones,
//     over a detail whose body is the shared DAG layout.
//   RadialFan — plan-view sub-mode. Focal plan centred, deps left, blocks
//     right. Single-hop only. Click satellite to navigate.
//
// Both compute structural dependency depth, never execution ordering.

// Provenance direction is always research → plan → evidence. References may be
// same-project slugs or project-qualified `project:slug#stage` keys.
function _refSlug(ref) {
  return String(ref || "").split("#", 1)[0].split(":").pop();
}

function _artifactKey(artifact) {
  if (artifact?.nav_key) return artifact.nav_key;
  const artifactType = artifact?.type || "plan";
  const archivePart = artifact?.archived ? "archive:" : "";
  return artifactType === "plan" && !archivePart
    ? artifact.slug
    : `${artifactType}:${archivePart}${artifact.slug}`;
}

function _dependencyChainMeasure(plans) {
  plans = plans.filter(p => (p.type || "plan") === "plan");
  const bySlug = Object.fromEntries(plans.map(p => [p.slug, p]));
  const pathLen = {}, pathPrev = {};
  function lp(slug, seen = new Set()) {
    if (pathLen[slug] !== undefined) return pathLen[slug];
    if (seen.has(slug)) return 0;
    seen.add(slug);
    const deps = (bySlug[slug]?.depends_on || [])
      .map(_refSlug)
      .filter(d => bySlug[d]);
    if (deps.length === 0) { pathLen[slug] = 1; return 1; }
    let best = 0, bestPrev = null;
    for (const d of deps) {
      const v = lp(d, new Set(seen));
      if (v > best) { best = v; bestPrev = d; }
    }
    pathLen[slug] = best + 1;
    pathPrev[slug] = bestPrev;
    return pathLen[slug];
  }
  plans.forEach(p => lp(p.slug));
  const live = plans.filter(p => p.status === "active" || p.status === "blocked");
  let critEnd = null, critLen = 0;
  for (const p of live) {
    if (pathLen[p.slug] > critLen) { critLen = pathLen[p.slug]; critEnd = p.slug; }
  }
  const chain = [];
  let cur = critEnd;
  while (cur) { chain.unshift(cur); cur = pathPrev[cur]; }
  return { chain, critLen, critEnd, pathLen, pathPrev, bySlug };
}

function _dependencyClosure(endpointSlug, bySlug) {
  if (!endpointSlug || !bySlug[endpointSlug]) return new Set();
  const visited = new Set();
  function visit(slug) {
    if (visited.has(slug) || !bySlug[slug]) return;
    visited.add(slug);
    for (const dependency of (bySlug[slug].depends_on || [])) {
      visit(_refSlug(dependency));
    }
  }
  visit(endpointSlug);
  return visited;
}

function _openDecisionCount(plan) {
  const decisions = Array.isArray(plan?.decisions) ? plan.decisions : [];
  if (decisions.length) {
    return decisions.filter(decision => !(decision.chosen || decision.choice)).length;
  }
  return Number(plan?.dec_open || 0);
}

// One endpoint view for every endpoint, named or not. A closure exists as soon
// as a plan has prerequisites; an authored handle only decides whether that
// closure has a ship target, so it cannot decide whether the endpoint renders.
function _graphEndpointView(endpoint, members, hopCount, fallbackProject) {
  if (!endpoint) return null;
  const handle = String(endpoint.graph_handle || "").trim();
  const repositoryCounts = {};
  for (const member of members) {
    const repository = String(
      member.project || member.repo || fallbackProject || "unknown"
    );
    repositoryCounts[repository] = (repositoryCounts[repository] || 0) + 1;
  }
  const total = members.length;
  const shipped = members.filter(member => member.status === "shipped").length;
  const held = members.filter(member => member.status === "blocked").length;
  const openDecisions = members.reduce(
    (count, member) => count + _openDecisionCount(member),
    0,
  );
  const structuralDepth = Math.max(1, Number(hopCount || 0));
  return {
    slug: endpoint.slug,
    title: endpoint.title || endpoint.slug,
    named: Boolean(handle),
    handle: handle || "unnamed",
    // The ship skill takes a closure by handle. Without one there is no
    // resolvable target, and an unresolvable string in a copy control is worse
    // than no control, because being paste-ready is its whole value.
    shipLine: handle ? `/reckon-ship graph:${handle}` : null,
    members,
    repositories: Object.entries(repositoryCounts)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([repository, count]) => ({ repository, count })),
    shipped,
    held,
    total,
    shippedPercent: total ? Math.round((shipped / total) * 100) : 0,
    openDecisions,
    structuralDepth,
    averageWidth: total ? (total / structuralDepth).toFixed(2) : "0.00",
  };
}

// An endpoint is a plan nothing else depends on. Named handles lead, in the
// order the roadmap gives them; the rest are the unnamed live endpoints.
function _graphEndpoints(plans, pathLen) {
  const items = plans.filter(plan => (plan.type || "plan") === "plan");
  const bySlug = Object.fromEntries(items.map(plan => [plan.slug, plan]));
  const dependedOn = new Set();
  const prerequisites = plan => (plan.depends_on || [])
    .map(_refSlug)
    .filter(slug => bySlug[slug] && slug !== plan.slug);
  items.forEach(plan => prerequisites(plan).forEach(slug => dependedOn.add(slug)));
  const named = items.filter(plan => String(plan.graph_handle || "").trim());
  const unnamed = items.filter(plan =>
    !String(plan.graph_handle || "").trim()
    && prerequisites(plan).length > 0
    && !dependedOn.has(plan.slug)
  );
  const byDepthThenSlug = (left, right) => {
    const diff = (pathLen[right.slug] || 0) - (pathLen[left.slug] || 0);
    return diff !== 0 ? diff : left.slug.localeCompare(right.slug);
  };
  return [...named.sort(byDepthThenSlug), ...unnamed.sort(byDepthThenSlug)];
}

// One index row per endpoint, each carrying the same derived closure the
// detail draws, so a row and the detail it opens cannot disagree.
function _graphEndpointRows(plans, fallbackProject) {
  const { bySlug, pathLen } = _dependencyChainMeasure(plans);
  return _graphEndpoints(plans, pathLen).map(endpoint => {
    const closure = _dependencyClosure(endpoint.slug, bySlug);
    const members = [...closure].map(slug => bySlug[slug]).filter(Boolean);
    const view = _graphEndpointView(
      endpoint, members, pathLen[endpoint.slug], fallbackProject
    );
    const flag = view.openDecisions
      ? `${view.openDecisions} open`
      : view.held ? `${view.held} held` : "ready";
    return {
      ...view,
      flag,
      flagKind: view.openDecisions ? "open" : view.held ? "held" : "ready",
      done: view.total > 0 && view.shipped === view.total,
    };
  });
}

// The project roadmap owns endpoint membership and closure metrics. This
// adapter changes only field names and display formats for the graph surface.
function _roadmapEndpointRows(endpoints) {
  return (endpoints || []).map(endpoint => {
    const handle = String(endpoint.handle || "").trim();
    const completion = endpoint.completion || {};
    const shipped = Number(completion.shipped || 0);
    const total = Number(completion.total || 0);
    const openDecisions = Number(endpoint.open_decision_count || 0);
    const held = Number(endpoint.held || 0);
    const structuralDepth = Number(endpoint.structural_depth || 0);
    const flag = openDecisions
      ? `${openDecisions} open`
      : held ? `${held} held` : "ready";
    return {
      slug: endpoint.slug,
      title: endpoint.title || endpoint.slug,
      named: Boolean(handle),
      handle: handle || "unnamed",
      shipLine: handle ? `/reckon-ship graph:${handle}` : null,
      members: endpoint.members || [],
      repositories: endpoint.repositories || [],
      shipped,
      held,
      total,
      shippedPercent: Math.round(Number(endpoint.shipped_fraction || 0) * 100),
      openDecisions,
      structuralDepth,
      averageWidth: Number(endpoint.average_width || 0).toFixed(2),
      flag,
      flagKind: openDecisions ? "open" : held ? "held" : "ready",
      done: total > 0 && shipped === total,
    };
  });
}

// ─── One DAG layout, shared by every dependency surface ───────────────────
//
// Positions derive from structural dependency depth and row order, never from
// execution ordering or wall-clock time. Geometry is published so a stylesheet
// and a stage can agree on card size without either restating the numbers.
const DAG_GEOMETRY = {
  cardWidth: 216,
  cardHeight: 82,
  columnGap: 92,
  rowGap: 22,
  // Room above the first row for the column label.
  topInset: 34,
  stageMargin: 20,
  // A routed detour runs this far below the bottom of every column it crosses.
  detourClearance: 24,
  // Co-terminating arrivals are offset by this much around the card centre so
  // two arrowheads into one card stay distinguishable.
  arrivalFan: 8,
  // Horizontal room reserved for the arrowhead in front of the target card.
  arrowLength: 9,
  // A detour is the lowest ink on the stage, so the stage must clear it.
  detourBottomMargin: 30,
  // Quarter-turn control offsets: a short one for the in-column cubic, a
  // longer pair for the shoulders of a routed detour.
  cubicHandle: 38,
  turnHandle: 34,
  turnRun: 68,
};

const DAG_STROKE = "#c9ccd4";
const DAG_HELD_STROKE = "oklch(0.58 0.20 25)";

// Prerequisites that exist in the drawn set. A dependency on a slug outside it
// is not a missing node, it is context the caller chose not to draw, so it
// contributes neither depth nor an edge.
function _dagPrerequisites(plan, bySlug) {
  return (plan?.depends_on || [])
    .map(_refSlug)
    .filter(slug => bySlug[slug] && slug !== plan.slug);
}

// depth = 1 + max(depth of known prerequisites), zero without any. A cycle is
// terminated by the seen set rather than detected: re-entering a slug already
// on the current walk contributes zero, so every depth stays finite.
function _dagDepths(plans, bySlug) {
  const depth = {};
  const resolve = (slug, seen) => {
    if (depth[slug] != null) return depth[slug];
    const plan = bySlug[slug];
    if (!plan || seen.has(slug)) return 0;
    seen.add(slug);
    const prerequisites = _dagPrerequisites(plan, bySlug);
    depth[slug] = prerequisites.length
      ? 1 + Math.max(...prerequisites.map(dep => resolve(dep, new Set(seen))))
      : 0;
    return depth[slug];
  };
  plans.forEach(plan => resolve(plan.slug, new Set()));
  return depth;
}

function _dagLayout(plans, prefix) {
  const G = DAG_GEOMETRY;
  const drawn = (plans || []).filter(plan => plan && plan.slug);
  const bySlug = {};
  drawn.forEach(plan => { bySlug[plan.slug] = plan; });
  const depth = _dagDepths(drawn, bySlug);

  const columnMembers = {};
  drawn.forEach(plan => {
    (columnMembers[depth[plan.slug]] ||= []).push(plan);
  });
  Object.values(columnMembers).forEach(column => column.sort((left, right) =>
    String(left.slug).localeCompare(String(right.slug))
  ));

  const position = {};
  Object.keys(columnMembers).forEach(key => {
    columnMembers[key].forEach((plan, row) => {
      position[plan.slug] = {
        x: Number(key) * (G.cardWidth + G.columnGap),
        y: G.topInset + row * (G.cardHeight + G.rowGap),
      };
    });
  });

  // The bottom edge of a column, which is what a skip-level edge must clear.
  const columnBottom = key => G.topInset
    + Math.max(0, (columnMembers[key] || []).length - 1) * (G.cardHeight + G.rowGap)
    + G.cardHeight;

  const dependedOn = new Set();
  drawn.forEach(plan => _dagPrerequisites(plan, bySlug).forEach(dep => dependedOn.add(dep)));

  const nodes = drawn.map(plan => {
    const at = position[plan.slug];
    const ghost = Boolean(plan.ghost);
    return {
      key: `${prefix}-${plan.slug}`,
      slug: plan.slug,
      project: plan.project || plan.repo || null,
      title: plan.title || plan.slug,
      status: plan.status || "pending",
      // A context node names the sprint it actually belongs to, so a dimmed
      // card explains itself against the sprint in the header.
      statusText: ghost
        ? `${plan.status || "pending"} · ${plan.sprint || "unscheduled"}`
        : (plan.status || "pending"),
      hours: `${Number(plan.effort_hours || 0)}h`,
      percent: Math.round(Number(plan.impl || 0) * 100),
      depth: depth[plan.slug],
      ghost,
      blocked: plan.status === "blocked",
      // A card with neither a prerequisite nor a dependent is an isolate, and
      // reads as one: the connected cards carry the stronger border.
      connected: _dagPrerequisites(plan, bySlug).length > 0 || dependedOn.has(plan.slug),
      borderStyle: ghost ? "dashed" : "solid",
      background: ghost ? "transparent" : "var(--bg)",
      opacity: ghost ? 0.62 : 1,
      x: at.x,
      y: at.y,
      width: G.cardWidth,
      height: G.cardHeight,
    };
  });

  const arrivals = {};
  drawn.forEach(plan => {
    const prerequisites = _dagPrerequisites(plan, bySlug);
    if (prerequisites.length) arrivals[plan.slug] = prerequisites;
  });

  const edges = [];
  let deepestDetour = 0;
  drawn.forEach(plan => {
    (arrivals[plan.slug] || []).forEach((dep, index, all) => {
      const from = position[dep], to = position[plan.slug];
      const x1 = from.x + G.cardWidth;
      const y1 = from.y + G.cardHeight / 2;
      const x2 = to.x - G.arrowLength;
      const y2 = to.y + G.cardHeight / 2
        + (index - (all.length - 1) / 2) * G.arrivalFan;
      const span = depth[plan.slug] - depth[dep];
      let d = null;
      let detourY = null;
      if (span > 1) {
        // Explicit routed path: quarter-turn down, a flat run AT the clearance
        // depth, quarter-turn up. A cubic with both control points at that
        // depth reaches only three quarters of it and passes beneath the
        // intervening cards, which is why this is not a curve.
        let clear = Math.max(y1, y2);
        for (let k = depth[dep] + 1; k < depth[plan.slug]; k += 1) {
          clear = Math.max(clear, columnBottom(k));
        }
        detourY = clear + G.detourClearance;
        deepestDetour = Math.max(deepestDetour, detourY);
        d = `M ${x1} ${y1} C ${x1 + G.turnHandle} ${y1}, ${x1 + G.turnHandle} ${detourY}, ${x1 + G.turnRun} ${detourY}`
          + ` L ${x2 - G.turnRun} ${detourY}`
          + ` C ${x2 - G.turnHandle} ${detourY}, ${x2 - G.turnHandle} ${y2}, ${x2} ${y2}`;
      } else {
        d = `M ${x1} ${y1} C ${x1 + G.cubicHandle} ${y1}, ${x2 - G.cubicHandle} ${y2}, ${x2} ${y2}`;
      }
      const shipped = bySlug[dep].status === "shipped";
      const held = !shipped && plan.status === "blocked";
      edges.push({
        key: `${prefix}-${dep}-${plan.slug}`,
        from: dep,
        to: plan.slug,
        span,
        skip: span > 1,
        d,
        detourY,
        endY: y2,
        head: `${x2},${y2 - 4.5} ${x2 + G.arrowLength},${y2} ${x2},${y2 + 4.5}`,
        held,
        dashed: !shipped,
        stroke: held ? DAG_HELD_STROKE : DAG_STROKE,
        strokeWidth: held ? 1.8 : 1.4,
        dash: shipped ? "0" : "4 3",
      });
    });
  });

  const depths = Object.keys(columnMembers).map(Number);
  const columnCount = Math.max(1, ...depths.map(value => value + 1));
  const rowCount = Math.max(1, ...Object.values(columnMembers).map(column => column.length));
  const columns = [...depths].sort((left, right) => left - right).map(value => ({
    depth: value,
    label: value === 0 ? "no prerequisites" : `depth ${value}`,
    x: value * (G.cardWidth + G.columnGap),
    width: G.cardWidth,
  }));

  return {
    nodes,
    edges,
    columns,
    depth,
    geometry: G,
    width: columnCount * (G.cardWidth + G.columnGap) - G.columnGap + G.stageMargin,
    // The stage clears the row extent and the deepest detour actually emitted,
    // so a routed edge is never drawn outside it.
    height: Math.max(
      G.topInset + rowCount * (G.cardHeight + G.rowGap) + G.stageMargin,
      deepestDetour ? deepestDetour + G.detourBottomMargin : 0,
    ),
  };
}

// ─── Path prompt modal ────────────────────────────────────────────────────

function PathPromptModal({ chain, fullPrereqItems, bySlug, onClose }) {
  const M = window.STATE;
  const items = fullPrereqItems || (chain || []).map(s => bySlug?.[s]).filter(Boolean);
  const blocked = items.some(p => (p.decisions || []).some(d => !(d.chosen || d.choice)));
  const openDecCount = items.reduce((n, p) =>
    n + (p.decisions || []).filter(d => !(d.chosen || d.choice)).length, 0);

  const prompt = (!blocked && window.buildFleetPrompt)
    ? window.buildFleetPrompt(items, M)
    : "";

  React.useEffect(() => {
    const k = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [onClose]);

  const copy = () => {
    navigator.clipboard?.writeText(prompt);
    onClose();
    if (window.flashSaved) window.flashSaved("path prompt copied");
  };

  return (
    <div className="r-modal-scrim" onClick={onClose}>
      <div className="r-modal" onClick={(e) => e.stopPropagation()}>
        <div className="head">
          <div>
            <div className="r-eyebrow">Graph · fleet prompt</div>
            <h3>{chain?.[chain.length - 1] || "—"} · {items.length} plans</h3>
          </div>
          <button className="btn ghost" onClick={onClose}>Close · Esc</button>
        </div>
        {blocked ? (
          <div style={{ padding: "24px 20px", background: "var(--bad-2)", borderRadius: 6, margin: "12px 20px" }}>
            <span style={{ color: "var(--bad)", fontWeight: 600 }}>{openDecCount} open decision{openDecCount === 1 ? "" : "s"} — resolve before generating prompt.</span>
          </div>
        ) : (
          <textarea value={prompt} readOnly spellCheck={false} />
        )}
        <div className="foot">
          <span style={{ color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 11 }}>
            {items.length} plans · {blocked ? "blocked" : `${prompt.length} chars`}
          </span>
          <span style={{ flex: 1 }}></span>
          <button className="btn primary" onClick={copy} disabled={blocked}>Copy to clipboard</button>
        </div>
      </div>
    </div>
  );
}

// ─── Dependency-chain view (top-level Graph tab) ─────────────────────────

function DependencyChainView({ onNav }) {
  const M = window.STATE;

  const rows = React.useMemo(
    () => _roadmapEndpointRows(M?.endpoints),
    [M?.endpoints],
  );

  const [hideDone, setHideDone] = React.useState(false);
  const [selected, setSelected] = React.useState(null);
  const [showPrompt, setShowPrompt] = React.useState(false);

  // Hide-done drops endpoints whose whole closure has shipped, never the one
  // being read: a filter that empties the detail under the reader is a bug.
  const visible = hideDone ? rows.filter(row => !row.done || row.slug === selected) : rows;
  const selectedIndex = Math.max(0, visible.findIndex(row => row.slug === selected));
  const view = visible[selectedIndex] || null;

  const members = view ? view.members : [];
  const endPlan = view
    ? members.find(member => member.slug === view.slug) || null
    : null;
  const stage = React.useMemo(
    () => window.ReckonGraph.layout(members, "g"),
    [members],
  );

  const named = rows.filter(row => row.named);
  const largest = Math.max(0, ...rows.map(row => row.total));
  const lead = named[0] || null;

  const copyShipLine = async (line) => {
    if (!line) return;
    await navigator.clipboard?.writeText(line);
    if (window.flashSaved) window.flashSaved("graph ship line copied");
  };

  const handleGenPrompt = () => {
    if (!view) return;
    if (view.openDecisions > 0) {
      const firstBlocking = members.find(plan => plan.open_decision_count > 0);
      if (firstBlocking) { window.location.hash = `#plan/${firstBlocking.slug}`; return; }
    }
    setShowPrompt(true);
  };

  if (!M) return null;

  if (rows.length === 0) {
    return (
      <div className="r-graph">
        <p className="r-graph-empty">
          No plan in this project depends on another, so there is no dependency
          graph to derive.
        </p>
      </div>
    );
  }

  return (
    <div className="r-graph">
      <section className="r-graph-index" aria-label="Dependency endpoints">
        <header className="r-graph-index-header">
          <span className="r-graph-label">Endpoints</span>
          <span className="r-graph-figure">named<b>{named.length}</b></span>
          <span className="r-graph-figure">unnamed<b>{rows.length - named.length}</b></span>
          <span className="r-graph-figure">largest<b>{largest}</b></span>
          <label className="r-graph-hide-done">
            <input type="checkbox" checked={hideDone}
              onChange={event => setHideDone(event.target.checked)} />
            hide done
          </label>
          {lead && (
            <button
              type="button"
              className="r-graph-ship r-graph-index-ship"
              onClick={() => copyShipLine(lead.shipLine)}
              disabled={lead.openDecisions > 0}
              title={lead.openDecisions
                ? `${lead.openDecisions} open decision${lead.openDecisions === 1 ? "" : "s"} in the closure — shipping is held`
                : `Copy ${lead.shipLine}`}
            >
              {lead.shipLine}
            </button>
          )}
        </header>
        <div className="r-graph-index-rows">
          {visible.map(row => (
            <button
              key={row.slug}
              type="button"
              className={`r-graph-index-row${row.slug === view?.slug ? " selected" : ""}`}
              aria-current={row.slug === view?.slug}
              onClick={() => setSelected(row.slug)}
            >
              <span className={`r-graph-handle-token${row.named ? "" : " unnamed"}`}>
                {row.handle}
              </span>
              <span className="r-graph-index-title">{row.title}</span>
              <span className="r-graph-index-slug">/{row.slug}</span>
              <span className="r-graph-index-bar">
                <i style={{ width: `${row.shippedPercent}%` }} />
              </span>
              <span className="r-graph-index-pct">{row.shippedPercent}%</span>
              <span className="r-graph-index-shape">
                {row.total} members · {row.structuralDepth} deep
              </span>
              <span className="r-graph-index-repos">
                {row.repositories.map(scope => scope.repository).join(" · ")}
              </span>
              <span className={`r-graph-index-flag ${row.flagKind}`}>{row.flag}</span>
            </button>
          ))}
        </div>
      </section>

      {view && (
        <section className="r-graph-detail" aria-label="Endpoint detail">
          <header className="r-graph-header">
            <div className="r-graph-path-nav" aria-label="Endpoint navigation">
              <button type="button" disabled={selectedIndex <= 0}
                onClick={() => setSelected(visible[selectedIndex - 1]?.slug)}>‹</button>
              <span>{selectedIndex + 1}/{visible.length}</span>
              <button type="button" disabled={selectedIndex >= visible.length - 1}
                onClick={() => setSelected(visible[selectedIndex + 1]?.slug)}>›</button>
            </div>
            <span className={`r-graph-handle-token${view.named ? "" : " unnamed"}`}>
              {view.handle}
            </span>
            <strong>{view.title}</strong>
            <span className="r-graph-subtitle">
              endpoint /{view.slug} · closure {view.total} derived
            </span>
            {view.named ? (
              <button
                type="button"
                className="r-graph-ship"
                onClick={() => copyShipLine(view.shipLine)}
                disabled={view.openDecisions > 0}
                title={view.openDecisions
                  ? `${view.openDecisions} open decision${view.openDecisions === 1 ? "" : "s"} in the closure — shipping is held`
                  : `Copy ${view.shipLine}`}
              >
                {view.shipLine}
              </button>
            ) : (
              <span
                className="r-graph-needs-handle"
                title="Author a graph handle on this plan to give its closure a ship target"
              >
                needs plan-graph-handle
              </span>
            )}
          </header>

          <div className="r-graph-authority">
            <span className="r-graph-label">Derived authority</span>
            {view.repositories.map(scope => (
              <span className="r-graph-scope" key={scope.repository}>
                {scope.repository}<b>{scope.count}</b>
              </span>
            ))}
            <span className="r-graph-authority-note">
              repositories enter scope only through closure membership · writes outside scope refused
            </span>
            <span className={`r-graph-ship-state ${view.openDecisions ? "held" : "ready"}`}>
              {view.openDecisions ? `held · ${view.openDecisions} open` : "ready"}
            </span>
          </div>

          <div className="r-graph-layout">
            <nav className="r-graph-members" aria-label="Derived closure membership">
              <span className="r-graph-label">Derived closure</span>
              {view.members.map(member => (
                <a key={member.slug} href={`#plan/${member.slug}`}>
                  <span className="r-graph-member-repo">
                    {member.project || member.repo || M.project}
                  </span>
                  <span className="r-graph-member-title">{member.title || member.slug}</span>
                  <span className="r-graph-member-status">{member.status}</span>
                </a>
              ))}
            </nav>

            <section className="r-graph-canvas-panel" aria-label="Derived dependency closure">
              <div className="r-graph-metrics">
                <div>
                  <span>closure members</span>
                  <strong>{view.total}</strong>
                  <small>derived from the endpoint</small>
                </div>
                <div>
                  <span>average width</span>
                  <strong>{view.averageWidth}</strong>
                  <small>{view.total} members ÷ {view.structuralDepth} hops</small>
                </div>
                <div>
                  <span>longest dependency chain by hop count</span>
                  <strong>{view.structuralDepth}</strong>
                  <small>structural depth only; not execution ordering</small>
                </div>
              </div>

              <div className="r-graph-canvas-scroll">
                <div className="r-graph-canvas-stage"
                  style={{ width: stage.width, height: stage.height }}>
                  {stage.columns.map(column => (
                    <span key={column.depth} className="r-graph-column-label"
                      style={{ left: column.x, width: column.width }}>
                      {column.label}
                    </span>
                  ))}
                  <svg width={stage.width} height={stage.height} aria-hidden="true">
                    {stage.edges.map(edge => (
                      <React.Fragment key={edge.key}>
                        <path d={edge.d} className={edge.held ? "blocked" : ""}
                          strokeDasharray={edge.dash} strokeWidth={edge.strokeWidth} />
                        <polygon points={edge.head} className={edge.held ? "blocked" : ""} />
                      </React.Fragment>
                    ))}
                  </svg>
                  {stage.nodes.map(node => (
                    <a
                      key={node.key}
                      className={`r-graph-node-card ${node.status}`}
                      href={`#plan/${node.slug}`}
                      style={{
                        left: node.x, top: node.y,
                        width: node.width, height: node.height,
                        borderStyle: node.borderStyle, opacity: node.opacity,
                      }}
                      onClick={event => {
                        event.preventDefault();
                        onNav({ view: "plan", slug: _artifactKey(bySlug[node.slug] || node) });
                      }}
                    >
                      <strong>{node.title}</strong>
                      <span>{node.statusText}</span>
                      <em>{node.hours}</em>
                      <i className="r-graph-node-bar">
                        <b style={{ width: `${node.percent}%` }} />
                      </i>
                    </a>
                  ))}
                </div>
              </div>

              <footer className="r-graph-legend">
                <span><i></i>▶ depends on</span>
                <span><i className="blocked"></i>blocking</span>
                <button type="button" className="gen-prompt" onClick={handleGenPrompt}
                  disabled={view.total === 0}
                  title={view.openDecisions > 0
                    ? `${view.openDecisions} open decisions — resolve first`
                    : "Generate fleet prompt"}>
                  Generate prompt
                </button>
              </footer>
            </section>
          </div>
        </section>
      )}

      {showPrompt && window.reckon?.PromptModal && (
        <window.reckon.PromptModal
          planSlug={view?.slug || "graph"}
          initialPrompt={
            window.buildFleetPrompt
              ? window.buildFleetPrompt(members, window.STATE, endPlan?.title)
              : "(prompts.js not loaded)"
          }
          onClose={() => setShowPrompt(false)}
        />
      )}
    </div>
  );
}

window.GraphView = DependencyChainView;
window.DependencyChainView = DependencyChainView;
window.CriticalPathView = DependencyChainView;
window.PathPromptModal = PathPromptModal;

// ─── Plan-view focal graph (card-based, three-column) ────────────────────
// Layout:
//   ┌───────────────┬──────────────────────┬───────────────┐
//   │  DEPENDS ON   │       FOCAL          │    BLOCKS     │
//   │   cards       │       card           │     cards     │
//   └───────────────┴──────────────────────┴───────────────┘
// Edges are drawn as an SVG overlay using getBoundingClientRect once after
// layout settles. Clicking any satellite card navigates to that plan — the
// App-level viewMode keeps us in the Graph tab across the slug change, so the
// new focal slides in with its own dependency cone.

function _PlanCard({ plan, role, focal, onClick }) {
  if (!plan) return null;
  const artifactType = plan.type || "plan";
  const pct = Math.round((plan.impl || 0) * 100);
  const isArchived = plan.archived === "1" || plan.archived === true || plan.archived === "true";
  return (
    <button
      type="button"
      className={`r-gcard r-gcard-${role} ${focal ? "focal" : ""} ${artifactType === "plan" ? plan.status : artifactType} ${isArchived ? "archived" : ""}`}
      data-graph-slug={_artifactKey(plan)}
      onClick={onClick}
      title={plan.title}
    >
      <div className="r-gcard-top">
        <span className={`r-gcard-dot ${artifactType === "plan" ? plan.status : artifactType}`}></span>
        <span className="r-gcard-title">{plan.title}</span>
      </div>
      <div className="r-gcard-meta">
        <code>/{plan.slug}</code>
        <span className="sep">·</span>
        <span>{artifactType === "plan" ? (plan.ms || "—") : artifactType}</span>
        {plan.sprint && (<><span className="sep">·</span><span>{plan.sprint}</span></>)}
      </div>
      {artifactType === "plan" && <div className="r-gcard-bar"><i style={{ width: `${pct}%` }}></i></div>}
      <div className="r-gcard-foot">
        <span className="r-gcard-status">{artifactType === "plan" ? plan.status : (plan.verdict || artifactType)}</span>
        {artifactType === "plan" && <span className="r-gcard-pct">{pct}%</span>}
      </div>
    </button>
  );
}

// Reverse-edge index: makes graph relationships symmetric without requiring
// both sides to author them. If plan A's depends_on lists B, B's effective
// blocks should include A — even if B.blocks doesn't mention A explicitly.
// Memoised on inventory identity so it's recomputed only when the list rebuilds.
function _buildReverseIndex(inventory) {
  const revDeps = {};    // slug → [slugs that depend on this slug] → effective blocks
  const revBlocks = {};  // slug → [slugs that say they block this slug] → effective depends_on
  const researchInputs = {};
  const evidenceOutputs = {};
  for (const p of inventory) {
    const key = _artifactKey(p);
    for (const d of (p.depends_on || [])) {
      (revDeps[_refSlug(d)] = revDeps[_refSlug(d)] || []).push(key);
    }
    for (const b of (p.blocks || [])) {
      (revBlocks[_refSlug(b)] = revBlocks[_refSlug(b)] || []).push(key);
    }
    if (p.type === "research") {
      for (const ref of (p.informs || [])) {
        const target = _refSlug(ref);
        (researchInputs[target] = researchInputs[target] || []).push(key);
      }
    }
    if (p.type === "evidence") {
      for (const ref of [...(p.evidence_for || []), ...(p.verifies || [])]) {
        const source = _refSlug(ref);
        (evidenceOutputs[source] = evidenceOutputs[source] || []).push(key);
      }
    }
  }
  return { revDeps, revBlocks, researchInputs, evidenceOutputs };
}

function _effectiveNeighbours(focal, bySlug, revIdx) {
  if (!focal) return { deps: [], blocks: [] };
  const seen = new Set();
  const focalKey = _artifactKey(focal);
  const collect = (slugs) => {
    const out = [];
    for (const s of (slugs || [])) {
      if (!s || seen.has(s) || s === focalKey) continue;
      seen.add(s);
      const p = bySlug[s];
      if (p) out.push(p);
    }
    return out;
  };
  const depsSeen = new Set();
  const blocksSeen = new Set();
  const pushUnique = (set, out, plans) => {
    for (const p of plans) {
      const key = _artifactKey(p);
      if (!set.has(key)) { set.add(key); out.push(p); }
    }
  };
  const deps = [];
  if (focal.type === "plan" || !focal.type) {
    pushUnique(depsSeen, deps, ((focal.depends_on || []).map(_refSlug).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(depsSeen, deps, ((revIdx.revBlocks[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(depsSeen, deps, ((revIdx.researchInputs[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
  } else if (focal.type === "evidence") {
    const refs = [...(focal.evidence_for || []), ...(focal.verifies || [])].map(_refSlug);
    pushUnique(depsSeen, deps, refs.map(s => bySlug[s]).filter(Boolean));
  }
  const blocks = [];
  if (focal.type === "plan" || !focal.type) {
    pushUnique(blocksSeen, blocks, ((focal.blocks || []).map(_refSlug).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(blocksSeen, blocks, ((revIdx.revDeps[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(blocksSeen, blocks, ((revIdx.evidenceOutputs[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
  } else if (focal.type === "research") {
    pushUnique(blocksSeen, blocks, (focal.informs || []).map(_refSlug).map(s => bySlug[s]).filter(Boolean));
  }
  // Guard against self-loops and dep⇄block overlap (a plan shouldn't appear in
  // both columns — when it does, prefer the explicitly-authored side).
  const blockKeys = new Set(blocks.map(_artifactKey));
  const cleanDeps = deps.filter(p => !blockKeys.has(_artifactKey(p)) || (focal.depends_on || []).map(_refSlug).includes(p.slug));
  return { deps: cleanDeps, blocks };
}

function RadialFan({ focalSlug, onNav, compact = false }) {
  const M = window.STATE;
  if (!M) return null;
  const bySlug = React.useMemo(() => Object.fromEntries(M.inventory.map(p => [_artifactKey(p), p])), [M.inventory]);
  const revIdx = React.useMemo(() => _buildReverseIndex(M.inventory), [M.inventory]);
  const focal = bySlug[focalSlug];

  const containerRef = React.useRef(null);
  const [edges, setEdges] = React.useState([]);
  const [boxSize, setBoxSize] = React.useState({ w: 0, h: 0 });

  // Symmetric: blocks ∪ reverse-deps; depends_on ∪ reverse-blocks.
  const { deps, blocks } = React.useMemo(() => _effectiveNeighbours(focal, bySlug, revIdx), [focal, bySlug, revIdx]);

  // Recompute edge geometry whenever the layout could shift.
  React.useLayoutEffect(() => {
    if (!containerRef.current) return;
    const measure = () => {
      const root = containerRef.current;
      if (!root) return;
      const rect = root.getBoundingClientRect();
      setBoxSize({ w: rect.width, h: rect.height });
      const focalCard = root.querySelector('.r-gcard.focal');
      if (!focalCard) { setEdges([]); return; }
      const fr = focalCard.getBoundingClientRect();
      // Inset endpoints by GAP px so the arrow-tip marker is fully visible
      // (cards have z-index:1 so anything underneath them is clipped).
      const GAP = 6;
      const next = [];
      root.querySelectorAll('.r-gcard-dep').forEach(el => {
        const r = el.getBoundingClientRect();
        next.push({
          id: el.dataset.graphSlug,
          x1: r.right - rect.left + GAP, y1: r.top - rect.top + r.height / 2,
          x2: fr.left - rect.left - GAP, y2: fr.top - rect.top + fr.height / 2,
          dir: "dep",
        });
      });
      root.querySelectorAll('.r-gcard-blk').forEach(el => {
        const r = el.getBoundingClientRect();
        next.push({
          id: el.dataset.graphSlug,
          x1: fr.right - rect.left + GAP, y1: fr.top - rect.top + fr.height / 2,
          x2: r.left   - rect.left - GAP, y2: r.top - rect.top + r.height / 2,
          dir: "blk",
        });
      });
      setEdges(next);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(containerRef.current);
    window.addEventListener("resize", measure);
    return () => { ro.disconnect(); window.removeEventListener("resize", measure); };
  }, [focalSlug, deps.length, blocks.length]);

  if (!focal) return <div style={{ padding: 24, color: "var(--muted)" }}>No plan selected.</div>;

  const goTo = (key) => onNav({ view: "plan", slug: key });
  const empty = deps.length === 0 && blocks.length === 0;

  return (
    <div className={`r-fan-wrap ${compact ? "compact" : ""}`}>
      <div className="r-fan-grid" ref={containerRef}>
        {/* Edges overlay — absolute-positioned SVG behind the cards */}
        <svg className="r-fan-edges" width={boxSize.w} height={boxSize.h} aria-hidden="true">
          <defs>
            <marker id="fanArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 Z" fill="var(--line-2, #c4c4b8)" />
            </marker>
          </defs>
          {edges.map((e, i) => {
            // Smooth horizontal cubic bezier (left/right columns)
            const mx = (e.x1 + e.x2) / 2;
            const d = `M ${e.x1} ${e.y1} C ${mx} ${e.y1}, ${mx} ${e.y2}, ${e.x2} ${e.y2}`;
            return (
              <path
                key={`${e.dir}-${e.id}-${i}`}
                d={d}
                fill="none"
                stroke="var(--line-2, #c4c4b8)"
                strokeWidth="1.5"
                markerEnd="url(#fanArrow)"
              />
            );
          })}
        </svg>

        {/* Column: depends on */}
        <div className="r-fan-col r-fan-col-left">
          <div className="r-fan-col-h">
            <span className="r-fan-col-lbl">Depends on</span>
            <span className="r-fan-col-n">{deps.length}</span>
          </div>
          <div className="r-fan-col-body">
            {deps.length === 0 && <div className="r-fan-col-empty">No upstream dependencies</div>}
            {deps.map(p => (
              <_PlanCard key={_artifactKey(p)} plan={p} role="dep" focal={false} onClick={() => goTo(_artifactKey(p))} />
            ))}
          </div>
        </div>

        {/* Column: focal */}
        <div className="r-fan-col r-fan-col-focal">
          <div className="r-fan-col-h centred">
            <span className="r-fan-col-lbl">Focal</span>
          </div>
          <div className="r-fan-col-body centred">
            <_PlanCard plan={focal} role="focal" focal={true} onClick={() => {}} />
          </div>
        </div>

        {/* Column: blocks */}
        <div className="r-fan-col r-fan-col-right">
          <div className="r-fan-col-h">
            <span className="r-fan-col-lbl">Blocks</span>
            <span className="r-fan-col-n">{blocks.length}</span>
          </div>
          <div className="r-fan-col-body">
            {blocks.length === 0 && <div className="r-fan-col-empty">Nothing downstream</div>}
            {blocks.map(p => (
              <_PlanCard key={_artifactKey(p)} plan={p} role="blk" focal={false} onClick={() => goTo(_artifactKey(p))} />
            ))}
          </div>
        </div>
      </div>

      {empty && (
        <div className="r-fan-empty">This plan stands alone — no direct dependencies or downstream items.</div>
      )}
    </div>
  );
}
window.RadialFan = RadialFan;

window.ReckonGraph = { layout: _dagLayout, geometry: DAG_GEOMETRY };
