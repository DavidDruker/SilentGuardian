import { useState } from "react";
import { X } from "lucide-react";
import { api, type GuardianEvent, type Profile } from "./api";

export const urgencyNames = ["Info", "Review", "Concern", "Urgent"];
export const pretty = (value: string) => value.replaceAll("_", " ");
export const timeLabel = (time: number) =>
  new Date(time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

export function EventDetail({
  event, profile, onClose, onChange, onError,
}: {
  event: GuardianEvent;
  profile?: Profile;
  onClose: () => void;
  onChange: () => void;
  onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const update = async (path: string, body: unknown) => {
    setBusy(true);
    try {
      await api(path, "POST", body);
      onChange();
    } catch (cause) {
      onError(String(cause));
    } finally {
      setBusy(false);
    }
  };
  const metrics: [string, string][] = [];
  const features = event.features;
  if (features.posture != null) metrics.push(["Posture", pretty(String(features.posture))]);
  if (features.trunk_angle != null) metrics.push(["Trunk angle", `${features.trunk_angle}°`]);
  if (features.still_seconds != null) metrics.push(["Still for", `${features.still_seconds} s`]);
  if (typeof features.helper_present === "boolean") metrics.push(["Helper nearby", features.helper_present ? "Yes" : "No"]);
  if (typeof features.visibility === "number") metrics.push(["Visibility", `${Math.round(features.visibility * 100)}%`]);
  if (typeof features.anomaly_score === "number") metrics.push(["Baseline change score", features.anomaly_score.toFixed(1)]);
  return (
    <div className="modal-shade">
      <section className="modal event-modal compact-event" role="dialog" aria-modal="true" aria-labelledby="event-title">
        <header>
          <div>
            <span className={`pill urgency-${event.urgency}`}>{urgencyNames[event.urgency]}</span>
            <h2 id="event-title">{event.alias}: {event.rule_hits.length ? pretty(event.rule_hits[0]) : "Alert"}</h2>
            <p className="muted">{timeLabel(event.observed_at)} · {pretty(event.status)}</p>
          </div>
          <button className="icon-button" aria-label="Close alert" onClick={onClose}><X /></button>
        </header>
        <p className="event-summary">{event.summary}</p>
        {event.source === "sample" && <p className="muted">Simulated example for this profile. No notification was sent.</p>}
        {metrics.length > 0 && <dl className="metrics-list">
          {metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>}
        {event.verdict_status === "validated" && event.verdict && (
          <p className="verdict-summary">{event.verdict.summary}</p>
        )}
        {event.notifications.length > 0 && <section className="compact-notifications">
          <h3>Notifications</h3>
          {event.notifications.map((item) => <p key={item.id}>{item.recipient} · {pretty(item.state)}</p>)}
        </section>}
        {event.status === "open" && <div className="action-row">
          <button className="primary" disabled={busy} onClick={() => void update(`/events/${event.id}/ack`, { author: profile?.alias || "Operator", note: "" })}>Acknowledge</button>
          <button className="secondary" disabled={busy} onClick={() => void update(`/events/${event.id}/feedback`, { label: "false_alarm", note: "" })}>False alarm</button>
        </div>}
        {event.status !== "open" && event.source !== "sample" && <p className="muted">Reviewed</p>}
      </section>
    </div>
  );
}
