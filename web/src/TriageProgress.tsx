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
  geminiBlocker,
  mlNote,
}: {
  event?: GuardianEvent | null;
  candidateDecision?: string | null;
  prediction?: Record<string, unknown> | null;
  ruleHits?: string[];
  hasIncident?: boolean;
  quality?: string;
  scoredWindows?: number;
  geminiBlocker?: string;
  mlNote?: string;
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
    if (mlNote) {
      // Without the model only the fall and stillness safety rules can raise alerts.
      ml = { label: "ML", detail: mlNote, state: "skipped" };
    } else if (candidateDecision === "fallback_open" || event?.verdict_status === "model_fallback") {
      // Stillness on the floor: the safety rule raised it (Gemini still reviews it).
      ml = { label: "ML", detail: "Safety rule", state: "done" };
    } else if (candidateDecision === "supported" || ["model_supported_no_gemini", "pending", "validated", "fallback", "privacy_blocked", "not_reviewed"]
      .includes(event?.verdict_status ?? "")) {
      ml = { label: "ML", detail: "Supported", state: "done" };
    } else if (candidateDecision === "rejected") {
      ml = { label: "ML", detail: "Not supported", state: "done" };
    } else if (candidateDecision === "hold") {
      ml = { label: "ML", detail: "Held for review", state: "done" };
    } else if (action?.status === "scored" && action?.mode === "shadow") {
      ml = { label: "ML", detail: "Shadow only", state: "skipped" };
    } else if (action?.status === "pending") {
      ml = { label: "ML", detail: "Shadow running", state: "skipped" };
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
    gemini = { label: "Gemini", detail: "Review failed, reason in log", state: "done" };
  } else if (event?.verdict_status === "privacy_blocked") {
    gemini = { label: "Gemini", detail: "Consent blocked", state: "skipped" };
  } else if (event?.verdict_status === "not_reviewed") {
    gemini = { label: "Gemini", detail: "One review per video: a more urgent alert", state: "skipped" };
  } else if (["model_supported_no_gemini", "model_fallback"].includes(event?.verdict_status ?? "")) {
    gemini = { label: "Gemini", detail: geminiBlocker ?? "Not requested", state: "skipped" };
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
