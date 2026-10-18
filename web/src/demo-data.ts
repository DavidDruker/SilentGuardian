import type { GuardianEvent, Profile } from "./api";

export type DemoSettings = { alerts: number; density: number };
export const defaultDemoSettings: DemoSettings = { alerts: 12, density: 85 };

const key = (id: string) => `hayvn.demo.${id}`;
const bounded = (value: unknown, fallback: number, max: number) =>
  typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.min(max, Math.round(value))) : fallback;

export function readDemoSettings(id: string): DemoSettings {
  try {
    const saved = JSON.parse(localStorage.getItem(key(id)) || "{}");
    return {
      alerts: bounded(saved.alerts, defaultDemoSettings.alerts, 30),
      density: bounded(saved.density, defaultDemoSettings.density, 100),
    };
  } catch { return defaultDemoSettings; }
}

export function saveDemoSettings(id: string, settings: DemoSettings) {
  try { localStorage.setItem(key(id), JSON.stringify(settings)); } catch { /* private browsing */ }
}

export function profileSeed(id: string): number {
  let seed = 2166136261;
  for (const char of id) seed = Math.imul(seed ^ char.charCodeAt(0), 16777619);
  return seed >>> 0;
}

export function sampleUnit(seed: number): number {
  let x = seed >>> 0;
  x ^= x << 13; x ^= x >>> 17; x ^= x << 5;
  return (x >>> 0) / 4294967296;
}

const examples = [
  ["unsteady", "Unsteady movement detected", 2],
  ["near_fall", "Abrupt change in posture", 2],
  ["inactivity", "Long period without movement", 1],
  ["bed_exit", "Movement during quiet hours", 1],
  ["fall", "Possible fall detected", 3],
] as const;

export function demoAlerts(profile: Profile, count: number): GuardianEvent[] {
  const seed = profileSeed(profile.id);
  const now = Math.floor(Date.now() / 3600000) * 3600;
  return Array.from({ length: count }, (_, index) => {
    const variant = examples[Math.floor(sampleUnit(seed + index * 19) * examples.length)];
    const stamp = now - (index * 13 + 2 + Math.floor(sampleUnit(seed + index * 47) * 7)) * 3600;
    return {
      id: `sample-${profile.id}-${index}`, person_id: profile.id, alias: profile.alias,
      mode: profile.mode, profile_version: profile.version, source: "sample",
      opened_at: stamp, observed_at: stamp, updated_at: stamp,
      rule_hits: [variant[0]], floor_urgency: variant[2], urgency: variant[2],
      status: "sample", summary: variant[1], verdict_status: "sample", verdict: null,
      features: {}, keyframes: [], acknowledged_at: null, acknowledged_by: null,
      next_escalation_at: null, notifications: [], rule_summary: "Sample data",
    };
  });
}
