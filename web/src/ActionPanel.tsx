type Prediction = {
  status?: string;
  fall_score?: number;
  staggering_score?: number;
};

const percent = (v?: number) => `${((v ?? 0) * 100).toFixed(1)}%`;

export function ActionPanel({
  prediction,
}: {
  prediction?: Record<string, unknown> | null;
}) {
  const result = prediction as Prediction | null | undefined;
  if (result?.status !== "scored") return null;
  return (
    <section className="action-panel" aria-label="Learned model (shadow)">
      <h4>Learned model (shadow)</h4>
      <p>
        Falling {percent(result.fall_score)} · Staggering{" "}
        {percent(result.staggering_score)}
      </p>
      <p className="muted">
        Compared with the rules for review. It does not change alerts or
        levels.
      </p>
    </section>
  );
}
