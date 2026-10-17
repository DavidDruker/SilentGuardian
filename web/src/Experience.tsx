import { useEffect, useState } from "react";
import { api, type Profile } from "./api";
import { pretty } from "./EventDetail";

type Props = {
  profile: Profile;
  onError: (s: string) => void;
  onChange?: () => void;
};

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
      <span className="eyebrow">CONSENT AND STORAGE</span>
      <h2>Change consent or delete data.</h2>
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
      <details className="delete-panel">
        <summary>Delete this profile’s stored data</summary>
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
      </details>
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
