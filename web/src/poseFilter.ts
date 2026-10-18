import type { Landmark } from "./api";

// Split-second tracking gaps are filled with the last good pose. Kept below the
// backend's .5 s brief-dropout tolerance so both sides agree on what a blip is.
export const HOLD_SECONDS = 0.45;
const RESET_SECONDS = 1;

// One Euro filter (Casiez et al., CHI 2012): strong smoothing while a joint is
// nearly still, little lag while it moves fast, so idle jitter disappears but a
// fall stays sharp. Units: normalized image coordinates per second.
const MIN_CUTOFF_HZ = 0.6;
const BETA = 8;
const DERIVATIVE_CUTOFF_HZ = 1;

const alpha = (cutoff: number, dt: number) => 1 / (1 + 1 / (2 * Math.PI * cutoff * dt));

class OneEuro {
  private value = NaN;
  private speed = 0;
  private time = NaN;

  filter(raw: number, t: number) {
    if (t <= this.time) return this.value; // duplicate or out-of-order frame
    if (!Number.isFinite(this.value) || t - this.time > RESET_SECONDS) {
      this.value = raw;
      this.speed = 0;
      this.time = t;
      return raw;
    }
    const dt = t - this.time;
    this.speed += alpha(DERIVATIVE_CUTOFF_HZ, dt) * ((raw - this.value) / dt - this.speed);
    this.value += alpha(MIN_CUTOFF_HZ + BETA * Math.abs(this.speed), dt) * (raw - this.value);
    this.time = t;
    return this.value;
  }
}

export type StablePose = {
  /** Pose to draw and score, or null once the person has been gone past the hold. */
  pose: Landmark[] | null;
  /** People in view after absorbing brief blips. */
  people: number;
  /** True when the pose is the last good one, repeated through a brief gap. */
  held: boolean;
};

const hip = (pose: Landmark[]) => [(pose[23].x + pose[24].x) / 2, (pose[23].y + pose[24].y) / 2];

export class PoseStabilizer {
  private joints: OneEuro[][] = [];
  private visibility: number[] = [];
  private last: Landmark[] | null = null;
  private lastSeen = -Infinity;
  private crowdSince: number | null = null;

  reset() {
    this.joints = [];
    this.visibility = [];
    this.last = null;
    this.lastSeen = -Infinity;
    this.crowdSince = null;
  }

  update(detected: Landmark[][], t: number): StablePose {
    if (detected.length === 1) {
      this.crowdSince = null;
      return { pose: this.track(detected[0], t), people: 1, held: false };
    }
    const recent = this.last !== null && t - this.lastSeen <= HOLD_SECONDS;
    if (detected.length > 1) {
      // A second detection for a moment is usually a ghost. Keep following the
      // person nearest the tracked one; a real second person persists past the hold.
      this.crowdSince ??= t;
      if (this.last && t - this.crowdSince <= HOLD_SECONDS) {
        const [x, y] = hip(this.last);
        const nearest = detected.reduce((best, pose) => {
          const [bx, by] = hip(best), [px, py] = hip(pose);
          return Math.hypot(px - x, py - y) < Math.hypot(bx - x, by - y) ? pose : best;
        });
        return { pose: this.track(nearest, t), people: 1, held: false };
      }
      this.forget();
      return { pose: null, people: detected.length, held: false };
    }
    this.crowdSince = null;
    if (recent) return { pose: this.last, people: 1, held: true };
    this.forget();
    return { pose: null, people: 0, held: false };
  }

  private track(raw: Landmark[], t: number) {
    const pose = raw.map((point, i) => {
      const filters = (this.joints[i] ??= [new OneEuro(), new OneEuro(), new OneEuro()]);
      const seen = this.visibility[i];
      // Visibility fades rather than snapping, so joints do not blink on and off.
      this.visibility[i] = seen === undefined ? point.visibility : seen + 0.35 * (point.visibility - seen);
      return {
        x: filters[0].filter(point.x, t),
        y: filters[1].filter(point.y, t),
        z: filters[2].filter(point.z, t),
        visibility: this.visibility[i],
      };
    });
    this.last = pose;
    this.lastSeen = t;
    return pose;
  }

  private forget() {
    // Re-acquire fresh after a real loss instead of sliding from a stale position.
    this.joints = [];
    this.visibility = [];
    this.last = null;
  }
}
