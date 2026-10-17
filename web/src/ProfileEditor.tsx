import { useState } from "react";
import { Save, X } from "lucide-react";
import { api, type Profile, uid } from "./api";

function blankProfile(mode: "care" | "personal"): Profile {
  return {
    id: uid(), version: 1, alias: "", age: -1, mode,
    timezone: "America/Toronto",
    conditions: [], medication_flags: [], mobility_aid: "none",
    restrict_night_standing: false, quiet_start: 22, quiet_end: 7,
    bed_zone: null, check_in_first: mode === "personal", check_in_seconds: 60,
    contacts: [], privacy: "skeleton_only", retain_days: 7,
    consent: true, care_notes: "", risk_categories: [], gemini_consent: false,
    data_category: "consenting_teammate",
  };
}

export function ProfileEditor({
  profile, mode, onClose, onSaved,
}: {
  profile?: Profile;
  mode: "care" | "personal";
  onClose: () => void;
  onSaved: (profile: Profile) => void;
}) {
  const [draft, setDraft] = useState<Profile>(() => profile ? structuredClone(profile) : blankProfile(mode));
  const [conditionsText, setConditionsText] = useState(() => profile?.conditions?.join(", ") || "");
  const [medicationText, setMedicationText] = useState(() => profile?.medication_flags?.join(", ") || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const field = <K extends keyof Profile>(name: K, value: Profile[K]) =>
    setDraft((current) => ({ ...current, [name]: value }));
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const labels = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean);
      const submitted = {
        ...draft,
        data_category: draft.data_category === "real" ? "consenting_teammate" : draft.data_category,
        conditions: labels(conditionsText),
        medication_flags: labels(medicationText),
      };
      const saved = await api<Profile>(`/profiles/${draft.id}`, "PUT", submitted);
      onSaved(saved);
      onClose();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="modal-shade">
      <section className="modal profile-modal compact-profile" role="dialog" aria-modal="true" aria-labelledby="profile-title">
        <header>
          <h2 id="profile-title">{profile ? "Edit profile" : "Create profile"}</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close profile editor"><X /></button>
        </header>
        <form onSubmit={save}>
          <div className="form-grid">
            <label>Display name
              <input required maxLength={60} autoFocus value={draft.alias} onChange={(event) => field("alias", event.target.value)} />
            </label>
            <label>Age
              <input type="number" min={0} max={120} required value={draft.age < 0 ? "" : draft.age}
                onChange={(event) => field("age", Number(event.target.value))} />
            </label>
            <label>Mobility aid
              <select value={draft.mobility_aid} onChange={(event) => field("mobility_aid", event.target.value as Profile["mobility_aid"])}>
                <option value="none">None</option><option value="walker">Walker</option>
                <option value="cane">Cane</option><option value="wheelchair">Wheelchair</option>
              </select>
            </label>
            <label className="span2">Conditions <small>Separate with commas</small>
              <input value={conditionsText} onChange={(event) => setConditionsText(event.target.value)}
                placeholder="e.g. vertigo, arthritis" />
            </label>
            <label className="span2">Medication flags <small>Separate with commas</small>
              <input value={medicationText} onChange={(event) => setMedicationText(event.target.value)}
                placeholder="e.g. medication that may affect balance" />
            </label>
            <label className="span2">Care notes
              <textarea maxLength={1000} value={draft.care_notes}
                onChange={(event) => field("care_notes", event.target.value)}
                placeholder="Relevant movement or assistance context" />
            </label>
            <fieldset className="span2 context-flags">
              <legend>Additional context</legend>
              {([
                ["balance_support", "Balance support"],
                ["mobility_support", "Mobility support"],
                ["transfer_assistance", "Transfer assistance"],
                ["medication_fall_risk", "Medication fall risk"],
              ] as const).map(([value, label]) => (
                <label className="check" key={value}>
                  <input type="checkbox" checked={draft.risk_categories?.includes(value) || false}
                    onChange={(event) => field("risk_categories", event.target.checked
                      ? [...(draft.risk_categories || []), value]
                      : (draft.risk_categories || []).filter((item) => item !== value))} />
                  {label}
                </label>
              ))}
            </fieldset>
            <label className="check span2">
              <input type="checkbox" checked={draft.restrict_night_standing}
                onChange={(event) => field("restrict_night_standing", event.target.checked)} />
              Alert on standing without a helper at night
            </label>
            {draft.restrict_night_standing && <>
              <label>From (hour)<input type="number" min={0} max={23} value={draft.quiet_start}
                onChange={(event) => field("quiet_start", Number(event.target.value))} /></label>
              <label>Until (hour)<input type="number" min={0} max={23} value={draft.quiet_end}
                onChange={(event) => field("quiet_end", Number(event.target.value))} /></label>
            </>}
            {draft.mode === "personal" && <label className="check span2">
              <input type="checkbox" checked={draft.check_in_first}
                onChange={(event) => field("check_in_first", event.target.checked)} />
              Ask me to check in first
            </label>}
          </div>
          {error && <p className="error" role="alert">{error}</p>}
          <footer>
            <button className="secondary" type="button" onClick={onClose}>Cancel</button>
            <button className="primary" disabled={busy}><Save size={16} />{busy ? "Saving…" : "Save profile"}</button>
          </footer>
        </form>
      </section>
    </div>
  );
}
