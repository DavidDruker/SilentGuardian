import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { api } from "./api";
import { urgencyNames } from "./EventDetail";

type ContactEvent = {
  id: string;
  alias: string;
  summary: string;
  urgency: number;
  status: string;
  opened_at: number;
  contact_name: string;
  source: string;
  acknowledged_by: string | null;
};
export function ContactView({ parts }: { parts: string[] }) {
  const [ev, setEv] = useState<ContactEvent | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [, id, contact, token] = parts;
  const refresh = () =>
    api<ContactEvent>(`/contact/${id}/${contact}`, "GET", undefined, token)
      .then(setEv)
      .catch((e) => setError(String(e)));
  useEffect(() => {
    void refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [id, contact, token]);
  const ack = async (note: string) => {
    setBusy(true);
    try {
      await api(
        `/contact/${id}/${contact}/ack`,
        "POST",
        { author: "Contact", note },
        token,
      );
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="contact-page">
      <div className="brand">
        <img className="brand-logo" src="/brand/hayvn-lockup.png" alt="Hayvn" />
      </div>
      <section className="contact-card">
        <span className="eyebrow">YOUR TRUSTED CIRCLE</span>
        {ev ? (
          <>
            <h1>
              {ev.status === "acknowledged"
                ? "Someone is on it."
                : `${ev.alias} could use a check-in.`}
            </h1>
            <span className={`pill urgency-${ev.urgency}`}>
              {urgencyNames[ev.urgency]} · Level {ev.urgency}
            </span>
            <p>{ev.summary}</p>
            <p className="muted">
              {ev.source === "replay"
                ? "This event came from a labelled demo replay."
                : "Please check on the person and decide what support is needed."}
            </p>
            {ev.status === "acknowledged" ? (
              <div className="acknowledged">
                <Check /> Acknowledged by {ev.acknowledged_by}
              </div>
            ) : (
              <div className="contact-buttons">
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => ack("On my way")}
                >
                  I’m on my way
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => ack("Checked, okay")}
                >
                  Checked, they’re okay
                </button>
              </div>
            )}
            <small>
              For {ev.contact_name}. Acknowledging stops future alerts for this
              event.
            </small>
          </>
        ) : (
          <h2>Opening your alert…</h2>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </section>
      <p className="muted">Demonstration only · Human support, always.</p>
    </main>
  );
}
