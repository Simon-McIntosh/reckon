// Decision row — select from options and/or give a free-form answer + rationale,
// commit with the button. Backed by semantic <div class="r-dec"> HTML in the plan.

function Decision({ d, onUpdate }) {
  const labels = d.option_labels || {};
  const hasOptions = Array.isArray(d.choices) && d.choices.length > 0;
  const [choice, setChoice] = useState(d.chosen || d.choice || "");
  const [rationale, setRationale] = useState(d.rationale || "");
  const [editing, setEditing] = useState(false);
  const isTaken = !!((d.chosen || d.choice || "").trim() || (d.rationale || "").trim());

  const commit = () => {
    const c = choice.trim();
    if (!c && !rationale.trim()) return;
    onUpdate(c, rationale.trim());
    setEditing(false);
  };

  if (isTaken && !editing) {
    const shown = labels[d.chosen] || d.chosen || d.choice;
    return (
      <div className="r-dec taken">
        <div className="h">
          <span className="key">{d.key}</span>
          <span className="title">{d.title}</span>
          {(d.when || d.by) && <span className="when">✓ {d.when}{d.by ? " · " + d.by : ""}</span>}
        </div>
        <div className="taken-summary">
          {shown && <span className="chosen-tag">{shown}</span>}
          {d.rationale && <span className="rat">{d.rationale}</span>}
          <span className="edit-link" onClick={() => setEditing(true)}>edit</span>
        </div>
      </div>
    );
  }

  return (
    <div className="r-dec editing">
      <div className="h">
        <span className="key">{d.key}</span>
        <span className="title">{d.title}</span>
      </div>
      <div className="r-dec-form">
        {d.context && <div className="ctx">{d.context}</div>}
        {hasOptions && (
          <div className="choices">
            {d.choices.map(c => (
              <button
                key={c}
                className={`choice ${choice === c ? "selected" : ""}`}
                onClick={() => setChoice(choice === c ? "" : c)}
                title="Click to choose; click the button below to commit"
              >
                {labels[c] || c}
              </button>
            ))}
          </div>
        )}
        <div className="rat-row">
          <input
            className="dec-answer"
            placeholder={hasOptions ? "…or type a free-form answer" : "answer"}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") commit(); }}
          />
        </div>
        <div className="rat-row">
          <input
            placeholder="rationale (optional)"
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") commit(); }}
          />
          <button className="upd" onClick={commit}>{isTaken ? "Update" : "Take decision"}</button>
        </div>
      </div>
    </div>
  );
}

window.Decision = Decision;
window.DecisionRow = Decision;
