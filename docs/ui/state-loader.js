// state-loader.js — runtime state fetcher for reckon plan pages.
//
// Builds window.STATE from three sources, in priority order:
//   1. state/<project>/projection.json — derived static distributed view
//      or state/<project>/index.json   — legacy central index
//   2. state/<project>/<slug>.json — per-plan state files (per-doc layout)
//   3. /_discover/<project>        — auto-discovery from HTML meta tags
//
// window.STATE_READY is a Promise. Templates wait on it before rendering.
// The same assembly remains callable so an open page can revalidate its state.

window.revalidateProjectState = async function () {
  const PROJECT = (document.querySelector('meta[name="docs-project"]')?.content) ||
                  window.location.pathname.replace(/^\/+/, "").split("/")[0] ||
                  "unknown";
  const discoveryEndpoint = `/_discover/${PROJECT}`;

  window.STATE_LOAD = {
    endpoint: discoveryEndpoint,
    startedAt: Date.now(),
  };
  window.projectStateLoadView = (error = null, now = Date.now()) => ({
    phase: error ? "error" : "pending",
    endpoint: error?.endpoint || window.STATE_LOAD.endpoint,
    httpStatus: Number.isFinite(error?.status) ? error.status : null,
    message: error?.message || "",
    elapsedSeconds: Math.max(
      0,
      Math.floor((now - window.STATE_LOAD.startedAt) / 1000),
    ),
  });

  async function getJson(url, { required = false } = {}) {
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) {
        if (required) throw new Error(`${url} returned HTTP ${r.status}`);
        return null;
      }
      return await r.json();
    } catch (error) {
      if (required) throw error;
      return null;
    }
  }

  const stateBase = `state/${PROJECT}`;
  const canonicalType = (value) => {
    const raw = String(value || "plan").trim().toLowerCase();
    return raw === "doc" ? "research" : raw;
  };
  const mapLegacyCapability = (record) => {
    if (!record || typeof record !== "object" || record.capability || !record.tier) {
      return record;
    }
    const classes = {
      haiku: "routine",
      sonnet: "general",
      opus: "orchestrator",
    };
    const capabilityClass = classes[String(record.tier).toLowerCase()];
    if (!capabilityClass) return record;
    return {
      ...record,
      capability: {
        version: "1.0",
        class: capabilityClass,
        requirements: {},
      },
      compatibility_warning: "legacy tier mapped on read",
    };
  };

  // ── 1. Central index ───────────────────────────────────────────────────
  const projectionBlob = await getJson(`${stateBase}/projection.json`);
  const idxBlob = projectionBlob ||
                  (await getJson(`${stateBase}/index.json`, { required: true }));
  const idx = (idxBlob && idxBlob.data) || {};

  let sprints    = Array.isArray(idx.sprints)
    ? idx.sprints.map(sprint => ({
        ...sprint,
        items: (sprint.items || []).map(item =>
          typeof item === "object" ? mapLegacyCapability(item) : item
        ),
      }))
    : [];
  let milestones = Array.isArray(idx.milestones) ? idx.milestones : [];
  let inventory  = Array.isArray(idx.inventory)  ? idx.inventory  : [];
  let northStars = Array.isArray(idx.north_stars) ? idx.north_stars : [];

  // ── 2. Central-index layout: data.plans[] (no data.inventory) ─────────
  // Handles repos that store plan metadata in a central index.json using
  // data.plans[] (with path fields) instead of data.inventory[] with slugs.
  if (inventory.length === 0 && Array.isArray(idx.plans) && idx.plans.length > 0) {
    const pathToSprint = {};
    for (const s of sprints) {
      for (const it of (s.items || [])) {
        const raw = typeof it === "string" ? it : (it.path || it.slug || "");
        const key = raw.replace(/^.*\//, "").replace(/\.[^.]+$/, "");
        if (key) pathToSprint[key] = s.id;
      }
    }
    inventory = idx.plans.map(pl => {
      const rawPath = pl.path || pl.slug || "";
      const slug = rawPath.replace(/^.*\//, "").replace(/\.[^.]+$/, "")
                   || (pl.title || "plan").toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 48);
      return {
        slug,
        title:    pl.title || slug,
        type:     canonicalType(pl.type || pl.reckon_type),
        status:   pl.status || "pending",
        ms:       pl.milestone || "—",
        roi:      pl.roi    || "mid",
        effort:   pl.effort || "M",
        effort_hours: pl.effort_hours,
        impl:     pl.implementation_fraction || 0,
        dec_open: pl.dec_open || 0,
        blockers: Array.isArray(pl.blocked_by) ? pl.blocked_by.length
                  : (typeof pl.blockers === "number" ? pl.blockers : 0),
        sprint:   pathToSprint[slug] || null,
        last:     pl.last_modified || "",
        summary:  pl.summary || "",
        category: pl.category || "",
        informs:      pl.informs || [],
        evidence_for: pl.evidence_for || [],
        verifies:     pl.verifies || [],
        reviewed_at:  pl.reviewed_at || "",
        recorded_at:  pl.recorded_at || "",
        verdict:      pl.verdict || "",
        environment:  pl.environment || "",
        source:       pl.source || "",
        source_quality: pl.source_quality || "",
        commits:      pl.commits || [],
        artifacts:    pl.artifacts || [],
        _central: true,
      };
    });
  }

  // ── 3. Discovery: always the authoritative inventory source ───────────────
  // Plans are the single source of truth. /_discover/ parses HTML meta tags
  // directly, includes server-computed fields (created, dec_open) that
  // index.json never stores, and is always up-to-date.
  // index.json is only used for project config (sprints, milestones, timeline).
  let disc = null;
  let discoveryResponse;
  try {
    discoveryResponse = await fetch(discoveryEndpoint, { cache: "no-store" });
  } catch (cause) {
    const error = new Error(
      `${discoveryEndpoint} failed: ${cause?.message || "network error"}`
    );
    error.endpoint = discoveryEndpoint;
    error.cause = cause;
    throw error;
  }
  if (discoveryResponse.ok) {
    disc = await discoveryResponse.json();
  } else if (!(projectionBlob && discoveryResponse.status === 404)) {
    const error = new Error(
      `${discoveryEndpoint} returned HTTP ${discoveryResponse.status}`
    );
    error.endpoint = discoveryEndpoint;
    error.status = discoveryResponse.status;
    throw error;
  }
  if (Array.isArray(disc?.north_stars)) northStars = disc.north_stars;
  if (Array.isArray(disc?.inventory) && disc.inventory.length > 0) {
    inventory = disc.inventory;
    if (disc.source_format === "distributed") {
      if (Array.isArray(disc.sprints)) sprints = disc.sprints;
      if (Array.isArray(disc.milestones)) milestones = disc.milestones;
    } else {
      if (!sprints.length    && Array.isArray(disc.sprints))    sprints    = disc.sprints;
      if (!milestones.length && Array.isArray(disc.milestones)) milestones = disc.milestones;
    }
  }
  // disc unavailable (server down) → fall through with inventory from index.json / idx.plans

  // ── 4. Per-plan state travels inside the inventory ─────────────────────
  // Each inventory entry was parsed from its plan page's embedded
  // <script id="reckon-owned sections in (status, decisions, followups,
  // comments, questions). The plan HTML is the sole store — there is no
  // per-plan state JSON to fetch.
  const isArchivedArtifact = (inv) =>
    inv.archived === true || inv.archived === "1" || inv.archived === "true";
  const mergedInventory = inventory.map(inv => {
    const workflowStatus = inv.workflow_status || inv.status || "draft";
    const effectiveStatus = inv.effective_status || workflowStatus;
    return {
      ...mapLegacyCapability(inv),
      workflow_status: workflowStatus,
      effective_status: effectiveStatus,
      status: workflowStatus,
      type: canonicalType(inv.type),
      nav_key: canonicalType(inv.type) === "plan" && !isArchivedArtifact(inv)
        ? inv.slug
        : `${canonicalType(inv.type)}:${isArchivedArtifact(inv) ? "archive:" : ""}${inv.slug}`,
    };
  });
  const plans = Object.fromEntries(mergedInventory.map(inv => [inv.nav_key, inv]));
  const attachmentRelations = mergedInventory.flatMap(source =>
    ["informs", "evidence_for", "verifies"].flatMap(relation =>
      (Array.isArray(source[relation]) ? source[relation] : []).map(target => ({
        relation,
        source: source.nav_key,
        target,
      }))
    )
  );

  // ── 5b. Auto-augment sprint items from inventory.sprint membership ──────
  // Plans with sprint:"X" in their inventory entry appear in that sprint
  // automatically — no explicit sprint.items[] wiring needed.
  const augmentedSprints = sprints.map(s => {
    const explicit = new Set(
      (s.items || []).map(it => typeof it === "string" ? it : it.slug)
    );
    const auto = mergedInventory
      .filter(p => p.type === "plan" && p.sprint === s.id && !explicit.has(p.slug))
      .map(p => p.slug);
    return auto.length ? { ...s, items: [...(s.items || []), ...auto] } : s;
  });
  const activeSprintId = disc?.active_sprint_id ?? idx.active_sprint_id ?? null;
  const activeSprints = augmentedSprints.filter(s => s.status === "active");
  const activeSprintConflict = activeSprints.length === 0
    ? activeSprintId !== null
    : activeSprints.length !== 1 || activeSprints[0].id !== activeSprintId;
  const activeSprint = augmentedSprints.find(s => s.id === activeSprintId)
                    || augmentedSprints.find(s => s.status === "active");

  // ── 6. Assemble window.STATE ───────────────────────────────────────────

  // Ensure projects[0] is populated. Some central-index repos (e.g. imas-efit)
  // have data.plans[] + data.counts + data.milestones at the top level and no
  // data.projects[]. Synthesise one so the SPA components can read uniformly.
  let projects = Array.isArray(idx.projects) ? idx.projects.slice() : [];

  // Live counts derived from the discovered inventory. /_discover is the
  // authoritative plan list; the persisted projects[] counts in index.json go
  // stale (the audit recomputes rollups in its response but never writes them).
  // So whenever a live inventory is present, the counts shown MUST come from it
  // — never from the persisted block. When inventory is empty (GitHub Pages /
  // server down), liveCounts is null and we keep whatever the persisted block
  // holds as the only available fallback.
  const liveCounts = mergedInventory.length > 0 ? (() => {
    const actionable = mergedInventory.filter(p => p.type === "plan");
    const count = (s) => actionable.filter(p => p.effective_status === s).length;
    const lastMods = actionable.map(p => p.last || "").filter(Boolean).sort();
    return {
      plans_count:   actionable.length,
      active:        count("active"),
      blocked:       count("blocked"),
      pending:       count("pending"),
      shipped:       count("shipped"),
      last_modified: lastMods.length ? lastMods[lastMods.length - 1] : (idx.audit_date || ""),
    };
  })() : null;

  if (projects.length === 0) {
    projects = [{
      project:       PROJECT,
      path:          window.location.pathname.replace(/\/$/, "").split("/").pop() || PROJECT,
      published:     "",
      owner:         "",
      ...(liveCounts || {
        plans_count:   (idx.counts && idx.counts.total) || mergedInventory.length,
        active: 0, blocked: 0, pending: 0, shipped: 0,
        last_modified: idx.audit_date || "",
      }),
      milestones,
      top:           [],
      activity30:    [],
      tests_30d:     { pass: 0, runs: 0 },
    }];
  } else {
    // Persisted projects[0] exists: overlay live counts (when available) so the
    // cockpit never shows a stale plan count, and backfill milestones if absent.
    projects = projects.map((p, i) => i === 0
      ? {
          ...p,
          ...(liveCounts || {}),
          milestones: (Array.isArray(p.milestones) && p.milestones.length) ? p.milestones : milestones,
        }
      : p);
  }

  const surfaceState = disc ?? idx;
  const readySet = (
    surfaceState.ready_set &&
    typeof surfaceState.ready_set === "object" &&
    !Array.isArray(surfaceState.ready_set)
  ) ? surfaceState.ready_set : {};
  const endpoints = Array.isArray(surfaceState.endpoints)
    ? surfaceState.endpoints
    : (Array.isArray(readySet.endpoints) ? readySet.endpoints : []);

  window.STATE = {
    today:            new Date().toISOString().slice(0, 10),
    project:          PROJECT,
    projects,
    milestones,
    north_stars:       northStars,
    inventory:        mergedInventory,
    source_format:    disc?.source_format ?? idx.source_format ?? "legacy-index",
    resource_versions: disc?.resource_versions ?? idx.resource_versions ?? {},
    loaded_at:        new Date().toISOString(),
    active_sprint_id: activeSprintId,
    active_sprints:   activeSprints,
    active_sprint_conflict: activeSprintConflict,
    sprints:          augmentedSprints,
    sprint:           activeSprint,
    blockers:         Array.isArray(disc?.blockers) ? disc.blockers
                      : (Array.isArray(idx.blockers) ? idx.blockers : []),
    timeline:         Array.isArray(disc?.timeline) ? disc.timeline
                      : (Array.isArray(idx.timeline) ? idx.timeline : []),
    ready_set:        readySet,
    endpoints,
    attachment_relations: attachmentRelations,
    plans,
  };
  window.STATE_ERROR = null;
  return window.STATE;
};

window.STATE_READY = window.revalidateProjectState().catch(error => {
  window.STATE_ERROR = error;
  throw error;
});

window.watchProjectStateChanges = function (onChange) {
  const project = (document.querySelector('meta[name="docs-project"]')?.content) ||
                  window.location.pathname.replace(/^\/+/, "").split("/")[0] ||
                  "unknown";
  const changes = new EventSource(`/_changes/${project}`);
  changes.addEventListener("change", () => onChange());
  return changes;
};
