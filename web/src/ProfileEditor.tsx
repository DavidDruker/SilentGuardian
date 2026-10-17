import { useState } from "react";
import { Plus, Save, Trash2, X } from "lucide-react";
import { api, type Profile, uid } from "./api";

export function ProfileEditor({
  profile,
  onClose,
  onSaved,
}: {
  profile: Profile;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [p, setP] = useState<Profile>(structuredClone(profile)),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [conditionsText, setConditionsText] = useState(
    profile.conditions?.join(", ") || "",
  );
  const [flagsText, setFlagsText] = useState(
    profile.medication_flags?.join(", ") || "",
  );
  const field = <K extends keyof Profile>(k: K, v: Profile[K]) =>
    setP({ ...p, [k]: v });
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const labels = (value: string) =>
        value
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
      await api(`/profiles/${p.id}`, "PUT", {
        ...p,
        conditions: labels(conditionsText),
        medication_flags: labels(flagsText),
      });
      onSaved();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="modal-shade">
      <section
        className="modal profile-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="profile-title"
      >
        <header>
          <div>
            <span className="eyebrow">PROFILE VERSION {p.version}</span>
            <h2 id="profile-title">Care that knows the person.</h2>
          </div>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Close profile editor"
          >
            <X />
          </button>
        </header>
        <form onSubmit={save}>
          <div className="privacy-callout">
            <span>
              Conditions and free-text notes stay on this server. Gemini
              receives only the selected categories, detector metrics and
              skeleton-rendered keyframes with consent.
            </span>
          </div>
          <fieldset className="risk-flags">
            <legend>Category flags allowed in Gemini context</legend>
            {(
              [
                "balance_support",
                "mobility_support",
                "transfer_assistance",
                "medication_fall_risk",
              ] as const
            ).map((category) => (
              <label className="check-row" key={category}>
                <input
                  type="checkbox"
                  checked={p.risk_categories?.includes(category) || false}
                  onChange={(e) =>
                    field(
                      "risk_categories",
                      e.target.checked
                        ? [...(p.risk_categories || []), category]
                        : (p.risk_categories || []).filter(
                            (c) => c !== category,
                          ),
                    )
                  }
                />
                {category.replaceAll("_", " ")}
              </label>
            ))}
            <label className="check-row">
              <input
                type="checkbox"
                checked={p.gemini_consent || false}
                onChange={(e) => field("gemini_consent", e.target.checked)}
              />
              Consent to Gemini receiving category flags and skeleton-rendered
              keyframes
            </label>
            <label>
              Demo data category
              <select
                value={p.data_category || "synthetic"}
                onChange={(e) =>
                  field(
                    "data_category",
                    e.target.value as Profile["data_category"],
                  )
                }
              >
                <option value="synthetic">Synthetic profile</option>
                <option value="consenting_teammate">Consenting teammate</option>
              </select>
            </label>
          </fieldset>
          <div className="form-grid">
            <label>
              Display alias
              <input
                required
                maxLength={60}
                value={p.alias}
                onChange={(e) => field("alias", e.target.value)}
              />
            </label>
            <label>
              Age
              <input
                type="number"
                min={18}
                max={120}
                value={p.age}
                onChange={(e) => field("age", +e.target.value)}
              />
            </label>
            <label>
              Mode
              <select
                value={p.mode}
                onChange={(e) =>
                  field("mode", e.target.value as Profile["mode"])
                }
              >
                <option value="care">Care</option>
                <option value="personal">Personal</option>
              </select>
            </label>
            <label>
              Mobility aid
              <select
                value={p.mobility_aid}
                onChange={(e) =>
                  field(
                    "mobility_aid",
                    e.target.value as Profile["mobility_aid"],
                  )
                }
              >
                {["none", "walker", "cane", "wheelchair"].map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            </label>
            <label className="span2">
              Conditions <small>Comma separated</small>
              <input
                value={conditionsText}
                onChange={(e) => setConditionsText(e.target.value)}
              />
            </label>
            <label className="span2">
              Medication flags <small>Flags only</small>
              <input
                value={flagsText}
                onChange={(e) => setFlagsText(e.target.value)}
              />
            </label>
            <label className="span2">
              Care notes
              <textarea
                maxLength={1000}
                value={p.care_notes}
                onChange={(e) => field("care_notes", e.target.value)}
              />
            </label>
            <label>
              Quiet hours start
              <input
                type="number"
                min={0}
                max={23}
                value={p.quiet_start}
                onChange={(e) => field("quiet_start", +e.target.value)}
              />
            </label>
            <label>
              Quiet hours end
              <input
                type="number"
                min={0}
                max={23}
                value={p.quiet_end}
                onChange={(e) => field("quiet_end", +e.target.value)}
              />
            </label>
            <label className="span2">
              Time zone
              <input
                value={p.timezone}
                onChange={(e) => field("timezone", e.target.value)}
              />
            </label>
            <label className="check span2">
              <input
                type="checkbox"
                checked={p.restrict_night_standing}
                onChange={(e) =>
                  field("restrict_night_standing", e.target.checked)
                }
              />{" "}
              Notify for standing without a nearby helper in quiet hours
            </label>
            <label className="check span2">
              <input
                type="checkbox"
                checked={p.check_in_first}
                onChange={(e) => field("check_in_first", e.target.checked)}
              />{" "}
              Ask the person first for level 1 events in Personal mode
            </label>
            <label>
              Check-in wait (seconds)
              <input
                type="number"
                min={10}
                max={300}
                value={p.check_in_seconds}
                onChange={(e) => field("check_in_seconds", +e.target.value)}
              />
            </label>
            <label>
              Skeleton retention (days)
              <input
                type="number"
                min={1}
                max={30}
                value={p.retain_days}
                onChange={(e) => field("retain_days", +e.target.value)}
              />
            </label>
            <label className="check span2">
              <input
                type="checkbox"
                checked={Boolean(p.bed_zone)}
                onChange={(e) =>
                  field(
                    "bed_zone",
                    e.target.checked
                      ? { x1: 0.05, y1: 0.25, x2: 0.35, y2: 0.95 }
                      : null,
                  )
                }
              />{" "}
              Enable a bed zone for this camera
            </label>
            {p.bed_zone && (
              <div className="span2 zone-fields">
                {(["x1", "y1", "x2", "y2"] as const).map((k) => (
                  <label key={k}>
                    {k}
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step={0.01}
                      value={p.bed_zone![k]}
                      onChange={(e) =>
                        field("bed_zone", {
                          ...p.bed_zone!,
                          [k]: +e.target.value,
                        })
                      }
                    />
                  </label>
                ))}
                <small>
                  Normalized corners: top-left (0,0), bottom-right (1,1).
                  Calibrate to your camera.
                </small>
              </div>
            )}
            <label className="check span2">
              <input
                type="checkbox"
                checked={p.consent}
                onChange={(e) => field("consent", e.target.checked)}
              />{" "}
              Consent to monitoring for this synthetic demonstration
            </label>
          </div>
          <div className="contacts-heading">
            <h3>People who can help</h3>
            <button
              type="button"
              className="text-button"
              onClick={() =>
                field("contacts", [
                  ...(p.contacts || []),
                  {
                    id: uid(),
                    name: "New contact",
                    role: "Trusted circle",
                    channel: "push",
                    min_urgency: 2,
                    delay_seconds: 180,
                    phone: "",
                  },
                ])
              }
            >
              <Plus size={14} /> Add contact
            </button>
          </div>
          {p.contacts?.map((c, i) => (
            <div className="contact-edit" key={c.id}>
              <label>
                Name
                <input
                  required
                  value={c.name}
                  onChange={(e) =>
                    field(
                      "contacts",
                      p.contacts!.map((x, j) =>
                        j === i ? { ...x, name: e.target.value } : x,
                      ),
                    )
                  }
                />
              </label>
              <label>
                Channel
                <select
                  value={c.channel}
                  onChange={(e) =>
                    field(
                      "contacts",
                      p.contacts!.map((x, j) =>
                        j === i
                          ? { ...x, channel: e.target.value as "push" | "call" }
                          : x,
                      ),
                    )
                  }
                >
                  <option value="push">Browser push</option>
                  <option value="call">Phone call</option>
                </select>
              </label>
              <label>
                Min. level
                <input
                  type="number"
                  min={1}
                  max={3}
                  value={c.min_urgency}
                  onChange={(e) =>
                    field(
                      "contacts",
                      p.contacts!.map((x, j) =>
                        j === i ? { ...x, min_urgency: +e.target.value } : x,
                      ),
                    )
                  }
                />
              </label>
              <label>
                Delay (sec)
                <input
                  type="number"
                  min={0}
                  max={3600}
                  value={c.delay_seconds}
                  onChange={(e) =>
                    field(
                      "contacts",
                      p.contacts!.map((x, j) =>
                        j === i ? { ...x, delay_seconds: +e.target.value } : x,
                      ),
                    )
                  }
                />
              </label>
              <button
                type="button"
                className="icon-button"
                aria-label={`Remove ${c.name}`}
                onClick={() =>
                  field(
                    "contacts",
                    p.contacts!.filter((_, j) => j !== i),
                  )
                }
              >
                <Trash2 size={16} />
              </button>
              {c.channel === "call" && (
                <label className="span-all">
                  Phone (E.164, must also be allowlisted)
                  <input
                    type="tel"
                    placeholder="+1…"
                    value={c.phone}
                    onChange={(e) =>
                      field(
                        "contacts",
                        p.contacts!.map((x, j) =>
                          j === i ? { ...x, phone: e.target.value } : x,
                        ),
                      )
                    }
                  />
                </label>
              )}
            </div>
          ))}
          <p className="muted">
            Skeleton-only privacy. Camera video stays on the capture device.
            Walker recognition is outside this demo.
          </p>
          {error && (
            <div role="alert" className="error">
              {error}
            </div>
          )}
          <footer>
            <button className="secondary" type="button" onClick={onClose}>
              Cancel
            </button>
            <button className="primary" disabled={busy}>
              <Save size={16} />
              {busy ? "Saving…" : "Save profile"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
