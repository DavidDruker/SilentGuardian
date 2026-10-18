import { useMemo, useState } from "react";
import type { GuardianEvent, Profile } from "./api";
import { profileSeed, sampleUnit, type DemoSettings } from "./demo-data";
import { pretty, timeLabel, urgencyNames } from "./EventDetail";
import { LiveActivity } from "./LiveActivity";

type Cell = { hour: number; detections: number };
type Day = { day: string; hours: Cell[] };

function dayInZone(stamp: number, zone: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(stamp * 1000));
  const part = (name: string) => parts.find((item) => item.type === name)?.value || "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

function hourInZone(stamp: number, zone: string) {
  return Number(new Intl.DateTimeFormat("en-US", {
    timeZone: zone, hour: "numeric", hourCycle: "h23",
  }).format(new Date(stamp * 1000)));
}

function sampleGrid(profile: Profile, density: number): Day[] {
  const today = dayInZone(Date.now() / 1000, profile.timezone);
  const seed = profileSeed(profile.id);
  const date = new Date(`${today}T12:00:00Z`);
  return Array.from({ length: 7 }, (_, dayIndex) => {
    const dayDate = new Date(date);
    dayDate.setUTCDate(date.getUTCDate() - 6 + dayIndex);
    const day = dayDate.toISOString().slice(0, 10);
    return {
      day,
      hours: Array.from({ length: 24 }, (_, hour) => {
        const random = sampleUnit(seed + dayIndex * 101 + hour * 7919);
        const neighbor = sampleUnit(seed + dayIndex * 31 + Math.floor(hour / 3) * 53);
        const peak = (hour >= 7 && hour <= 11 || hour >= 16 && hour <= 21) && [1, 2, 4, 5].includes(dayIndex);
        const baseline = hour >= 6 && hour <= 22 ? 0.45 : 0.12;
        const concentration = peak ? 1.35 + neighbor * 0.7 : neighbor * 0.35;
        const detections = density === 0 ? 0 : Math.max(0, Math.round(
          (baseline + concentration + random * 0.48) * (density / 100) * 13 - 2,
        ));
        return { hour, detections };
      }),
    };
  });
}

export function Activity({ profile, settings, alerts, database }: {
  profile: Profile; settings: DemoSettings; alerts: GuardianEvent[]; database: string;
}) {
  const [showLive, setShowLive] = useState(false);
  const [selected, setSelected] = useState<{ day: string; hour: number } | null>(null);
  const grid = useMemo(() => sampleGrid(profile, settings.density), [profile, settings.density]);
  const max = Math.max(1, ...grid.flatMap((row) => row.hours.map((cell) => cell.detections)));
  const selectedCount = selected ? grid.find((row) => row.day === selected.day)?.hours[selected.hour]?.detections || 0 : 0;
  const visible = selected ? alerts.filter((alert) =>
    dayInZone(alert.opened_at, profile.timezone) === selected.day &&
    hourInZone(alert.opened_at, profile.timezone) === selected.hour,
  ) : alerts;

  const sourceSwitch = <div className="activity-source-switch" role="group" aria-label="Activity data source">
    <button type="button" className={!showLive ? "selected" : ""} onClick={() => setShowLive(false)}>Sample</button>
    <button type="button" className={showLive ? "selected" : ""} onClick={() => setShowLive(true)}>Live</button>
  </div>;
  if (showLive) return <div className="activity-layout">{sourceSwitch}<LiveActivity profile={profile} database={database} /></div>;

  return <div className="activity-layout">
    {sourceSwitch}
    <section className="panel activity-panel">
      <div className="panel-heading"><div><span className="eyebrow">SAMPLE DATA · {profile.alias.toUpperCase()}</span><h2>Movement heat map</h2></div></div>
      <p>Sample movement detections across the last seven days. Taller, deeper blue columns show busier hours.</p>
      <div className="activity-grid-scroll"><div className="activity-grid">
        <div className="activity-day-label" />
        {Array.from({ length: 24 }, (_, hour) => <span key={hour} className="activity-hour-label">{hour % 3 === 0 ? String(hour).padStart(2, "0") : ""}</span>)}
        {grid.map((row) => <div className="activity-row" key={row.day}>
          <span className="activity-day-label">{new Date(`${row.day}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</span>
          {row.hours.map((cell) => {
            const intensity = cell.detections / max;
            return <button key={cell.hour} type="button"
              className={`activity-cell ${cell.detections ? "has-activity" : ""} ${selected?.day === row.day && selected.hour === cell.hour ? "selected" : ""}`}
              title={`${row.day} ${String(cell.hour).padStart(2, "0")}:00 · ${cell.detections} sample detections`}
              aria-label={`${row.day} ${cell.hour}:00, ${cell.detections} sample movement detections`}
              aria-pressed={selected?.day === row.day && selected.hour === cell.hour}
              onClick={() => setSelected((old) => old?.day === row.day && old.hour === cell.hour ? null : { day: row.day, hour: cell.hour })}>
              {cell.detections > 0 && <span className="activity-column" style={{
                height: `${5 + intensity * 52}px`,
                background: `linear-gradient(180deg, hsl(215 91% ${72 - intensity * 39}%), hsl(213 88% ${83 - intensity * 47}%))`,
              }} />}
            </button>;
          })}
        </div>)}
      </div></div>
      <p className="activity-legend"><span className="activity-legend-low" /> Fewer detections <span className="activity-legend-high" /> More detections</p>
      <small>Times shown in {profile.timezone}. Select an hour to inspect it. All values are simulated.</small>
    </section>
    <section className="panel activity-panel">
      <div className="panel-heading"><div><span className="eyebrow">SAMPLE ALERTS</span><h2>{selected ? `${selected.day} at ${String(selected.hour).padStart(2, "0")}:00` : "Recent examples"}</h2></div>
        {selected && <button className="text-button" onClick={() => setSelected(null)}>Clear filter</button>}
      </div>
      {selected && <p>{selectedCount} simulated movement detections in this hour.</p>}
      {visible.length === 0 ? <p>No sample alerts in this period.</p> : visible.map((alert) =>
        <article key={alert.id} className="activity-incident">
          <div className="activity-incident-head"><strong>{alert.summary}</strong><span className={`pill urgency-${alert.urgency}`}>{urgencyNames[alert.urgency]}</span></div>
          <p>{dayInZone(alert.observed_at, profile.timezone)} · {timeLabel(alert.observed_at)} · sample</p>
          <small>Rule: {alert.rule_hits.map(pretty).join(", ")}</small>
        </article>)}
    </section>
  </div>;
}
