import { useEffect, useState } from "react";
import { api, type Profile } from "./api";
import { pretty } from "./EventDetail";

type Props = {
  profile: Profile;
  onError: (s: string) => void;
  onChange?: () => void;
};
type Stat = { median: number; mad: number; scale: number; count: number };
type Baseline = {
  calibration_windows: number;
  anchor: Record<string, Record<string, Stat>>;
  current: Record<string, Record<string, Stat>>;
  threshold: number;
  trigger_enabled: boolean;
};

export function BaselinePanel({ profile, onError }: Props) {
  const [data, setData] = useState<Baseline | null>(null),
    [reviewed, setReviewed] = useState(false),
    [message, setMessage] = useState("");
  const refresh = () =>
    api<Baseline>(`/baseline/${profile.id}`)
      .then(setData)
      .catch((e) => onError(String(e)));
  useEffect(() => {
    void refresh();
  }, [profile.id]);
  const fit = async () => {
    try {
      await api(`/baseline/${profile.id}/fit`, "POST", {
        reviewed_normal: reviewed,
      });
      setMessage(
        "Personal baseline saved. The first calibration stays frozen as the anchor.",
      );
      await refresh();
    } catch (e) {
      onError(String(e));
    }
  };
  const update = async () => {
    try {
      const r = await api<{ updated: boolean; reason: string }>(
        `/baseline/${profile.id}/refresh`,
        "POST",
      );
      setMessage(
        r.updated
          ? "Current statistics updated; frozen anchor unchanged. Re-evaluate before enabling triggers."
          : "Not enough eligible observations yet.",
      );
      await refresh();
    } catch (e) {
      onError(String(e));
    }
  };
  return (
    <section className="baseline-panel">
      <h3>A baseline you can explain</h3>
      <p>
        Six torso-normalized movement features, compared within standing,
        sitting and lying postures. Modified z-score = 0.6745 × (value − median)
        / MAD, with small resolution floors for zero MAD.
      </p>
      <p className="muted">
        Calibration: six walking passes, eight sit-to-stands, five bends, one
        minute standing, one minute on a bed, then turns and reaches. Include
        ordinary fast sitting. Use two sessions and review the recording as
        normal.
      </p>
      <p>
        <strong>{data?.calibration_windows ?? 0}</strong> calibration windows ·{" "}
        <strong>k = {data?.threshold ?? 3.5}</strong> ·{" "}
        {data?.trigger_enabled
          ? "Validated persistent triggers enabled"
          : "Score only — held-out evaluation required"}
      </p>
      <label className="check-row">
        <input
          type="checkbox"
          checked={reviewed}
          onChange={(e) => setReviewed(e.target.checked)}
        />
        I reviewed the calibration sessions as normal movement
      </label>
      <div className="action-row">
        <button className="secondary small" disabled={!reviewed} onClick={fit}>
          Fit personal baseline
        </button>
        <button className="secondary small" onClick={update}>
          Update current from eligible windows
        </button>
        <button className="text-button" onClick={refresh}>
          Refresh counts
        </button>
      </div>
      {message && <p role="status">{message}</p>}
      {Object.entries(data?.anchor || {}).map(([posture, stats]) => (
        <details key={posture}>
          <summary>{pretty(posture)} · frozen anchor</summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Feature</th>
                  <th>Anchor median</th>
                  <th>Current median</th>
                  <th>MAD</th>
                  <th>Independent windows</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(stats).map(([name, s]) => (
                  <tr key={name}>
                    <td>{pretty(name)}</td>
                    <td>{s.median.toFixed(3)}</td>
                    <td>
                      {data?.current[posture]?.[name]?.median.toFixed(3) ?? "—"}
                    </td>
                    <td>{s.mad.toFixed(3)}</td>
                    <td>{s.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ))}
    </section>
  );
}

type PrivacyData = {
  counts: Record<string, number>;
  consent: boolean;
  gemini_consent: boolean;
};
export function PrivacyPanel({ profile, onError, onChange }: Props) {
  const [data, setData] = useState<PrivacyData | null>(null),
    [alias, setAlias] = useState(""),
    [receipt, setReceipt] = useState<{
      deleted: Record<string, number>;
      remaining: Record<string, number>;
      aggregates: string;
      retained: string;
    } | null>(null),
    [busy, setBusy] = useState(false);
  const refresh = () =>
    api<PrivacyData>(`/privacy/${profile.id}`)
      .then(setData)
      .catch((e) => onError(String(e)));
  useEffect(() => {
    void refresh();
  }, [profile.id, profile.version]);
  useEffect(() => {
    setAlias("");
    setReceipt(null);
  }, [profile.id]);
  const pause = async () => {
    try {
      await api(`/privacy/${profile.id}/consent`, "POST", {
        accepted: false,
        gemini: false,
        local_preview: false,
      });
      onChange?.();
      await refresh();
    } catch (e) {
      onError(String(e));
    }
  };
  const purge = async () => {
    setBusy(true);
    try {
      setReceipt(
        await api(`/privacy/${profile.id}/purge`, "POST", {
          confirm_alias: alias,
        }),
      );
      setAlias("");
      onChange?.();
      await refresh();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel privacy-panel">
      <span className="eyebrow">YOUR CONTROL</span>
      <h2>Pause. Review. Delete.</h2>
      <p>
        Video stays on the capture device. Skeleton landmarks are identifying
        movement data and are stored by this server. Category flags and
        skeleton-rendered keyframes may go to Gemini only with your explicit
        consent and a confirmed billed project. This demonstration is not a
        medical device.
      </p>
      <div className="action-row">
        <span className="pill">
          Monitoring {data?.consent ? "consented" : "paused"}
        </span>
        <span className="pill">
          Gemini {data?.gemini_consent ? "consented" : "not consented"}
        </span>
        <button className="secondary" onClick={pause}>
          Pause monitoring and Gemini
        </button>
      </div>
      <h3>Stored for {profile.alias}</h3>
      <dl className="metrics-list">
        {Object.entries(data?.counts || {}).map(([key, value]) => (
          <div key={key}>
            <dt>{pretty(key)}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <div className="delete-panel">
        <h3>Delete this profile’s stored data</h3>
        <p>
          This permanently removes pose windows, keyframes, events, notification
          history, subscriptions, baseline statistics and profile versions.
          Monitoring stops. Only an empty identifier with consent revoked
          remains, so a restart cannot restore the profile.
        </p>
        <p className="muted">
          External provider logs, downloaded files and database backups are
          outside this deletion. Tiger aggregates are refreshed when available;
          the result below reports any incomplete refresh.
        </p>
        <label>
          Type “{profile.alias}” to confirm
          <input
            aria-label="Confirm deletion alias"
            value={alias}
            onChange={(e) => setAlias(e.target.value)}
          />
        </label>
        <button
          className="danger-button"
          onClick={purge}
          disabled={busy || alias !== profile.alias}
        >
          {busy ? "Deleting…" : "Permanently delete stored data"}
        </button>
      </div>
      {receipt && (
        <div role="status" className="deletion-receipt">
          <h3>Deletion result</h3>
          <p>
            Remaining windows: {receipt.remaining.windows} · events:{" "}
            {receipt.remaining.events} · keyframes:{" "}
            {receipt.remaining.keyframes}
          </p>
          <p>Aggregates: {receipt.aggregates}</p>
          <p>{receipt.retained}</p>
        </div>
      )}
    </section>
  );
}

type Point = {
  day: string;
  posture: string;
  feature: string;
  mean: number;
  stddev: number;
  anchor: number | null;
  gap: number | null;
  synthetic: boolean;
  windows: number;
};
export function TrendsPanel({ profile, onError }: Props) {
  const [points, setPoints] = useState<Point[]>([]),
    [synthetic, setSynthetic] = useState(false),
    [feature, setFeature] = useState("sway");
  const refresh = () =>
    api<{ points: Point[] }>(`/trends/${profile.id}`)
      .then((r) => setPoints(r.points))
      .catch((e) => onError(String(e)));
  useEffect(() => {
    void refresh();
  }, [profile.id]);
  const load = async () => {
    try {
      await api(`/demo/trends/${profile.id}`, "POST");
      setSynthetic(true);
      await refresh();
    } catch (e) {
      onError(String(e));
    }
  };
  const shown = points.filter(
    (p) =>
      p.synthetic === synthetic &&
      p.feature === feature &&
      p.posture === "standing",
  );
  const max =
    Math.max(0.1, ...shown.flatMap((p) => [p.mean, p.anchor || 0])) * 1.2;
  const x = (i: number) => 40 + (i * 520) / Math.max(shown.length - 1, 1),
    y = (v: number) => 190 - (v / max) * 160;
  return (
    <>
      <section className="panel">
        <span className="eyebrow">FROZEN ANCHOR, CURRENT MOVEMENT</span>
        <h2>Change stays visible.</h2>
        <p>
          Daily means and standard deviations beside the original calibration
          median. A changing current baseline never rewrites the frozen anchor.
          A gap is an observation, not a diagnosis.
        </p>
        <div className="action-row">
          <select
            aria-label="Trend feature"
            value={feature}
            onChange={(e) => setFeature(e.target.value)}
          >
            <option value="sway">Lateral sway / torso length</option>
            <option value="hip_speed">
              Hip speed / torso length per second
            </option>
          </select>
          <select
            aria-label="Trend data source"
            value={String(synthetic)}
            onChange={(e) => setSynthetic(e.target.value === "true")}
          >
            <option value="false">Observed eligible windows</option>
            <option value="true">Synthetic six-day example</option>
          </select>
          <button className="secondary small" onClick={load}>
            Load synthetic drift example
          </button>
        </div>
        <p className="pill">
          {synthetic
            ? "SYNTHETIC DATA — not measured decline"
            : "Observed data — standing posture"}
        </p>
        {shown.length ? (
          <>
            <svg
              className="trend-chart"
              viewBox="0 0 600 240"
              role="img"
              aria-label={`${synthetic ? "Synthetic" : "Observed"} daily ${feature} with frozen anchor`}
            >
              <line x1="40" y1="190" x2="560" y2="190" stroke="#9aa8a0" />
              <polyline
                points={shown.map((p, i) => `${x(i)},${y(p.mean)}`).join(" ")}
                fill="none"
                stroke="#20685b"
                strokeWidth="3"
              />
              {shown[0].anchor !== null && (
                <line
                  x1="40"
                  x2="560"
                  y1={y(shown[0].anchor)}
                  y2={y(shown[0].anchor)}
                  stroke="#bb853e"
                  strokeDasharray="6 4"
                />
              )}
              {shown.map((p, i) => (
                <g key={p.day}>
                  <circle cx={x(i)} cy={y(p.mean)} r="4" fill="#20685b" />
                  <text x={x(i)} y="214" textAnchor="middle" fontSize="10">
                    {p.day.slice(5)}
                  </text>
                  <text
                    x={x(i)}
                    y={y(p.mean) - 12}
                    textAnchor="middle"
                    fontSize="11"
                  >
                    {p.mean.toFixed(3)}
                  </text>
                </g>
              ))}
            </svg>
            <p className="muted">
              Green: daily mean. Dashed amber: frozen anchor median
              {synthetic ? " (synthetic reference)" : ""}.
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Day</th>
                    <th>Mean ± SD</th>
                    <th>Gap from anchor</th>
                    <th>Windows</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((p) => (
                    <tr key={p.day}>
                      <td>{p.day}</td>
                      <td>
                        {p.mean.toFixed(3)} ± {p.stddev.toFixed(3)}
                      </td>
                      <td>{p.gap?.toFixed(3) ?? "Uncalibrated"}</td>
                      <td>{p.windows}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="empty-copy">
            No eligible observations yet. Calibrate with reviewed normal
            sessions, or load the clearly labelled synthetic example.
          </p>
        )}
      </section>
      <section className="panel">
        <BaselinePanel profile={profile} onError={onError} />
      </section>
    </>
  );
}

type EvidenceData = {
  scored_windows: number;
  gemini_requests: number;
  requests_per_100_windows: number;
  dismissals_logged: number;
  report: null | {
    scope: string;
    normal_observation_seconds: number;
    normal_false_alerts: number;
    results: {
      scenario: string;
      profile: string;
      event_count: number;
      ingest_p95_ms: number;
      fall_onset_to_queued_alert_s: number | null;
    }[];
  };
};
export function EvidencePanel({ onError }: { onError: (s: string) => void }) {
  const [data, setData] = useState<EvidenceData | null>(null);
  useEffect(() => {
    void api<EvidenceData>("/evidence")
      .then(setData)
      .catch((e) => onError(String(e)));
  }, []);
  return (
    <section className="panel">
      <span className="eyebrow">MEASURED, WITH SCOPE</span>
      <h2>Show what passed.</h2>
      <p>
        These are generated skeleton fixtures through the actual detector, local
        persistence and dry-run alert queue. They are not recorded people,
        camera latency or clinical validation.
      </p>
      <div className="summary-grid">
        <div className="summary-card">
          <span>Gemini requests / scored windows</span>
          <strong>
            {data?.gemini_requests ?? 0} / {data?.scored_windows ?? 0}
          </strong>
        </div>
        <div className="summary-card">
          <span>Requests per 100 windows</span>
          <strong>{data?.requests_per_100_windows ?? 0}%</strong>
        </div>
        <div className="summary-card">
          <span>Human dismissals logged</span>
          <strong>{data?.dismissals_logged ?? 0}</strong>
        </div>
      </div>
      <p className="muted">
        The request ratio is a cost/exposure proxy, never an accuracy score.
        Synthetic trend rows and calibration are excluded.
      </p>
      {data?.report && (
        <>
          <p>
            <strong>
              {data.report.normal_false_alerts} false alerts in{" "}
              {data.report.normal_observation_seconds} seconds
            </strong>{" "}
            of synthetic normal movement. Too little exposure to infer an hourly
            false-alert rate.
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Synthetic scenario</th>
                  <th>Profile</th>
                  <th>Events</th>
                  <th>Ingest p95</th>
                  <th>Fall onset → queue</th>
                </tr>
              </thead>
              <tbody>
                {data.report.results.map((r, i) => (
                  <tr key={i}>
                    <td>{pretty(r.scenario)}</td>
                    <td>{r.profile}</td>
                    <td>{r.event_count}</td>
                    <td>{r.ingest_p95_ms} ms</td>
                    <td>
                      {r.fall_onset_to_queued_alert_s === null
                        ? "—"
                        : `${r.fall_onset_to_queued_alert_s}s`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <h3>Evidence still required</h3>
      <p>
        Two consenting people, two camera angles, independent tuning and test
        clips, 20–30 minutes of ordinary movement, an uncertain/occluded case,
        and real provider receipt. Tiger load latency, Vultr operation and prize
        eligibility remain unverified. No clinical accuracy, regional residency,
        SBAR handoff, phone-sensor integration or fork replay is claimed.
      </p>
    </section>
  );
}
