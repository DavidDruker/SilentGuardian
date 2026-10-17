import { useEffect, useState } from "react";
import { api } from "./api";

type Prediction = {
  status: string;
  observed_at?: number;
  latency_ms?: number;
  fall_score?: number;
  staggering_score?: number;
  top_actions?: { class_id: number; label: string; score: number }[];
  rule_hits?: string[];
  source?: string;
  age_seconds?: number;
};
type ModelStatus = {
  state: string;
  mode: string;
  error: string | null;
  latest?: Prediction | null;
  streamId?: string | null;
};

export function ActionPanel({
  prediction,
  live = false,
  streamId = null,
}: {
  prediction?: Record<string, unknown> | null;
  live?: boolean;
  streamId?: string | null;
}) {
  const [model, setModel] = useState<ModelStatus | null>(null);
  useEffect(() => {
    if (!live) return;
    let active = true;
    const refresh = () =>
      api<ModelStatus>(`/action-model${streamId ? `?stream_id=${encodeURIComponent(streamId)}` : ""}`)
        .then((value) => {
          if (active) setModel({ ...value, streamId });
        })
        .catch(() => {
          if (active)
            setModel({ state: "unreachable", mode: "shadow", error: null });
        });
    void refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [live, streamId]);
  const direct = prediction as Prediction | null | undefined;
  const polled = model?.streamId === streamId && streamId ? model.latest : null;
  const canUsePolled = direct?.status !== "uncertain" && direct?.status !== "warming_up";
  const result = canUsePolled && polled && (polled.observed_at ?? 0) > (direct?.observed_at ?? 0) ? polled : direct;
  const status =
    result?.status === "scored"
      ? (live && (result.age_seconds ?? 0) > 2 ? `Last model score · ${result.age_seconds?.toFixed(1)}s behind the latest pose` : "Model scores available")
      : result?.status === "inference_failed"
        ? "Inference failed; existing detection continues"
        : model?.state === "ready"
          ? result?.status === "uncertain"
            ? "Tracking uncertain"
            : result?.status === "pending" ? "Processing the latest pose sequence" : "Waiting for a continuous pose sequence"
          : model?.state === "unavailable"
            ? "Model setup required"
            : model?.state === "disabled"
              ? "Model disabled"
              : model?.state === "unreachable"
                ? "Model status unavailable"
                : live
                  ? "Loading model status"
                  : "No model score recorded";
  return (
    <section className="action-panel" aria-label="Learned action recognition">
      <div className="action-heading">
        <h3>Learned movement · ST-GCN++</h3>
        <span className="pill">Shadow evaluation</span>
      </div>
      <p>
        {status}. Model scores are recorded for comparison. They do not control
        alerts.
      </p>
      {result?.status === "scored" && (
        <>
          {!live && (
            <p>
              Snapshot with the highest falling score recorded for this event.
            </p>
          )}
          <div className="action-scores">
            <div>
              <span>Falling score</span>
              <strong>{((result.fall_score ?? 0) * 100).toFixed(1)}%</strong>
            </div>
            <div>
              <span>Staggering score</span>
              <strong>
                {((result.staggering_score ?? 0) * 100).toFixed(1)}%
              </strong>
            </div>
            <div>
              <span>Inference time</span>
              <strong>{result.latency_ms?.toFixed(0)} ms</strong>
            </div>
          </div>
          <ol>
            {result.top_actions?.slice(0, 3).map((action) => (
              <li key={action.class_id}>
                <span>{action.label}</span>
                <strong>{(action.score * 100).toFixed(1)}%</strong>
              </li>
            ))}
          </ol>
          <p>
            Rules on the same window:{" "}
            {result.rule_hits?.length
              ? result.rule_hits.join(", ").replaceAll("_", " ")
              : "none"}
            .
          </p>
          <small>
            {result.source === "synthetic"
              ? "Synthetic replay"
              : result.source === "replay"
                ? "Local-video pose sequence"
                : "Camera pose sequence"}
            {result.observed_at
              ? ` · observed ${new Date(result.observed_at * 1000).toLocaleTimeString()}`
              : ""}
            . Scores are model outputs, not calibrated probabilities of harm.
            The model always ranks known actions, even for unfamiliar movement.
          </small>
        </>
      )}
      {model?.state === "unavailable" && (
        <p>
          Install the optional model runtime and checkpoint using the project
          setup guide, then restart.
        </p>
      )}
    </section>
  );
}
