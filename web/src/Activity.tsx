import { useMemo } from "react";
import type { Profile } from "./api";
import { profileSeed, sampleUnit } from "./demo-data";

type Day = { date: string; level: number; future: boolean };
type Week = { month: string; days: Day[] };

function localToday(zone: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const part = (name: string) => parts.find((item) => item.type === name)?.value || "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

function calendar(profile: Profile): Week[] {
  const today = new Date(`${localToday(profile.timezone)}T12:00:00Z`);
  const first = new Date(today);
  first.setUTCDate(first.getUTCDate() - first.getUTCDay() - 25 * 7);
  const seed = profileSeed(profile.id);
  const peaks = [3 + seed % 18, 5 + (seed >>> 8) % 18, 7 + (seed >>> 16) % 16];
  const centerDay = 1 + (seed >>> 5) % 5;

  return Array.from({ length: 26 }, (_, weekIndex) => {
    const weekStart = new Date(first);
    weekStart.setUTCDate(first.getUTCDate() + weekIndex * 7);
    return {
      month: weekStart.toLocaleDateString(undefined, { month: "short", timeZone: "UTC" }),
      days: Array.from({ length: 7 }, (_, dayIndex) => {
        const date = new Date(weekStart);
        date.setUTCDate(weekStart.getUTCDate() + dayIndex);
        const future = date > today;
        const dayNumber = Math.floor(date.getTime() / 86400000);
        const random = sampleUnit(seed ^ Math.imul(dayNumber, 2654435761));
        let level = random < 0.16 ? 0 : random < 0.74 ? 1 : random < 0.92 ? 2 : 3;
        const peakDistance = Math.min(...peaks.map((peak) => Math.abs(weekIndex - peak)));
        if (peakDistance <= 1 && Math.abs(dayIndex - centerDay) <= 2) {
          level = peakDistance === 0
            ? random > 0.35 ? 4 : 3
            : random > 0.35 ? 3 : 2;
        }
        return { date: date.toISOString().slice(0, 10), level: future ? 0 : level, future };
      }),
    };
  });
}

export function Activity({ profile }: { profile: Profile }) {
  const weeks = useMemo(() => calendar(profile), [profile]);
  return <section className="panel activity-calendar-panel">
    <div className="panel-heading"><div><span className="eyebrow">{profile.alias.toUpperCase()}</span><h2>Activity heat map</h2></div></div>
    <div className="activity-calendar-scroll">
      <div className="activity-calendar" role="img" aria-label={`Activity for ${profile.alias} over the past 26 weeks`}>
        <div className="calendar-months" aria-hidden="true">
          {weeks.map((week, index) => <span key={index}>{index === 0 || week.month !== weeks[index - 1].month ? week.month : ""}</span>)}
        </div>
        <div className="calendar-body">
          <div className="calendar-weekdays" aria-hidden="true"><span /><span>Mon</span><span /><span>Wed</span><span /><span>Fri</span><span /></div>
          <div className="calendar-weeks">
            {weeks.map((week, index) => <div className="calendar-week" key={index}>
              {week.days.map((day) => <span key={day.date}
                className={`calendar-tile level-${day.level}${day.future ? " future" : ""}`}
                title={day.future ? "" : `${day.date} · ${day.level === 0 ? "No activity" : day.level >= 3 ? "High activity" : "Activity"}`}
              />)}
            </div>)}
          </div>
        </div>
      </div>
    </div>
  </section>;
}
