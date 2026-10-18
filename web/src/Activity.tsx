import { useCallback, useEffect, useState } from "react";
import { api, type ActivityHeatmap, type ActivityPage, type Profile } from "./api";
import { pretty, timeLabel, urgencyNames } from "./EventDetail";

type SelectedHour = { day: string; hour: number };

const dayLabel = (day: string) => new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, {
  weekday: "short", month: "short", day: "numeric", timeZone: "UTC",
});
const hourLabel = (hour: number) => `${String(hour).padStart(2, "0")}:00`;

export function Activity({ profile, onOpenEvent }: {
  profile: Profile;
  onOpenEvent: (id: string) => void;
}) {
  const [grid, setGrid] = useState<ActivityHeatmap | null>(null);
  const [selected, setSelected] = useState<SelectedHour | null>(null);
  const [cell, setCell] = useState<ActivityPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [cellLoading, setCellLoading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = await api<ActivityHeatmap>(`/activity/${encodeURIComponent(profile.id)}/heatmap`);
      setGrid(data);
      setError("");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setLoading(false);
    }
  }, [profile.id]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setCellLoading(true);
    void api<ActivityPage>(`/activity/${encodeURIComponent(profile.id)}/cell?day=${selected.day}&hour=${selected.hour}`)
      .then((data) => { if (!cancelled) { setCell(data); setError(""); } })
      .catch((cause) => { if (!cancelled) setError(String(cause)); })
      .finally(() => { if (!cancelled) setCellLoading(false); });
    return () => { cancelled = true; };
  }, [profile.id, selected]);

  const first = grid?.days[0]?.day;
  const last = grid?.days[grid.days.length - 1]?.day;

  return <section className="panel activity-week-panel">
    <div className="panel-heading activity-heading">
      <div><span className="eyebrow">{profile.alias.toUpperCase()}</span><h2>Activity heat map</h2></div>
      {first && last && <span className="activity-range">{dayLabel(first)} – {dayLabel(last)}</span>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {loading && !grid ? <div className="activity-state">Loading activity…</div> : grid && <>
      <div className="activity-week-scroll">
        <div className="activity-week-grid" role="group" aria-label={`Hourly alerts for ${profile.alias}`}>
          <div className="activity-corner" />
          {Array.from({ length: 24 }, (_, hour) => <span className="activity-hour-label" key={hour}>{String(hour).padStart(2, "0")}</span>)}
          {grid.days.map(({ day, hours }) => <div className="activity-day-row" key={day}>
            <span className="activity-day-label">{dayLabel(day)}</span>
            {hours.map(({ hour, incidents, covered }) => {
              const active = selected?.day === day && selected.hour === hour;
              const level = incidents === 0 ? 0 : incidents === 1 ? 1 : incidents === 2 ? 2 : incidents <= 4 ? 3 : 4;
              return <button key={hour} type="button"
                className={`activity-hour-cell level-${level}${covered ? " covered" : ""}${active ? " selected" : ""}`}
                aria-label={`${dayLabel(day)}, ${hourLabel(hour)}: ${incidents} alert${incidents === 1 ? "" : "s"}${covered ? ", monitored" : ""}`}
                aria-pressed={active}
                title={`${dayLabel(day)} · ${hourLabel(hour)} · ${incidents} alert${incidents === 1 ? "" : "s"}`}
                onClick={() => setSelected({ day, hour })}>
                {incidents > 0 && <span>{incidents}</span>}
              </button>;
            })}
          </div>)}
        </div>
      </div>
      {selected && <div className="activity-cell-detail">
        <div className="panel-heading"><h3>{dayLabel(selected.day)} · {hourLabel(selected.hour)}</h3>
          <button type="button" className="text-button" onClick={() => { setSelected(null); setCell(null); }}>Close</button></div>
        {cellLoading ? <div className="activity-state">Loading alerts…</div> : cell?.incidents.length ?
          <div className="event-list">{cell.incidents.map((incident) => <button type="button" className="event-row" key={incident.id}
            onClick={() => onOpenEvent(incident.id)}>
            <div><strong>{incident.rules.length ? pretty(incident.rules[0]) : "Alert"}</strong>
              <p>{incident.summary}</p><small>{timeLabel(incident.observed_at)} · {pretty(incident.status)}</small></div>
            <span className={`pill urgency-${incident.urgency}`}>{urgencyNames[incident.urgency]}</span>
          </button>)}</div> : <div className="activity-state">No alerts in this hour</div>}
        {cell && cell.total > cell.incidents.length && <span className="activity-count">{cell.total} alerts</span>}
      </div>}
    </>}
  </section>;
}
