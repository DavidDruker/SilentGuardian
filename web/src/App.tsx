import { useCallback, useEffect, useState } from "react";
import { ChevronRight, Phone, Settings2, X } from "lucide-react";
import { api, type AppConfig, type GuardianEvent, type Profile, uid } from "./api";
import { Capture } from "./Capture";
import { ProfileEditor } from "./ProfileEditor";
import { EventDetail, pretty, timeLabel, urgencyNames } from "./EventDetail";
import { ContactView } from "./ContactView";
import { PrivacyPanel } from "./Experience";

type Monitoring = {
  person_id: string;
  last_seen: number;
  quality: string;
  source: string;
  online: boolean;
};
const hour = (h?: number) => `${String(h ?? 0).padStart(2, "0")}:00`;
const duration = (s: number) =>
  s >= 60 && s % 60 === 0 ? `${s / 60} min` : `${s} s`;
export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null),
    [profiles, setProfiles] = useState<Profile[]>([]),
    [events, setEvents] = useState<GuardianEvent[]>([]);
  const [monitoring, setMonitoring] = useState<Monitoring[]>([]),
    [mode, setMode] = useState<"care" | "personal">("care"),
    [personId, setPersonId] = useState("margaret");
  const [logged, setLogged] = useState(false),
    [token, setToken] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [editing, setEditing] = useState(false),
    [detail, setDetail] = useState<string | null>(null),
    [page, setPage] = useState("overview"),
    [callBusy, setCallBusy] = useState(false);
  const [hash, setHash] = useState(location.hash);
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const fn = () => setHash(location.hash);
    addEventListener("hashchange", fn);
    return () => removeEventListener("hashchange", fn);
  }, []);
  const refresh = useCallback(async () => {
    try {
      const [p, e, m] = await Promise.all([
        api<Profile[]>("/profiles"),
        api<GuardianEvent[]>("/events"),
        api<Monitoring[]>("/monitoring"),
      ]);
      setProfiles(p);
      setEvents(e);
      setMonitoring(m);
      setLogged(true);
    } catch (e) {
      if (String(e).includes("Sign in")) setLogged(false);
      else setError(String(e));
    }
  }, []);
  useEffect(() => {
    void api<AppConfig>("/config")
      .then(setConfig)
      .catch((e) => setError(String(e)));
    if (!location.hash.startsWith("#contact/")) void refresh();
  }, [refresh]);
  useEffect(() => {
    if (!logged || hash.startsWith("#contact/")) return;
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [logged, refresh, hash]);
  const profile =
    profiles.find((p) => p.id === personId) ||
    profiles.find((p) => p.mode === mode) ||
    profiles[0];
  const current = events.filter(
    (e) => e.person_id === profile?.id && e.source !== "demo_call",
  );
  const active = current.filter((e) => e.status === "open"),
    checkIn = active.find(
      (e) =>
        e.urgency === 1 &&
        profile?.check_in_first &&
        profile.mode === "personal",
    );
  useEffect(() => {
    if (!checkIn) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 500);
    return () => clearInterval(timer);
  }, [checkIn?.id]);
  const firstContact = profile?.contacts
    ?.filter((c) => c.min_urgency <= 2)
    .sort((a, b) => a.delay_seconds - b.delay_seconds)[0];
  const latest = current[0],
    told =
      profile?.mode === "personal" &&
      !checkIn &&
      latest &&
      latest.status !== "dismissed" &&
      latest.notifications.length > 0 &&
      Date.now() / 1000 - latest.opened_at < 1800
        ? latest
        : undefined;
  const firstSend = told?.notifications
    .slice()
    .sort((a, b) => a.due_at - b.due_at)[0];
  const review = active
    .slice()
    .sort((a, b) => b.urgency - a.urgency || b.opened_at - a.opened_at);
  const nav = [
    { id: "overview", label: mode === "care" ? "Care station" : "My home" },
    { id: "events", label: "Event history" },
    { id: "circle", label: mode === "care" ? "Care team" : "Trusted circle" },
    { id: "privacy", label: "Privacy" },
  ];
  const switchMode = (m: "care" | "personal") => {
    setMode(m);
    setPersonId(profiles.find((p) => p.mode === m)?.id || "");
    setPage("overview");
  };
  const login = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await api("/session", "POST", { token });
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };
  const demo = async () => {
    setError("");
    try {
      await api("/session", "POST", { token: "local-demo-only" });
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };
  const acknowledge = async (id: string) => {
    try {
      await api(`/events/${id}/ack`, "POST", {
        author: profile.alias,
        note: "Checked, okay",
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };
  const testCall = async () => {
    setCallBusy(true);
    try {
      const r = await api<{ event_id: string; mode: string }>(
        `/demo/call/${uid()}`,
        "POST",
      );
      setNotice(
        r.mode === "dry_run"
          ? "Demo call queued in simulation. No phone will ring until Voice credentials are configured."
          : "Demo call queued. Provider acceptance will appear in the event timeline.",
      );
      setDetail(r.event_id);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setCallBusy(false);
    }
  };
  const subscribe = async () => {
    try {
      if (!config?.push_public_key)
        throw new Error("Web push keys are not configured yet.");
      const contact = profile.contacts?.find((c) => c.channel === "push");
      if (!contact) throw new Error("Add a push contact in the profile first.");
      if ((await Notification.requestPermission()) !== "granted")
        throw new Error("Notification permission was not granted.");
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const raw = atob(
          config.push_public_key.replace(/-/g, "+").replace(/_/g, "/"),
        ),
        key = Uint8Array.from(raw, (c) => c.charCodeAt(0));
      const sub =
        (await reg.pushManager.getSubscription()) ||
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: key,
        }));
      const serialized = sub.toJSON();
      await api("/push", "POST", {
        person_id: profile.id,
        contact_id: contact.id,
        endpoint: serialized.endpoint,
        keys: serialized.keys,
      });
      setNotice(`This browser is subscribed as ${contact.name}.`);
    } catch (e) {
      setError(String(e));
    }
  };

  if (hash.startsWith("#contact/"))
    return <ContactView parts={hash.slice(1).split("/")} />;
  if (!logged || !config || !profile)
    return (
      <main className="login-page">
        <div className="login-art">
          <img
            className="login-logo"
            src="/brand/hayvn-lockup-reversed.png"
            alt="Hayvn"
          />
          <h1>
            See who needs
            <br />
            a check-in first.
          </h1>
          <p>
            Hayvn notices possible changes from a person’s usual movement.
            <br />
            A person reviews each signal and decides what happens next.
            <br />
            It supports care staff. It does not replace them or diagnose.
          </p>
          <small>Care mode for staff · Personal mode for people at home</small>
        </div>
        <section className="login-form">
          <h2>Sign in to review signals.</h2>
          <p>
            {config?.demo_enabled
              ? "Open the synthetic demo, or sign in with the operator access token for this deployment."
              : "Use the operator access token set for this deployment."}
          </p>
          {config?.demo_enabled && (
            <button className="primary" onClick={demo}>
              Open the local demo
            </button>
          )}
          <form onSubmit={login}>
            <label>
              Operator access token
              <input
                type="password"
                autoComplete="current-password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Enter your access token"
              />
            </label>
            <button className={config?.demo_enabled ? "secondary" : "primary"}>
              Sign in
            </button>
          </form>
          <p className="muted">
            Seeded profiles are synthetic. The server receives body landmarks, not video.
            <br />This is a demonstration, not a medical device.
          </p>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
        </section>
      </main>
    );
  const selectedEvent = events.find((e) => e.id === detail);
  const stream = monitoring.find((m) => m.person_id === profile.id);
  const sameMode = profiles.filter((p) => p.mode === mode);
  const team = mode === "care" ? "care team" : "trusted circle";
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#">
          <img
            className="brand-logo"
            src="/brand/hayvn-lockup.png"
            alt="Hayvn"
          />
        </a>
        <div className="mode-switch">
          <button
            className={mode === "care" ? "selected" : ""}
            onClick={() => switchMode("care")}
          >
            Care
          </button>
          <button
            className={mode === "personal" ? "selected" : ""}
            onClick={() => switchMode("personal")}
          >
            Personal
          </button>
        </div>
        <span className="nav-label">YOUR WORKSPACE</span>
        <nav>
          {nav.map((n) => (
            <button
              key={n.id}
              className={page === n.id ? "active" : ""}
              onClick={() => setPage(n.id)}
            >
              {n.label}
              {n.id === "events" && active.length > 0 && (
                <span className="nav-count">{active.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="privacy-note">
            <h4>What this server receives</h4>
            <p>
              Body landmarks. Video stays on the capture device.
              <br />
              Gemini receives skeleton keyframes only with consent.
            </p>
          </div>
          <button
            className="text-button"
            onClick={() => {
              void api("/session", "DELETE").then(() => setLogged(false));
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            {mode === "care" ? "Care workspace" : "Personal workspace"}
            <ChevronRight size={14} />
            <strong>{nav.find((n) => n.id === page)?.label}</strong>
          </div>
        </header>
        <main className="workspace">
          <div className="page-heading">
            <div>
              <span className="eyebrow">
                {new Date().toLocaleDateString(undefined, {
                  weekday: "long",
                  month: "long",
                  day: "numeric",
                })}
              </span>
              <h1>
                {page === "overview"
                  ? mode === "care"
                    ? "Changes from usual, for staff review."
                    : "Changes from your usual day."
                  : page === "events"
                    ? "Each signal and what happened next."
                    : page === "circle"
                      ? "Who is told, and when."
                      : "What is stored, and who agreed."}
              </h1>
              {page === "overview" && (
                <p>
                  {mode === "care"
                    ? "Possible changes from a resident’s usual movement appear here. A person decides what happens next."
                    : "Possible changes from your usual movement appear here. You choose who is told and when."}
                </p>
              )}
            </div>
          </div>
          {error && (
            <div className="banner error" role="alert">
              <span>{error}</span>
              <button
                className="icon-button"
                onClick={() => setError("")}
                aria-label="Dismiss error"
              >
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="banner notice" role="status">
              <span>{notice}</span>
              <button
                className="icon-button"
                onClick={() => setNotice("")}
                aria-label="Dismiss notice"
              >
                <X size={16} />
              </button>
            </div>
          )}
          <div className="demo-banner">
            <span>
              {profile.data_category === "consenting_teammate"
                ? "Consenting teammate"
                : "Synthetic profile"}{" "}
              ·{" "}
              {config.notification_mode === "dry_run"
                ? "Notifications are simulated"
                : "Live notification delivery enabled"}{" "}
              ·{" "}
              {config.gemini_configured
                ? "Gemini configured"
                : "Rule-based assessment"}
            </span>
            <span>DEMO</span>
          </div>
          {page === "overview" && (
            <>
              {checkIn && (
                <div className="check-in">
                  <div>
                    <span className="eyebrow">CHECK-IN REQUEST</span>
                    <h2>Are you feeling okay, {profile.alias}?</h2>
                    <p>
                      Hayvn noticed a possible change from your usual movement.
                      {checkIn.next_escalation_at
                        ? ` If you do not respond in ${Math.max(0, Math.ceil(checkIn.next_escalation_at - now))} s, ${firstContact?.name || "your trusted circle"} is told.`
                        : " If you do not respond, your circle is told."}
                    </p>
                  </div>
                  <button
                    className="primary"
                    onClick={() => acknowledge(checkIn.id)}
                  >
                    I’m okay
                  </button>
                </div>
              )}
              {told && (
                <div className="told-card" role="status">
                  <span className="eyebrow">TRUSTED CIRCLE</span>
                  {told.status === "acknowledged" ? (
                    <>
                      <h2>{told.acknowledged_by} responded.</h2>
                      <p>Further alerts for this event have stopped.</p>
                    </>
                  ) : (
                    <>
                      <h2>{firstSend?.recipient} was told.</h2>
                      <p>
                        No call or message was needed from you. Waiting for a
                        response.
                      </p>
                    </>
                  )}
                </div>
              )}
              <div className="content-grid">
                <div>
                  <Capture
                    key={profile.id}
                    profile={profile}
                    config={config}
                    onEvent={() => void refresh()}
                    onError={setError}
                  />
                  <section className="panel recent-panel">
                    <div className="panel-heading">
                      <div>
                        <span className="eyebrow">FOR REVIEW</span>
                        <h2>
                          {mode === "care" ? "For review now" : "Open signals"}
                        </h2>
                      </div>
                      <button
                        className="text-button"
                        onClick={() => setPage("events")}
                      >
                        View all
                      </button>
                    </div>
                    <EventList
                      events={review}
                      onSelect={setDetail}
                      empty={[
                        "No open signals.",
                        "Replay a demo scenario or start the camera.",
                      ]}
                    />
                  </section>
                </div>
                <aside className="right-column">
                  <section className="panel person-panel">
                    <div className="panel-heading">
                      <span className="eyebrow">
                        {mode === "care" ? "RESIDENT" : "YOUR PROFILE"}
                      </span>
                      <button
                        className="icon-button"
                        onClick={() => setEditing(true)}
                        aria-label="Edit selected profile"
                      >
                        <Settings2 size={16} />
                      </button>
                    </div>
                    <h2>{profile.alias}</h2>
                    <p>
                      {profile.age} years ·{" "}
                      {mode === "care" ? "Care mode" : "Personal mode"}
                    </p>
                    {sameMode.length > 1 && (
                      <select
                        aria-label="Selected profile"
                        value={profile.id}
                        onChange={(e) => setPersonId(e.target.value)}
                      >
                        {sameMode.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.alias}
                          </option>
                        ))}
                      </select>
                    )}
                    <div className="person-status">
                      <span className={`dot ${stream?.online ? "" : "grey"}`} />
                      {stream?.online
                        ? stream.quality === "uncertain"
                          ? "Visibility uncertain"
                          : stream.source === "replay"
                            ? "Demo replay active"
                            : "Camera connected"
                        : "No active camera"}
                    </div>
                    <div className="profile-details">
                      <span className="eyebrow">CARE CONTEXT</span>
                      {Boolean(profile.conditions?.length) && (
                        <p className="conditions">
                          {profile.conditions?.join(" · ")}
                        </p>
                      )}
                      <p>
                        <strong>
                          {profile.mobility_aid === "none"
                            ? "No mobility aid noted"
                            : `${pretty(profile.mobility_aid || "")} noted in profile`}
                        </strong>
                        <span>
                          {profile.restrict_night_standing
                            ? `Standing without a helper from ${hour(profile.quiet_start)} to ${hour(profile.quiet_end)} is flagged`
                            : "No quiet-hours standing rule"}
                        </span>
                      </p>
                      <p>
                        <strong>
                          {profile.check_in_first
                            ? "Check in with me first"
                            : `${mode === "care" ? "Care team" : "Trusted circle"} is told directly`}
                        </strong>
                        <span>
                          {profile.check_in_first
                            ? `If there is no answer in ${profile.check_in_seconds} s, ${firstContact?.name || `the ${team}`} is told.`
                            : `${firstContact?.name || "Nobody"} is told first.`}
                        </span>
                      </p>
                    </div>
                  </section>
                  <div className="small-note">
                    <span>
                      Low visibility is shown as uncertain. Uncertain does not
                      mean the person is okay.
                    </span>
                  </div>
                </aside>
              </div>
            </>
          )}
          {page === "events" && (
            <section className="panel">
              <div className="panel-heading">
                <h2>Event history</h2>
                <span className="muted">{events.length} events</span>
              </div>
              <EventList
                events={events}
                onSelect={setDetail}
                empty={[
                  "No events yet.",
                  "Start the camera or replay a labelled demo scenario.",
                ]}
              />
            </section>
          )}
          {page === "circle" && (
            <>
              <section className="panel circle-panel">
                <div className="panel-heading">
                  <h2>
                    {profile.alias}’s {team}
                  </h2>
                  {config.push_public_key && (
                    <button className="secondary" onClick={subscribe}>
                      Subscribe this browser
                    </button>
                  )}
                </div>
                <p className="muted">
                  Contacts are told in this order. Urgent signals reach every
                  eligible contact at once.
                </p>
                {profile.contacts?.map((c, i) => (
                  <div className="circle-row" key={c.id}>
                    <span className="circle-order">{i + 1}</span>
                    <div>
                      <h3>{c.name}</h3>
                      <p>
                        {c.role} ·{" "}
                        {c.channel === "call" ? "Phone call" : "Browser push"}
                      </p>
                    </div>
                    <span>{urgencyNames[c.min_urgency]} and above</span>
                    <span className="muted">
                      {c.delay_seconds
                        ? `After ${duration(c.delay_seconds)}`
                        : "Immediately"}
                    </span>
                  </div>
                ))}
                <button className="text-button" onClick={() => setEditing(true)}>
                  Edit contacts and escalation
                </button>
              </section>
              <section className="panel">
                <span className="eyebrow">DEMO PHONE</span>
                <h2>Test the phone call.</h2>
                <p>
                  Rings the configured phone and hangs up without speaking. 1
                  call per event, 5 calls per day, 5 min apart.
                  {config.calls_configured
                    ? ""
                    : " Simulated until Voice credentials are set."}
                </p>
                <button
                  className="primary"
                  disabled={callBusy}
                  onClick={testCall}
                >
                  <Phone size={16} />
                  {callBusy
                    ? "Queuing…"
                    : config.notification_mode === "dry_run"
                      ? "Simulate demo call"
                      : "Ring demo phone"}
                </button>
              </section>
            </>
          )}
          {page === "privacy" && (
            <PrivacyPanel
              profile={profile}
              onError={setError}
              onChange={() => void refresh()}
            />
          )}
          <footer className="workspace-footer">
            <span>Hayvn</span>
            <span>Demonstration build · Not a medical device</span>
            <span>Supports care staff. Does not replace them.</span>
          </footer>
        </main>
      </div>
      {editing && (
        <ProfileEditor
          profile={profile}
          onClose={() => setEditing(false)}
          onSaved={() => void refresh()}
        />
      )}
      {selectedEvent && (
        <EventDetail
          event={selectedEvent}
          profile={profiles.find((p) => p.id === selectedEvent.person_id)}
          onClose={() => setDetail(null)}
          onChange={() => void refresh()}
          onError={setError}
        />
      )}
    </div>
  );
}

function EventList({
  events,
  onSelect,
  empty,
}: {
  events: GuardianEvent[];
  onSelect: (id: string) => void;
  empty: [string, string];
}) {
  if (!events.length)
    return (
      <div className="empty-events">
        <h3>{empty[0]}</h3>
        <p>{empty[1]}</p>
      </div>
    );
  return (
    <div className="event-list">
      {events.map((e) => (
        <button className="event-row" key={e.id} onClick={() => onSelect(e.id)}>
          <div>
            <strong>
              {e.alias} ·{" "}
              {e.rule_hits.length ? pretty(e.rule_hits[0]) : "Demo phone call"}
            </strong>
            <p>{e.summary}</p>
            <small>
              {timeLabel(e.observed_at)} ·{" "}
              {e.source === "replay"
                ? "Synthetic replay"
                : e.source === "demo_call"
                  ? "Demo call"
                  : "Camera"}{" "}
              ·{" "}
              {e.status === "acknowledged"
                ? `Acknowledged by ${e.acknowledged_by}`
                : e.status === "dismissed"
                  ? "Dismissed"
                  : "Open"}
            </small>
          </div>
          <span
            className={e.status === "open" ? `pill urgency-${e.urgency}` : "pill"}
          >
            {urgencyNames[e.urgency]}
          </span>
          <ChevronRight size={16} />
        </button>
      ))}
    </div>
  );
}
