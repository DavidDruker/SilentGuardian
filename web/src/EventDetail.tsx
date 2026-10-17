import { useState } from "react";
import { Check, ExternalLink, MessageSquare, X } from "lucide-react";
import { api, type GuardianEvent, type Landmark, type Profile } from "./api";
import { Skeleton } from "./Skeleton";
import { ActionPanel } from "./ActionPanel";

export const urgencyNames = ["Info", "Review", "Concern", "Urgent"];
export const pretty = (s: string) => s.replaceAll("_", " ");
export const timeLabel = (t: number) =>
  new Date(t * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

export function EventDetail({
  event,
  profile,
  onClose,
  onChange,
  onError,
}: {
  event: GuardianEvent;
  profile?: Profile;
  onClose: () => void;
  onChange: () => void;
  onError: (s: string) => void;
}) {
  const [note, setNote] = useState(""),
    [busy, setBusy] = useState(false);
  const [payload, setPayload] = useState<unknown>(null);
  const preview = async () => {
    try {
      setPayload(await api(`/events/${event.id}/payload-preview`));
    } catch (e) {
      onError(String(e));
    }
  };
  const demonstrateFloor = async () => {
    try {
      await api(`/demo/floor/${event.id}`, "POST");
      onChange();
    } catch (e) {
      onError(String(e));
    }
  };
  const ack = async () => {
    setBusy(true);
    try {
      await api(`/events/${event.id}/ack`, "POST", {
        author: "Demo operator",
        note,
      });
      onChange();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const feedback = async (label: string) => {
    try {
      await api(`/events/${event.id}/feedback`, "POST", { label, note });
      onChange();
    } catch (e) {
      onError(String(e));
    }
  };
  const link = async () => {
    try {
      const r = await api<{ url: string }>(
        `/events/${event.id}/contact-link/${profile!.contacts![0].id}`,
      );
      window.open(r.url, "_blank", "noopener,noreferrer");
    } catch (e) {
      onError(String(e));
    }
  };
  return (
    <div className="modal-shade">
      <section
        className="modal event-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="event-title"
      >
        <header>
          <div>
            <span className={`pill urgency-${event.urgency}`}>
              Level {event.urgency} · {urgencyNames[event.urgency]}
            </span>
            <h2 id="event-title">{event.alias}’s event record.</h2>
            <p className="muted">
              {timeLabel(event.opened_at)} · Profile v{event.profile_version} ·{" "}
              {pretty(event.source)}
            </p>
          </div>
          <button
            className="icon-button"
            aria-label="Close event"
            onClick={onClose}
          >
            <X />
          </button>
        </header>
        <p className="event-summary">{event.summary}</p>
        {Number(event.features.demo_time_scale) > 1 && (
          <p className="simulated-clock">
            SIMULATED TIME · {String(event.features.demo_time_scale)}×. Observed
            clock: {timeLabel(event.observed_at)}.
          </p>
        )}
        <div className="comparison-grid">
          <section>
            <span className="eyebrow">RULES ONLY</span>
            <h3>Observed pattern + minimum level</h3>
            <p>{event.rule_summary}</p>
            <strong>Minimum level {event.floor_urgency}</strong>
          </section>
          <section>
            <span className="eyebrow">GEMINI ASSESSMENT</span>
            <h3>
              {event.verdict_status === "validated"
                ? "Validated response"
                : "Rule-based fallback active"}
            </h3>
            <p>
              {event.verdict?.summary ||
                (event.verdict_status === "privacy_blocked"
                  ? "External assessment is off: profile consent and a confirmed billed project are required."
                  : "No validated Gemini response is available. The provisional alert stays active.")}
            </p>
            <span className="pill">{pretty(event.verdict_status)}</span>
          </section>
        </div>
        {event.floor_decision && (
          <div className="floor-decision" role="status">
            <strong>{String(event.floor_decision.source)}</strong>
            <p>
              Proposed level {String(event.floor_decision.proposed)} → minimum
              level {String(event.floor_decision.floor)} → applied level{" "}
              {String(event.floor_decision.applied)}.{" "}
              {event.floor_decision.blocked
                ? "Downgrade blocked."
                : "Minimum level kept."}
            </p>
          </div>
        )}
        <div className="action-row">
          <button className="secondary small" onClick={preview}>
            Preview Gemini payload
          </button>
          {event.source === "replay" && (
            <button className="secondary small" onClick={demonstrateFloor}>
              Demonstrate blocked downgrade
            </button>
          )}
        </div>
        {payload !== null && (
          <details open className="payload-preview">
            <summary>
              Exact category-only package preview · skeleton keyframes listed
              separately
            </summary>
            <pre>{JSON.stringify(payload, null, 2)}</pre>
          </details>
        )}
        <div className="rule-tags">
          {event.rule_hits.map((r) => (
            <span className="tag" key={r}>
              {pretty(r)}
            </span>
          ))}
        </div>
        {event.keyframes.length > 0 && (
          <>
            <h3>Skeleton keyframes</h3>
            <div className="keyframes">
              {event.keyframes.map((f, i) => (
                <div key={i}>
                  <Skeleton poses={[(f.landmarks || []) as Landmark[]]} />
                  <span>
                    {i === 0
                      ? "Window start"
                      : i === 1
                        ? "Window midpoint"
                        : "At trigger"}{" "}
                    · skeleton only
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
        <div className="event-columns">
          <section>
            <h3>Observed movement</h3>
            <dl className="metrics-list">
              {Object.entries(event.features)
                .filter(([k, v]) => v !== null && !k.endsWith("_json"))
                .map(([k, v]) => (
                  <div key={k}>
                    <dt>{pretty(k)}</dt>
                    <dd>{String(v)}</dd>
                  </div>
                ))}
            </dl>
          </section>
          <section>
            <h3>Assessment</h3>
            <p>
              <span className="pill">
                {event.verdict_status === "validated"
                  ? "Gemini · validated"
                  : "Rule-based assessment"}
              </span>
            </p>
            <p className="muted">
              Minimum level: {event.floor_urgency}.{" "}
              {event.verdict_status === "fallback"
                ? "Gemini was unavailable or its response did not pass validation."
                : ""}
            </p>
            {event.verdict?.evidence.map((e, i) => (
              <p className="evidence" key={i}>
                <code>{e.ref}</code>
                <br />
                {e.detail}
              </p>
            ))}
            {event.verdict?.recommended_actions.map((s, i) => (
              <p key={i}>{s}</p>
            ))}
            <p className="muted">
              This signal is for staff review. It is not a diagnosis.
            </p>
          </section>
        </div>
        {event.action_prediction && <ActionPanel prediction={event.action_prediction} />}
        {Boolean(event.baseline_contributions?.length) && (
          <section>
            <h3>Personal baseline evidence</h3>
            {event.baseline_contributions?.map((c, i) => (
              <p key={i}>
                {pretty(String(c.feature))}: {Number(c.value).toFixed(3)} vs
                median {Number(c.median).toFixed(3)} · modified z ={" "}
                {Number(c.z).toFixed(2)}
              </p>
            ))}
          </section>
        )}
        <h3>Notification timeline</h3>
        {event.notifications.length > 12 && (
          <p className="muted">
            Showing the latest 12 of {event.notifications.length} attempts. All
            attempts remain stored in the event record.
          </p>
        )}
        <div className="notification-list">
          {event.notifications.length === 0 ? (
            <p className="muted">
              {event.status === "acknowledged"
                ? "Acknowledged before contact escalation."
                : "Waiting for the person’s check-in before contacting their circle."}
            </p>
          ) : (
            event.notifications.slice(-12).map((n) => (
              <div key={n.id}>
                <span
                  className={`timeline-dot ${n.state === "simulated" ? "simulated" : ""}`}
                />
                <div>
                  <strong>{n.recipient}</strong>
                  <small>
                    {pretty(n.channel)} · {timeLabel(n.due_at)} · {n.attempts}{" "}
                    attempt{n.attempts !== 1 ? "s" : ""}
                  </small>
                  {n.last_error && (
                    <small className="error-text">{n.last_error}</small>
                  )}
                </div>
                <span className="pill">{pretty(n.state)}</span>
              </div>
            ))
          )}
        </div>
        {event.status === "dismissed" ? (
          <div className="acknowledged">
            Dismissed and logged for review. Future sends for this event
            stopped; continued long-lie evidence can open a new alert and raise
            the minimum level.
          </div>
        ) : event.status === "acknowledged" ? (
          <div className="acknowledged">
            <Check size={18} /> Acknowledged by {event.acknowledged_by}. Future
            sends have stopped.
          </div>
        ) : (
          <>
            <label className="note-label">
              Response note
              <textarea
                maxLength={500}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="On my way, or checked and okay…"
              />
            </label>
            <button className="primary" disabled={busy} onClick={ack}>
              <Check size={16} /> Acknowledge event
            </button>
          </>
        )}
        <footer>
          <div>
            <button
              className="text-button"
              onClick={() => feedback("confirmed")}
            >
              <MessageSquare size={14} /> Confirm event
            </button>
            <button
              className="text-button"
              onClick={() => feedback("false_alarm")}
            >
              Dismiss as false alarm
            </button>
          </div>
          {event.human_review && (
            <span className="pill">
              Human review: {pretty(event.human_review)}
            </span>
          )}
          {Boolean(profile?.contacts?.length) && (
            <button className="secondary small" onClick={link}>
              Contact view <ExternalLink size={13} />
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}
