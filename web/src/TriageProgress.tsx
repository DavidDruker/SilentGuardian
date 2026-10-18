import type { GuardianEvent } from "./api";

type StageState = "done" | "active" | "skipped";
type Stage = { label: string; detail: string; state: StageState };

export function TriageProgress({
  event,
  candidateDecision,
  prediction,
  ruleHits = [],
  hasIncident = false,
  quality,
  scoredWindows = 0,
}: {
  event?: GuardianEvent | null;
  candidateDecision?: string | null;
  prediction?: Record<string, unknown> | null;
  ruleHits?: string[];
  hasIncident?: boolean;
  quality?: string;
  scoredWindows?: number;
}) {
  const action = prediction ?? event?.action_prediction;
  const hasRuleHit = Boolean(event || candidateDecision || ruleHits.length || hasIncident);
  const rule: Stage = hasRuleHit
    ? { label: "Rules", detail: "Flagged movement", state: "done" }
    : quality === "good" || scoredWindows > 0
      ? { label: "Rules", detail: "Checked", state: "done" }
      : { label: "Rules", detail: "Waiting for clear view", state: "active" };

  let ml: Stage = { label: "ML", detail: "Not used", state: "skipped" };
  if (hasRuleHit) {
    if (candidateDecision === "supported" || event?.verdict_status === "model_supported_no_gemini") {
      ml = { label: "ML", detail: "Supported", state: "done" };
    } else if (candidateDecision === "rejected") {
      ml = { label: "ML", detail: "Not supported", state: "done" };
    } else if (candidateDecision === "hold") {
      ml = { label: "ML", detail: "Held for review", state: "done" };
    } else if (candidateDecision === "fallback_open" || event?.verdict_status === "model_fallback") {
      ml = { label: "ML", detail: "Unavailable", state: "done" };
    } else if (action?.status === "scored") {
      ml = { label: "ML", detail: "Shadow scored", state: "done" };
    } else if (action?.status === "pending") {
      ml = { label: "ML", detail: "Shadow running", state: "active" };
    } else if (candidateDecision === "shadow" || action?.mode === "shadow") {
      ml = { label: "ML", detail: "Shadow only", state: "skipped" };
    }
  }

  let gemini: Stage = { label: "Gemini", detail: "Not requested", state: "skipped" };
  if (event?.verdict_status === "pending") {
    gemini = { label: "Gemini", detail: "Reviewing", state: "active" };
  } else if (event?.verdict_status === "validated") {
    gemini = { label: "Gemini", detail: "Reviewed", state: "done" };
  } else if (event?.verdict_status === "fallback") {
    gemini = { label: "Gemini", detail: "Review unavailable", state: "done" };
  } else if (event?.verdict_status === "privacy_blocked") {
    gemini = { label: "Gemini", detail: "Consent blocked", state: "skipped" };
  }

  const stages = [rule, ml, gemini];
  const reached = [...stages].reverse().find((stage) => stage.state !== "skipped")?.label ?? "Rules";
  return <section className="triage-progress" aria-label="Triage progress">
    <div className="triage-heading"><strong>Triage progress</strong><span>Reached: {reached}</span></div>
    <ol className="triage-steps">
      {stages.map((stage, index) => <li key={stage.label} className={`triage-step ${stage.state}`}>
        <span className="triage-step-marker" aria-hidden="true">{index + 1}</span>
        <div><strong>{stage.label}</strong><span>{stage.detail}</span></div>
      </li>)}
    </ol>
  </section>;
}
