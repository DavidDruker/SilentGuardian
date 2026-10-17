import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  ArrowRight,
  Bell,
  Check,
  ChevronRight,
  CircleHelp,
  HeartHandshake,
  LayoutDashboard,
  Leaf,
  LockKeyhole,
  LogOut,
  Phone,
  Settings2,
  ShieldCheck,
  Users,
  X,
} from "lucide-react";
import { api, type AppConfig, type GuardianEvent, type Profile } from "./api";
import { Capture } from "./Capture";
import { ProfileEditor } from "./ProfileEditor";
import { EventDetail, pretty, timeLabel, urgencyNames } from "./EventDetail";
import { ContactView } from "./ContactView";
import { EvidencePanel, PrivacyPanel, TrendsPanel } from "./Experience";

type Monitoring = {
  person_id: string;
  last_seen: number;
  quality: string;
  source: string;
  online: boolean;
};
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
  const [checkingPose, setCheckingPose] = useState(false);
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
        `/demo/call/${crypto.randomUUID()}`,
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

  const checkPose = () => {
    setCheckingPose(true);
    const worker = new Worker("/pose-worker.js");
    const timeout = setTimeout(() => {
      worker.terminate();
      setCheckingPose(false);
      setError(
        "Pose model check timed out. Rebuild the local assets and try again.",
      );
    }, 30000);
    worker.onmessage = (event) => {
      if (event.data.type === "ready") {
        const canvas = new OffscreenCanvas(640, 480),
          context = canvas.getContext("2d")!;
        context.fillStyle = "#ffffff";
        context.fillRect(0, 0, 640, 480);
        const bitmap = canvas.transferToImageBitmap();
        worker.postMessage(
          {
            type: "frame",
            bitmap,
            timestamp: performance.now(),
            region: { x1: 0.15, y1: 0.05, x2: 0.85, y2: 0.98 },
          },
          [bitmap],
        );
      } else if (event.data.type === "poses" || event.data.type === "error") {
        clearTimeout(timeout);
        worker.terminate();
        setCheckingPose(false);
        if (event.data.type === "error")
          setError("Pose model check failed: " + event.data.error);
        else
          setNotice(
            "Pose model loaded and processed a blank test frame. Your camera was not opened.",
          );
      }
    };
    worker.postMessage({ type: "init" });
  };

  if (hash.startsWith("#contact/"))
    return <ContactView parts={hash.slice(1).split("/")} />;
  if (!logged || !config || !profile)
    return (
      <main className="login-page">
        <div className="login-art">
          <ShieldCheck size={58} />
          <span className="eyebrow">SILENT GUARDIAN</span>
          <h1>
            Here for the
            <br />
            moments that matter.
          </h1>
          <p>
            Quiet observation.
            <br />
            Thoughtful check-ins.
            <br />
            People who care.
          </p>
          <div className="login-orbit">
            <HeartHandshake size={90} />
          </div>
          <small>Care and Personal · One connected circle</small>
        </div>
        <section className="login-form">
          <span className="brand-mark">
            <ShieldCheck />
          </span>
          <h2>A little peace of mind.</h2>
          <p>Open your monitoring station.</p>
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
            <button className="primary">
              Open station <ArrowRight size={17} />
            </button>
          </form>
          {config?.demo_enabled && (
            <button className="secondary" onClick={demo}>
              Explore the local demo
            </button>
          )}
          <p className="muted">
            Synthetic profiles. Skeleton-only processing.
            <br />A demonstration of human support, not clinical care.
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
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#">
          <span className="brand-mark">
            <ShieldCheck />
          </span>
          <strong>
            silent
            <br />
            guardian<span>CARE, CONNECTED.</span>
          </strong>
        </a>
        <div className="mode-switch">
          <button
            className={mode === "care" ? "selected" : ""}
            onClick={() => switchMode("care")}
          >
            <Users size={15} /> Care
          </button>
          <button
            className={mode === "personal" ? "selected" : ""}
            onClick={() => switchMode("personal")}
          >
            <HeartHandshake size={15} /> Personal
          </button>
        </div>
        <span className="nav-label">YOUR WORKSPACE</span>
        <nav>
          {[
            {
              id: "overview",
              icon: LayoutDashboard,
              label: mode === "care" ? "Care station" : "My home",
            },
            { id: "events", icon: Activity, label: "Event history" },
            {
              id: "circle",
              icon: Users,
              label: mode === "care" ? "Care team" : "Trusted circle",
            },
            { id: "setup", icon: Settings2, label: "Demo setup" },
            { id: "trends", icon: Activity, label: "Trends" },
            { id: "privacy", icon: LockKeyhole, label: "Privacy" },
            { id: "evidence", icon: ShieldCheck, label: "Evidence" },
          ].map((n) => (
            <button
              key={n.id}
              className={page === n.id ? "active" : ""}
              onClick={() => setPage(n.id)}
            >
              <n.icon size={18} />
              {n.label}
              {n.id === "events" && active.length > 0 && (
                <span className="nav-count">{active.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="privacy-note">
            <Leaf size={21} />
            <h4>Privacy comes first.</h4>
            <p>
              Video stays on the device.
              <br />
              Skeleton keyframes may go to Gemini with consent.
            </p>
          </div>
          <button
            className="text-button"
            onClick={() => {
              void api("/session", "DELETE").then(() => setLogged(false));
            }}
          >
            <LogOut size={15} /> Sign out
          </button>
          <span className="demo-label">HACKATHON DEMONSTRATION</span>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            {mode === "care" ? "Care workspace" : "Personal workspace"}
            <ChevronRight size={14} />
            <strong>
              {page === "overview"
                ? mode === "care"
                  ? "Care station"
                  : "My home"
                : pretty(page)}
            </strong>
          </div>
          <div className="topbar-right">
            <span className="pill green">
              <span className="dot" /> Local demo
            </span>
            <button
              className="icon-button"
              aria-label="View events"
              onClick={() => setPage("events")}
            >
              <Bell size={19} />
            </button>
            <div className="operator-avatar">SG</div>
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
                    ? "A little closer to everyone."
                    : "Your day. A little more supported."
                  : page === "events"
                    ? "Every moment, in context."
                    : page === "circle"
                      ? "People you can count on."
                      : page === "trends"
                        ? "A baseline, and a bigger picture."
                        : page === "privacy"
                          ? "You stay in control."
                          : page === "evidence"
                            ? "Every claim needs evidence."
                            : "Ready for the demonstration."}
              </h1>
              <p>
                {mode === "care"
                  ? "Know when a moment needs your attention. Keep care personal."
                  : "Go about your day, with your trusted circle within reach."}
              </p>
            </div>
            <button className="secondary" onClick={() => setEditing(true)}>
              <Settings2 size={16} /> Edit profile
            </button>
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
            <ShieldCheck size={16} />
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
                : "Rule-based verdicts"}
            </span>
            <span>DEMO</span>
          </div>
          {page === "overview" && (
            <>
              <div className="summary-grid">
                <div className="summary-card">
                  <span>
                    {mode === "care" ? "In your care" : "Your monitoring"}
                  </span>
                  <strong>
                    {mode === "care"
                      ? profiles.filter((p) => p.mode === "care").length
                      : stream?.online
                        ? "Active"
                        : "Paused"}
                    <small>
                      {mode === "care" ? "resident profile" : "camera status"}
                    </small>
                  </strong>
                  <Users size={22} />
                </div>
                <div className="summary-card">
                  <span>Needs a check</span>
                  <strong>
                    {active.length}
                    <small>open event{active.length !== 1 ? "s" : ""}</small>
                  </strong>
                  <Bell size={22} />
                </div>
                <div className="summary-card">
                  <span>People ready to help</span>
                  <strong>
                    {profile.contacts?.length || 0}
                    <small>
                      in the {mode === "care" ? "care team" : "trusted circle"}
                    </small>
                  </strong>
                  <HeartHandshake size={22} />
                </div>
              </div>
              {checkIn && (
                <div className="check-in">
                  <div>
                    <span className="eyebrow">A QUICK CHECK-IN</span>
                    <h2>Are you feeling okay, {profile.alias}?</h2>
                    <p>
                      We noticed a brief change in movement. Your circle will be
                      notified if you don’t respond.
                    </p>
                  </div>
                  <button
                    className="primary"
                    onClick={() => acknowledge(checkIn.id)}
                  >
                    <Check size={18} /> I’m okay
                  </button>
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
                        <span className="eyebrow">A SHARED PICTURE</span>
                        <h2>Recent moments</h2>
                      </div>
                      <button
                        className="text-button"
                        onClick={() => setPage("events")}
                      >
                        View all <ArrowRight size={15} />
                      </button>
                    </div>
                    <EventList
                      events={current.slice(0, 4)}
                      onSelect={setDetail}
                    />
                  </section>
                </div>
                <aside className="right-column">
                  <section className="panel person-panel">
                    <div className="panel-heading">
                      <span className="eyebrow">
                        {mode === "care"
                          ? "MONITORED RESIDENT"
                          : "YOUR PROFILE"}
                      </span>
                      <button
                        className="icon-button"
                        onClick={() => setEditing(true)}
                        aria-label="Edit selected profile"
                      >
                        <Settings2 size={16} />
                      </button>
                    </div>
                    <div className="person-avatar">
                      {profile.alias[0]}
                      <span>
                        <Leaf size={14} />
                      </span>
                    </div>
                    <h2>{profile.alias}</h2>
                    <p>
                      {profile.age} years ·{" "}
                      {mode === "care" ? "Care mode" : "Personal mode"}
                    </p>
                    <select
                      aria-label="Monitored profile"
                      value={profile.id}
                      onChange={(e) => setPersonId(e.target.value)}
                    >
                      {profiles
                        .filter((p) => p.mode === mode)
                        .map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.alias}
                          </option>
                        ))}
                    </select>
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
                      <div className="rule-tags">
                        {profile.conditions?.map((c) => (
                          <span className="tag" key={c}>
                            {c}
                          </span>
                        ))}
                      </div>
                      <p>
                        <strong>
                          {profile.mobility_aid === "none"
                            ? "Independent movement"
                            : `${pretty(profile.mobility_aid || "")} noted in profile`}
                        </strong>
                        <span>
                          {profile.restrict_night_standing
                            ? "Assistance required during quiet hours"
                            : "No standing restrictions"}
                        </span>
                      </p>
                      <p>
                        <strong>
                          {profile.check_in_first
                            ? "Check in with me first"
                            : "Notify the care team"}
                        </strong>
                        <span>
                          {profile.check_in_first
                            ? `Level 1 response window: ${profile.check_in_seconds}s`
                            : "Direct routing for concern events"}
                        </span>
                      </p>
                    </div>
                  </section>
                  <section className="support-card">
                    <HeartHandshake size={25} />
                    <h3>Support is a shared effort.</h3>
                    <p>
                      {profile.contacts?.[0]?.name || "Add a contact"} is first
                      in the {mode === "care" ? "care team" : "trusted circle"}.
                    </p>
                    <button
                      className="text-button"
                      onClick={() => setPage("circle")}
                    >
                      Meet the circle <ArrowRight size={15} />
                    </button>
                  </section>
                  <div className="small-note">
                    <CircleHelp size={16} />
                    <span>
                      Low visibility is shown as uncertain. It never means
                      everything is okay.
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
                <span className="pill">{events.length} events</span>
              </div>
              <EventList events={events} onSelect={setDetail} />
            </section>
          )}
          {page === "circle" && (
            <section className="panel circle-panel">
              <div className="panel-heading">
                <h2>
                  {profile.alias}’s{" "}
                  {mode === "care" ? "care team" : "trusted circle"}
                </h2>
                <button className="secondary" onClick={subscribe}>
                  <Bell size={16} /> Subscribe this browser
                </button>
              </div>
              <p className="muted">
                The first push contact is assigned to this browser when you
                subscribe. Level 3 alerts reach all eligible contacts
                immediately.
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
                  <span className="pill">Level {c.min_urgency}+</span>
                  <span className="muted">
                    {c.delay_seconds
                      ? `After ${c.delay_seconds}s`
                      : "Immediately"}
                  </span>
                </div>
              ))}
              <button className="text-button" onClick={() => setEditing(true)}>
                Edit contacts and escalation <ArrowRight size={15} />
              </button>
            </section>
          )}
          {page === "setup" && (
            <div className="setup-grid">
              <section className="panel">
                <span className="eyebrow">DEMO PHONE</span>
                <h2>A ring is all it takes.</h2>
                <p>
                  Request a short call to the configured personal phone. If
                  answered, it hangs up without speaking.
                </p>
                <div className="setup-status">
                  <Phone />
                  <div>
                    <strong>
                      {config.calls_configured
                        ? "Voice configured"
                        : "Voice credentials pending"}
                    </strong>
                    <p>
                      {config.notification_mode === "dry_run"
                        ? "Simulation mode — no real call will be made."
                        : "Live mode — requests are limited and allowlisted."}
                    </p>
                  </div>
                </div>
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
                <p className="muted">
                  One call per event. Five calls per day and a five-minute
                  cooldown by default. SMS is outside the current scope.
                </p>
              </section>
              <section className="panel">
                <span className="eyebrow">CONNECTIONS</span>
                <h2>Built to connect.</h2>
                <button
                  className="secondary small"
                  onClick={checkPose}
                  disabled={checkingPose}
                >
                  {checkingPose ? "Checking pose model…" : "Check pose model"}
                </button>
                <dl className="metrics-list">
                  <div>
                    <dt>Persistence</dt>
                    <dd>{config.database}</dd>
                  </div>
                  <div>
                    <dt>Gemini</dt>
                    <dd>
                      {config.gemini_configured
                        ? "Configured"
                        : "Pending credentials"}
                    </dd>
                  </div>
                  <div>
                    <dt>Browser push</dt>
                    <dd>
                      {config.push_public_key ? "Configured" : "Pending keys"}
                    </dd>
                  </div>
                  <div>
                    <dt>Tiger Data budget</dt>
                    <dd>
                      ${config.tiger_budget_limit.toLocaleString()} maximum
                    </dd>
                  </div>
                </dl>
                <p className="muted">
                  Cloud deployment is pending credentials. The budget shown is a
                  planning constraint, not a provider billing cap.
                </p>
                <div className="privacy-callout">
                  <LockKeyhole size={18} />
                  <span>
                    The server receives skeletons; raw video stays on the
                    capture device. Seeded example profiles are synthetic.
                  </span>
                </div>
              </section>
            </div>
          )}
          {page === "trends" && (
            <TrendsPanel profile={profile} onError={setError} />
          )}
          {page === "privacy" && (
            <PrivacyPanel
              profile={profile}
              onError={setError}
              onChange={() => void refresh()}
            />
          )}
          {page === "evidence" && <EvidencePanel onError={setError} />}
          <footer className="workspace-footer">
            <span>
              <ShieldCheck size={14} /> Silent Guardian
            </span>
            <span>Staged, not clinical · Not a medical device</span>
            <span>Made for people.</span>
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
}: {
  events: GuardianEvent[];
  onSelect: (id: string) => void;
}) {
  if (!events.length)
    return (
      <div className="empty-events">
        <span>
          <Check size={23} />
        </span>
        <h3>A quiet moment.</h3>
        <p>No events yet. Start capture or play a labelled demo scenario.</p>
      </div>
    );
  return (
    <div className="event-list">
      {events.map((e) => (
        <button className="event-row" key={e.id} onClick={() => onSelect(e.id)}>
          <span className={`event-symbol urgency-${e.urgency}`}>
            {e.status === "acknowledged" ? (
              <Check size={18} />
            ) : (
              <Activity size={18} />
            )}
          </span>
          <div>
            <strong>
              {e.alias} ·{" "}
              {e.rule_hits.length ? pretty(e.rule_hits[0]) : "Demo phone call"}
            </strong>
            <p>{e.summary}</p>
            <small>
              {timeLabel(e.opened_at)} · {pretty(e.source)} · {pretty(e.status)}
            </small>
          </div>
          <span className={`pill urgency-${e.urgency}`}>
            {urgencyNames[e.urgency]}
          </span>
          <ChevronRight size={16} />
        </button>
      ))}
    </div>
  );
}
