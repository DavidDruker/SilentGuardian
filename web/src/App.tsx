import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronRight, Settings2, X } from "lucide-react";
import { api, type AppConfig, type GuardianEvent, type Profile } from "./api";
import { Capture } from "./Capture";
import { ProfileEditor } from "./ProfileEditor";
import { EventDetail, pretty, timeLabel, urgencyNames } from "./EventDetail";
import { ContactView } from "./ContactView";
import { Activity } from "./Activity";

type Monitoring = { person_id: string; quality: string; online: boolean };
type Mode = "care" | "personal";
const visibleProfile = (person: Profile) =>
  !(person.data_category === "synthetic" && ["margaret", "daniel"].includes(person.id));

// Per-browser convenience only: profiles live in the database, this just reopens
// the workspace and person that were selected before a reload.
const remembered = (key: string) => {
  try { return localStorage.getItem(`hayvn.${key}`) ?? ""; } catch { return ""; }
};
const remember = (key: string, value: string) => {
  try { localStorage.setItem(`hayvn.${key}`, value); } catch { /* storage unavailable */ }
};

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [events, setEvents] = useState<GuardianEvent[]>([]);
  const [monitoring, setMonitoring] = useState<Monitoring[]>([]);
  const [mode, setMode] = useState<Mode>(remembered("mode") === "care" ? "care" : "personal");
  const [personId, setPersonId] = useState(remembered("person"));
  const restored = useRef(false);
  const [logged, setLogged] = useState(false);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [page, setPage] = useState<"overview" | "events" | "activity">("overview");
  const [editing, setEditing] = useState<"new" | "existing" | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [hash, setHash] = useState(location.hash);

  useEffect(() => {
    const onHash = () => setHash(location.hash);
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const people = await api<Profile[]>("/profiles");
      setProfiles(people.filter(visibleProfile));
      setLogged(true);
      const [history, streams] = await Promise.all([
        api<GuardianEvent[]>("/events"),
        api<Monitoring[]>("/monitoring"),
      ]);
      setEvents(history);
      setMonitoring(streams);
    } catch (cause) {
      if (String(cause).includes("Sign in")) setLogged(false);
      else setError(String(cause));
    }
  }, []);

  useEffect(() => {
    void api<AppConfig>("/config").then(setConfig).catch((cause) => setError(String(cause)));
    if (!location.hash.startsWith("#contact/")) void refresh();
  }, [refresh]);
  useEffect(() => {
    if (!logged || hash.startsWith("#contact/")) return;
    // Schedule the next poll only after the previous one finishes, so slow
    // database responses cannot pile up overlapping requests.
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      timer = setTimeout(() => void refresh().finally(() => { if (!stopped) poll(); }), 2000);
    };
    poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [logged, hash, refresh]);

  useEffect(() => remember("mode", mode), [mode]);
  useEffect(() => { if (personId) remember("person", personId); }, [personId]);
  useEffect(() => {
    // On the first load only, open the workspace that holds the remembered person,
    // or the other workspace if this one is empty. Later mode clicks are respected.
    if (restored.current || profiles.length === 0) return;
    restored.current = true;
    const target = profiles.find((person) => person.id === personId)
      ?? (profiles.some((person) => person.mode === mode) ? undefined : profiles[0]);
    if (target && target.mode !== mode) setMode(target.mode as Mode);
  }, [profiles, personId, mode]);

  const inMode = profiles.filter((person) => person.mode === mode);
  const profile = inMode.find((person) => person.id === personId) ?? inMode[0];
  const profileIds = new Set(profiles.map((person) => person.id));
  const visibleEvents = events.filter((event) => profileIds.has(event.person_id) && event.source !== "demo_call");
  const current = visibleEvents.filter((event) => event.person_id === profile?.id);
  const open = current.filter((event) => event.status === "open");
  const today = current.filter((event) => new Date(event.opened_at * 1000).toDateString() === new Date().toDateString());
  const stream = monitoring.find((item) => item.person_id === profile?.id);
  const checkIn = mode === "personal" && profile?.check_in_first
    ? open.find((event) => event.urgency === 1)
    : undefined;
  const sorted = [...(page === "events" ? current : open)].sort(
    (a, b) => page === "events"
      ? b.opened_at - a.opened_at
      : b.urgency - a.urgency || b.opened_at - a.opened_at,
  );

  const login = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      await api("/session", "POST", { token });
      setToken("");
      await refresh();
    } catch (cause) {
      setError(String(cause));
    }
  };
  const acknowledge = async (eventId: string) => {
    if (!profile) return;
    try {
      await api(`/events/${eventId}/ack`, "POST", { author: profile.alias, note: "I'm okay" });
      await refresh();
    } catch (cause) {
      setError(String(cause));
    }
  };

  if (hash.startsWith("#contact/")) return <ContactView parts={hash.slice(1).split("/")} />;
  if (!logged || !config) return (
    <main className="login-page">
      <div className="login-art">
        <img className="login-logo" src="/brand/hayvn-lockup-reversed.png" alt="Hayvn" />
        <h1>Movement monitoring, at a glance.</h1>
        <p>Live capture, clear metrics, and alerts in one place.</p>
      </div>
      <section className="login-form">
        <h2>Sign in</h2>
        <form onSubmit={login}>
          <label>Operator access token
            <input type="password" autoComplete="current-password" value={token}
              onChange={(event) => setToken(event.target.value)} placeholder="Enter access token" required />
          </label>
          <button className="primary">Continue</button>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
      </section>
    </main>
  );

  const selectedEvent = visibleEvents.find((event) => event.id === detail);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" onClick={() => setPage("overview")}>
          <img className="brand-logo" src="/brand/hayvn-lockup.png" alt="Hayvn" />
        </a>
        <div className="mode-switch" aria-label="Workspace mode">
          {(["personal", "care"] as const).map((value) => (
            <button key={value} className={mode === value ? "selected" : ""}
              onClick={() => { setMode(value); setPersonId(""); setPage("overview"); }}>
              {value === "personal" ? "Personal" : "Care"}
            </button>
          ))}
        </div>
        <nav aria-label="Main navigation">
          <button className={page === "overview" ? "active" : ""} onClick={() => setPage("overview")}>Overview</button>
          <button className={page === "events" ? "active" : ""} onClick={() => setPage("events")}>Alerts
            {open.length > 0 && <span className="nav-count">{open.length}</span>}
          </button>
          <button className={page === "activity" ? "active" : ""} onClick={() => setPage("activity")}>Activity</button>
        </nav>
        <div className="sidebar-bottom">
          <button className="text-button" onClick={() => void api("/session", "DELETE").then(() => setLogged(false))}>Sign out</button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">Hayvn <ChevronRight size={14} /> <strong>{page === "overview" ? "Overview" : page === "events" ? "Alerts" : "Activity"}</strong></div>
          <span className="storage-label">Storage: {config.database}</span>
        </header>
        <main className="workspace mvp-workspace">
          <div className="page-heading">
            <div>
              <span className="eyebrow">{mode === "care" ? "CARE MONITORING" : "PERSONAL MONITORING"}</span>
              <h1>{page === "overview" ? "Overview" : page === "events" ? "Alerts" : "Activity"}</h1>
            </div>
            <div className="heading-actions">
              {inMode.length > 1 && <select aria-label="Selected person" value={profile?.id || ""}
                onChange={(event) => setPersonId(event.target.value)}>
                {inMode.map((person) => <option key={person.id} value={person.id}>{person.alias}</option>)}
              </select>}
              {profile && <button className="secondary" onClick={() => setEditing("existing")}><Settings2 size={16} /> Edit profile</button>}
              <button className="primary" onClick={() => setEditing("new")}>{profile ? "Add person" : "Create profile"}</button>
            </div>
          </div>
          {error && <div className="banner error" role="alert"><span>{error}</span>
            <button className="icon-button" onClick={() => setError("")} aria-label="Dismiss error"><X size={16} /></button>
          </div>}
          {!profile ? (
            <section className="panel setup-empty">
              <h2>No {mode === "care" ? "residents" : "personal profile"} yet</h2>
              <p>Create a profile, then start the camera or select a video.</p>
              <button className="primary" onClick={() => setEditing("new")}>Create profile</button>
            </section>
          ) : (
            <>
              {checkIn && <section className="check-in">
                <div><span className="eyebrow">CHECK-IN</span><h2>Are you okay, {profile.alias}?</h2></div>
                <button className="primary" onClick={() => void acknowledge(checkIn.id)}>I'm okay</button>
              </section>}
              {page === "overview" && <>
                <div className="overview-metrics" aria-label="Monitoring metrics">
                  <div><span>Open alerts</span><strong>{open.length}</strong></div>
                  <div><span>Today</span><strong>{today.length}</strong></div>
                  <div><span>Camera</span><strong className="metric-status">{stream?.online ? stream.quality === "uncertain" ? "Uncertain" : "Live" : "Offline"}</strong></div>
                </div>
                <Capture key={profile.id} profile={profile} config={config} onEvent={() => void refresh()} onError={setError} />
              </>}
              {page === "activity" && <Activity key={profile.id} profile={profile} database={config.database} />}
              {page !== "activity" &&
              <section className="panel recent-panel">
                <div className="panel-heading"><h2>{page === "overview" ? "Open alerts" : "Alert history"}</h2>
                  {page === "overview" && <button className="text-button" onClick={() => setPage("events")}>View all</button>}
                </div>
                {sorted.length === 0 ? <div className="empty-events"><h3>{page === "overview" ? "No open alerts" : "No alerts yet"}</h3></div> :
                  <div className="event-list">{sorted.map((event) => (
                    <button className="event-row" key={event.id} onClick={() => setDetail(event.id)}>
                      <div><strong>{event.rule_hits.length ? pretty(event.rule_hits[0]) : "Alert"}</strong>
                        <p>{event.summary}</p><small>{timeLabel(event.observed_at)} · {pretty(event.status)}</small></div>
                      <span className={event.status === "open" ? `pill urgency-${event.urgency}` : "pill"}>{urgencyNames[event.urgency]}</span>
                    </button>
                  ))}</div>}
              </section>
              }
            </>
          )}
        </main>
      </div>
      {editing && <ProfileEditor profile={editing === "existing" ? profile : undefined} mode={mode} onClose={() => setEditing(null)}
        onSaved={(saved) => {
          setProfiles((current) => [...current.filter((person) => person.id !== saved.id), saved].filter(visibleProfile));
          setMode(saved.mode as Mode);
          setPersonId(saved.id);
          void refresh();
        }} />}
      {selectedEvent && <EventDetail event={selectedEvent} profile={profile}
        onClose={() => setDetail(null)} onChange={() => void refresh()} onError={setError} />}
    </div>
  );
}
