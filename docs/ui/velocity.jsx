// The velocity work tab: what the fleet delivered over a window the reader
// names. It renders the payload GET /crew/velocity answers with — the same
// payload crew(view="velocity") and `reckon crew velocity` compose — and
// derives no figure of its own: every number below is copied from the response,
// with only formatting (a fraction as a percentage, seconds as a duration)
// applied for display.
const { useEffect, useState } = React;

const VELOCITY_LINE_CLASSES = [
  ["source", "source"],
  ["tests", "tests"],
  ["plan_evidence_research_html", "plan·evidence"],
  ["figures", "figures"],
  ["docs_state", "docs/state"],
  ["other", "other"],
];

function velocityPercent(ratio) {
  const value = ratio && typeof ratio === "object" ? ratio.value : ratio;
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function velocityNumber(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return String(value);
}

function velocityClock(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toISOString().replace(/\.\d{3}Z$/, "Z");
}

function velocityDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  if (value >= 3600) return `${(value / 3600).toFixed(1)}h`;
  if (value >= 60) return `${(value / 60).toFixed(1)}m`;
  return `${value.toFixed(0)}s`;
}

function velocityWindowLabel(window) {
  if (!window) return "the default window";
  const start = window.start ?? window[0];
  const end = window.end ?? window[1];
  if (!start || !end) return "the default window";
  return `${velocityClock(start)} → ${velocityClock(end)}`;
}

function velocityMetrics(row) {
  return (row && row.metrics) || {};
}

function velocityLineCells(metrics) {
  const lines = metrics.lines || {};
  return VELOCITY_LINE_CLASSES.map(([key, label]) => (
    <td key={key} className="r-velocity-num" title={label}>
      {velocityNumber((lines[key] || {}).added)}
    </td>
  ));
}

function VelocityPromotionCells({ metrics }) {
  const promotions = metrics.promoted_nodes || {};
  return (
    <>
      <td className="r-velocity-num">{velocityNumber(promotions.implement_class)}</td>
      <td className="r-velocity-num">{velocityNumber(promotions.review_investigate)}</td>
      <td className="r-velocity-num">{velocityPercent(promotions.review_share)}</td>
    </>
  );
}

function VelocityTimingCells({ metrics }) {
  const completion = metrics.dispatch_to_completion_seconds || {};
  return (
    <>
      <td className="r-velocity-num">{velocityDuration(completion.median)}</td>
      <td className="r-velocity-num">{velocityDuration(completion.p75)}</td>
      <td className="r-velocity-num">{velocityNumber(metrics.attempts_per_landed_node?.value)}</td>
      <td className="r-velocity-num">
        {velocityPercent(metrics.product_deleted_within_seven_days)}
      </td>
    </>
  );
}

function VelocityAggregateTable({ labelKey, label, rows }) {
  return (
    <section className="r-velocity-block" aria-label={`${label} velocity`}>
      <h2>{label}</h2>
      <div className="r-velocity-scroll">
        <table className="r-velocity-table">
          <thead>
            <tr>
              <th scope="col">{labelKey}</th>
              <th scope="col">impl</th>
              <th scope="col">review</th>
              <th scope="col">review share</th>
              <th scope="col">source</th>
              <th scope="col">tests</th>
              <th scope="col">plan·evidence</th>
              <th scope="col">figures</th>
              <th scope="col">docs/state</th>
              <th scope="col">other</th>
              <th scope="col">del 7d</th>
              <th scope="col">completion median</th>
              <th scope="col">p75</th>
              <th scope="col">attempts/node</th>
            </tr>
          </thead>
          <tbody>
            {(rows || []).map(row => {
              const metrics = velocityMetrics(row);
              const key = row.project ?? row.lane ?? row.day;
              return (
                <tr key={String(key)}>
                  <th scope="row">{String(key)}</th>
                  <VelocityPromotionCells metrics={metrics} />
                  {velocityLineCells(metrics)}
                  <VelocityTimingCells metrics={metrics} />
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function VelocityPlanWeeks({ plans }) {
  const rows = (plans && plans.by_project_week) || [];
  return (
    <section className="r-velocity-block" aria-label="Plans opened and closed by week">
      <h2>Plans opened and closed</h2>
      {rows.length === 0 ? (
        <div className="r-velocity-empty">No plan openings or closings in this window.</div>
      ) : (
        <div className="r-velocity-scroll">
          <table className="r-velocity-table">
            <thead>
              <tr>
                <th scope="col">project</th>
                <th scope="col">week</th>
                <th scope="col">opened</th>
                <th scope="col">closed</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => (
                <tr key={`${row.project}:${row.week_start}:${row.iso_week}`}>
                  <th scope="row">{row.project}</th>
                  <td>{row.iso_week || row.week_start}</td>
                  <td className="r-velocity-num">{velocityNumber(row.opened)}</td>
                  <td className="r-velocity-num">{velocityNumber(row.closed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {plans && plans.definition && (
        <p className="r-velocity-definition">{plans.definition}</p>
      )}
    </section>
  );
}

function velocityRequestUrl(scope, window) {
  const query = new URLSearchParams();
  query.set("project", scope || "*");
  query.set("fields", "plans");
  if (window && window.since && window.until) {
    query.set("since", window.since);
    query.set("until", window.until);
  }
  return `/crew/velocity?${query.toString()}`;
}

function VelocityView({ visibleProjects, mountedProjectCount, selectedProject }) {
  const scope = selectedProject || "*";
  const [window, setWindow] = useState(null);
  const [draftSince, setDraftSince] = useState("");
  const [draftUntil, setDraftUntil] = useState("");
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [inflightWindow, setInflightWindow] = useState(null);

  useEffect(() => {
    let active = true;
    setInflightWindow(window);
    (async () => {
      try {
        const response = await fetch(velocityRequestUrl(scope, window), {
          cache: "no-store",
        });
        const body = await response.json().catch(() => null);
        if (!response.ok || !body || body.ok === false) {
          throw new Error(
            (body && body.detail) ||
              `velocity route returned ${response.status}`
          );
        }
        if (!active) return;
        setPayload(body);
        setError("");
      } catch (cause) {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "velocity route unavailable");
      } finally {
        if (active) {
          setInflightWindow(null);
          setLoaded(true);
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [scope, window]);

  const fetchedWindow = payload?.window
    ? { since: payload.window.start, until: payload.window.end }
    : null;
  const loadingLabel = velocityWindowLabel(inflightWindow || fetchedWindow);
  const mountedLabel = mountedProjectCount || 0;
  const visibleLabel = Array.isArray(visibleProjects) ? visibleProjects.length : 0;

  const applyWindow = () => {
    if (!draftSince || !draftUntil) return;
    setWindow({ since: draftSince, until: draftUntil });
  };

  return (
    <div className="r-velocity-surface">
      <div className="r-velocity-heading">
        <h1>Velocity · {scope === "*" ? "all mounted projects" : scope}</h1>
        <button
          type="button"
          className="r-velocity-reset"
          disabled={!window}
          onClick={() => setWindow(null)}
        >
          Default window
        </button>
        <span>
          {visibleLabel} shown / {mountedLabel} mounted
          {fetchedWindow ? ` · measured ${velocityWindowLabel(fetchedWindow)}` : ""}
        </span>
      </div>

      <div className="r-velocity-picker" role="group" aria-label="Velocity window">
        <label>
          since
          <input
            type="datetime-local"
            value={draftSince}
            onChange={event => setDraftSince(event.target.value)}
          />
        </label>
        <label>
          until
          <input
            type="datetime-local"
            value={draftUntil}
            onChange={event => setDraftUntil(event.target.value)}
          />
        </label>
        <button type="button" onClick={applyWindow} disabled={!draftSince || !draftUntil}>
          Load window
        </button>
      </div>

      {error && (
        <div role="status" className="r-velocity-error">
          {error}
        </div>
      )}

      {inflightWindow !== null || (!loaded && !error) ? (
        <div className="r-velocity-loading" role="status">
          Measuring {loadingLabel} — a cold window can take minutes.
        </div>
      ) : null}

      {payload && !error && inflightWindow === null && (
        <>
          <VelocityAggregateTable labelKey="project" label="By project" rows={payload.by_project} />
          <VelocityAggregateTable labelKey="lane" label="By lane" rows={payload.by_lane} />
          <VelocityAggregateTable labelKey="day" label="By day" rows={payload.by_day} />
          <VelocityPlanWeeks plans={payload.plans} />
        </>
      )}
    </div>
  );
}

window.VelocityView = VelocityView;