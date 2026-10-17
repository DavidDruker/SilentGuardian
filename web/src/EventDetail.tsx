import { useState } from "react";
import { Check, X } from "lucide-react";
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
    hourCycle: "h23",
  });

const ruleLabels: Record<string, string> = {
  fall: "fall",
  prolonged_stillness: "fall, then still",
  stumble: "stumble",
  sway: "unsteady sway",
  restricted_standing: "standing without a helper at night",
  bed_exit: "bed exit at night",
  personal_baseline: "change from usual movement",
};
const ruleLabel = (r: string) => ruleLabels[r] ?? pretty(r);

const sourceLabel = (s: string) =>
  s === "replay" ? "Synthetic replay" : s === "demo_call" ? "Demo call" : "Camera";

const channelLabel = (c: string) =>
  c === "push" ? "Browser push" : c === "call" ? "Phone call" : pretty(c);

const stateLabels: Record<string, string> = {
  simulated: "Sent (simulated)",
  provider_accepted: "Accepted by provider",
  pending: "Scheduled",
  retry: "Retrying",
  cancelled: "Stopped",
  suppressed: "Call limit reached",
  configuration_required: "Needs setup",
  unknown: "Status unknown",
};
const stateLabel = (s: string) => stateLabels[s] ?? pretty(s);

/** Send time relative to the signal, so simulated and real clocks never mix. */
const afterSignal = (seconds: number) => {
  const s = Math.round(seconds);
  if (s < 1) return "at the signal";
  return s >= 60 && s % 60 === 0 ? `${s / 60} min after` : `${s} s after`;
};

const assessmentLines: Record<string, string> = {
  rules_only:
    "Second assessment: rules only. Gemini is not set up for this deployment.",
  privacy_blocked:
    "Second assessment off. Gemini needs profile consent and a confirmed billed project.",
  pending: "Second assessment pending. The rule-based level applies now.",
  fallback:
    "The Gemini response did not pass validation. The rule-based level stays.",
};

type Notice = GuardianEvent["notifications"][number];

/** One row per recipient and channel, summarised from its latest send. */
function groupNotifications(list: Notice[]) {
  const groups = new Map<string, Notice[]>();
  for (const n of list) {
    const key = JSON.stringify([n.recipient, n.channel]);
    const group = groups.get(key);
    if (group) group.push(n);
    else groups.set(key, [n]);
  }
  return [...groups].map(([key, items]) => {
    const sorted = [...items].sort((a, b) => a.due_at - b.due_at);
    const latest = sorted[sorted.length - 1];
    return {
      key,
      recipient: latest.recipient,
      channel: latest.channel,
      firstDue: sorted[0].due_at,
      count: sorted.length,
      latest,
      error: [...sorted].reverse().find((n) => n.last_error)?.last_error,
    };
  });
}

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
    if (payload !== null) {
      setPayload(null);
      return;
    }
    try {
      setPayload(await api(`/events/${event.id}/payload-preview`));
    } catch (e) {
      onError(String(e));
    }
  };
  const ack = async () => {
    setBusy(true);
    try {
      await api(`/events/${event.id}/ack`, "POST", {
        author:
          profile?.mode === "care"
            ? "Charge nurse"
            : profile?.alias || "Operator",
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

  const isDemoCall = event.source === "demo_call";
  // Long-lie evidence extends an open fall record, so prefer it for the title.
  const headlineRule = event.rule_hits.includes("prolonged_stillness")
    ? "prolonged_stillness"
    : event.rule_hits[0];
  const title = headlineRule
    ? `${event.alias}: possible ${ruleLabel(headlineRule)}`
    : `${event.alias}: demo phone call`;

  const f = event.features;
  const metrics: [string, string][] = [];
  if (f.posture != null) metrics.push(["Posture", pretty(String(f.posture))]);
  if (f.trunk_angle != null) metrics.push(["Trunk angle", `${f.trunk_angle}°`]);
  if (f.still_seconds != null)
    metrics.push(["Still for", `${f.still_seconds} s`]);
  if (typeof f.helper_present === "boolean")
    metrics.push(["Helper nearby", f.helper_present ? "Yes" : "No"]);

  const verdict = event.verdict;
  const floor = event.floor_decision;
  const proposer = String(floor?.source ?? "").startsWith("Gemini")
    ? "Gemini"
    : "A simulated downgrade";
  const groups = groupNotifications(event.notifications);
  const feedbackButtons = (small: boolean) => {
    const className = small ? "secondary small" : "secondary";
    return (
      <>
        <button className={className} onClick={() => feedback("confirmed")}>
          Real concern
        </button>
        <button className={className} onClick={() => feedback("false_alarm")}>
          False alarm
        </button>
      </>
    );
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
            <h2 id="event-title">{title}</h2>
            <p className="muted">
              Observed {timeLabel(event.observed_at)} ·{" "}
              {sourceLabel(event.source)}
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
        {Number(f.demo_time_scale) > 1 && (
          <p className="simulated-clock">
            Simulated time · {String(f.demo_time_scale)}×.
          </p>
        )}

        {!isDemoCall && (
          <section
            className="detail-section"
            aria-labelledby="detail-noticed-title"
          >
            <span className="eyebrow">NOTICED</span>
            <h3 id="detail-noticed-title">What Hayvn noticed</h3>
            {event.rule_summary && event.rule_summary !== event.summary && (
              <p>{event.rule_summary}</p>
            )}
            {event.rule_hits.length > 0 && (
              <div className="rule-tags">
                {event.rule_hits.map((r) => (
                  <span className="tag" key={r}>
                    {pretty(r)}
                  </span>
                ))}
              </div>
            )}
            {event.keyframes.length > 0 && (
              <div className="keyframes">
                {event.keyframes.map((k, i) => (
                  <div key={i}>
                    <Skeleton poses={[(k.landmarks || []) as Landmark[]]} />
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
            )}
            {metrics.length > 0 && (
              <dl className="metrics-list">
                {metrics.map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            )}
            {event.action_prediction && (
              <ActionPanel prediction={event.action_prediction} />
            )}
          </section>
        )}

        {!isDemoCall && (
          <section
            className="detail-section"
            aria-labelledby="detail-limits-title"
          >
            <span className="eyebrow">CANNOT TELL</span>
            <h3 id="detail-limits-title">What it cannot tell</h3>
            <p>
              Whether {event.alias} is hurt, in pain, or why the movement
              changed. Hayvn sees body landmarks only, with no video or sound.
            </p>
            {event.verdict_status === "validated" && verdict ? (
              <>
                <p>
                  Gemini second assessment (
                  {Math.round(verdict.confidence * 100)}% confidence):{" "}
                  {verdict.summary}
                </p>
                {verdict.evidence.map((e, i) => (
                  <p className="evidence" key={i}>
                    {e.detail}
                  </p>
                ))}
                {verdict.recommended_actions.length > 0 && (
                  <ul className="detail-list">
                    {verdict.recommended_actions.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              assessmentLines[event.verdict_status] && (
                <p className="muted">
                  {assessmentLines[event.verdict_status]}
                </p>
              )
            )}
            {floor && (
              <p className="muted">
                {floor.blocked
                  ? `${proposer} proposed level ${String(floor.proposed)}. Hayvn kept level ${String(floor.applied)}: a second assessment can raise the level, never lower it below the rule level.`
                  : `Applied level ${String(floor.applied)}.`}
              </p>
            )}
            {event.verdict_status !== "not_requested" && (
              <button
                className="text-button"
                aria-expanded={payload !== null}
                onClick={preview}
              >
                What Gemini would receive
              </button>
            )}
            {payload !== null && (
              <details open className="payload-preview">
                <summary>
                  Category-only package. Skeleton keyframes are sent
                  separately.
                </summary>
                <pre>{JSON.stringify(payload, null, 2)}</pre>
              </details>
            )}
          </section>
        )}

        <section
          className="detail-section"
          aria-labelledby="detail-told-title"
        >
          <span className="eyebrow">NOTIFICATIONS</span>
          <h3 id="detail-told-title">Who was told</h3>
          <div className="notification-list">
            {groups.length === 0 ? (
              <p className="muted">
                {event.status === "acknowledged"
                  ? "Acknowledged before contact escalation."
                  : "Waiting for the person’s check-in before contacting their circle."}
              </p>
            ) : (
              groups.map((g) => (
                <div key={g.key}>
                  <span
                    className={`timeline-dot ${g.latest.state === "simulated" ? "simulated" : ""}`}
                  />
                  <div>
                    <strong>{g.recipient}</strong>
                    <small>
                      {channelLabel(g.channel)} ·{" "}
                      {afterSignal(g.firstDue - event.opened_at)} ·{" "}
                      {g.count} send{g.count !== 1 ? "s" : ""}
                    </small>
                    {g.error && <small className="error-text">{g.error}</small>}
                  </div>
                  <span className="pill">{stateLabel(g.latest.state)}</span>
                </div>
              ))
            )}
          </div>
        </section>

        <section
          className="detail-section detail-decision"
          aria-labelledby="detail-decide-title"
        >
          <span className="eyebrow">DECISION</span>
          <h3 id="detail-decide-title">You decide</h3>
          {event.status === "dismissed" ? (
            <>
              <div className="acknowledged">
                Dismissed and logged for review. Future sends for this event
                stopped; continued long-lie evidence can open a new alert and
                raise the minimum level.
              </div>
              <div className="action-row">{feedbackButtons(true)}</div>
            </>
          ) : event.status === "acknowledged" ? (
            <>
              <div className="acknowledged">
                <Check size={18} /> Acknowledged by {event.acknowledged_by}.
                Future sends have stopped.
              </div>
              <div className="action-row">{feedbackButtons(true)}</div>
            </>
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
              <div className="action-row">
                <button className="primary" disabled={busy} onClick={ack}>
                  Acknowledge
                </button>
                {feedbackButtons(false)}
              </div>
            </>
          )}
          {event.human_review && (
            <p className="muted">Human review: {pretty(event.human_review)}</p>
          )}
          {Boolean(profile?.contacts?.length) && (
            <div className="action-row">
              <button className="secondary small" onClick={link}>
                What the contact sees
              </button>
            </div>
          )}
        </section>
      </section>
    </div>
  );
}
