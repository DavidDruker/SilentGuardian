import { useEffect, useMemo, useState } from "react";
import { api, type Profile } from "./api";

type Stage = { ts: number; stage: string; outcome: string; urgency: number | null; details: Record<string, unknown> };
type Incident = { id: string; observed_at: number; opened_at: number; summary: string; rules: string[];
  source: string; urgency: number; status: string; gemini_status: string; stages: Stage[] };
type Timeline = { total: number; incidents: Incident[] };
type Cell = { hour: number; incidents: number; covered: boolean };
type Heatmap = { timezone: string; meaning: string; days: { day: string; hours: Cell[] }[] };

const format = (stamp: number, zone: string) => new Intl.DateTimeFormat(undefined, {
  timeZone: zone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
}).format(new Date(stamp * 1000));
const label = (value: string) => value.replaceAll("_", " ");

export function Activity({ profile, database }: { profile: Profile; database: string }) {
  const [grid, setGrid] = useState<Heatmap | null>(null);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [cellTimeline, setCellTimeline] = useState<Timeline | null>(null);
  const [selected, setSelected] = useState<{ day: string; hour: number } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const to = Date.now() / 1000 + 60;
        const from = to - 8 * 86400;
        const [heat, events] = await Promise.all([
          api<Heatmap>(`/activity/${profile.id}/heatmap`),
          api<Timeline>(`/activity/${profile.id}/timeline?from_ts=${from}&to_ts=${to}&limit=100`),
        ]);
        if (live) { setGrid(heat); setTimeline(events); setError(""); setLoading(false); }
      } catch (cause) {
        if (live) { setError(String(cause)); setLoading(false); }
      }
    };
    void load();
    const interval = setInterval(() => void load(), 10000);
    return () => { live = false; clearInterval(interval); };
  }, [profile.id]);

  useEffect(() => {
    if (!selected || !grid) { setCellTimeline(null); return; }
    let live = true;
    setCellTimeline(null);
    void api<Timeline>(`/activity/${profile.id}/cell?day=${selected.day}&hour=${selected.hour}&tz=${encodeURIComponent(grid.timezone)}`)
      .then((rows) => { if (live) { setCellTimeline(rows); setError(""); } })
      .catch((cause) => { if (live) setError(String(cause)); });
    return () => { live = false; };
  }, [profile.id, selected, grid?.timezone]);

  const visible = useMemo(() => {
    if (!timeline || !grid) return [];
    return selected ? cellTimeline?.incidents || [] : timeline.incidents;
  }, [timeline, grid, selected, cellTimeline]);

  if (loading) return <section className="panel activity-panel">Loading activity…</section>;
  if (error) return <section className="panel activity-panel error" role="alert">{error}</section>;
  if (!grid || !timeline) return null;
  return <div className="activity-layout">
    <section className="panel activity-panel">
      <div className="panel-heading"><div><span className="eyebrow">{database === "PostgreSQL" ? "TIGER DATA ACTIVITY" : "LOCAL ACTIVITY"}</span><h2>Incident heat map</h2></div></div>
      <p>Each square counts incident openings. A dot means pose monitoring was active during that hour.</p>
      <div className="activity-grid-scroll"><div className="activity-grid">
        <div className="activity-day-label" />
        {Array.from({ length: 24 }, (_, hour) => <span key={hour} className="activity-hour-label">{hour % 3 === 0 ? String(hour).padStart(2, "0") : ""}</span>)}
        {grid.days.map((row) => <div className="activity-row" key={row.day}>
          <span className="activity-day-label">{new Date(`${row.day}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</span>
          {row.hours.map((cell) => <button key={cell.hour} type="button"
            className={`activity-cell ${cell.incidents ? "has-incident" : ""} ${selected?.day === row.day && selected.hour === cell.hour ? "selected" : ""}`}
            title={`${row.day} ${String(cell.hour).padStart(2, "0")}:00 · ${cell.incidents} incident${cell.incidents === 1 ? "" : "s"} · ${cell.covered ? "monitored" : "no coverage"}`}
            aria-label={`${row.day} ${cell.hour}:00, ${cell.incidents} incidents, ${cell.covered ? "monitored" : "no coverage"}`}
            onClick={() => setSelected((old) => old?.day === row.day && old.hour === cell.hour ? null : { day: row.day, hour: cell.hour })}>
            {cell.incidents > 0 ? cell.incidents : cell.covered ? <span className="coverage-dot" /> : null}
          </button>)}
        </div>)}
      </div></div>
      <p className="activity-legend"><span className="activity-legend-incident" /> Incident opening <span className="coverage-dot" /> Monitoring coverage <span className="activity-legend-empty" /> Offline or no usable poses</p>
      <small>Times shown in {grid.timezone}. Select a square to filter the timeline.</small>
    </section>
    <section className="panel activity-panel">
      <div className="panel-heading"><div><span className="eyebrow">EVENT TRAIL</span><h2>{selected ? `${selected.day} at ${String(selected.hour).padStart(2, "0")}:00` : "Recent incidents"}</h2></div>
        {selected && <button className="text-button" onClick={() => setSelected(null)}>Clear filter</button>}
      </div>
      {selected && !cellTimeline ? <p>Loading selected hour…</p> : visible.length === 0 ? <p>No incidents in this period.</p> : visible.map((incident) =>
        <article key={incident.id} className="activity-incident">
          <div className="activity-incident-head"><strong>{incident.summary}</strong><span className={`pill urgency-${incident.urgency}`}>Level {incident.urgency}</span></div>
          <p>{format(incident.observed_at, grid.timezone)} · {label(incident.source)}{incident.source === "replay" ? " · synthetic demo" : ""} · {label(incident.status)}</p>
          <div className="activity-stages">{incident.stages.map((stage, index) => <span key={`${stage.ts}-${index}`} title={format(stage.ts, grid.timezone)}>{label(stage.stage)}: {label(stage.outcome)}</span>)}</div>
          <small>Rule: {incident.rules.map(label).join(", ") || "—"} · Gemini: {label(incident.gemini_status)}</small>
        </article>)}
      {(selected ? cellTimeline : timeline) && (selected ? cellTimeline! : timeline).total > visible.length &&
        <p className="muted">Showing the latest {visible.length} of {(selected ? cellTimeline! : timeline).total} incidents.</p>}
    </section>
  </div>;
}
