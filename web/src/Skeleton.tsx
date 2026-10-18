import type { Landmark } from "./api";
export const edges = [
  [11, 12],
  [11, 23],
  [12, 24],
  [23, 24],
  [11, 13],
  [13, 15],
  [12, 14],
  [14, 16],
  [23, 25],
  [25, 27],
  [24, 26],
  [26, 28],
];
// Body joints plus the head. Face, finger and toe points are the noisiest
// MediaPipe outputs and add twitching without adding information.
const joints = [0, ...new Set(edges.flat())];
// Fade low-confidence joints instead of letting them blink in and out.
const opacity = (point?: Landmark) => Math.min(1, Math.max(0, ((point?.visibility ?? 0) - 0.2) / 0.4));

export function Skeleton({ pose }: { pose: Landmark[] }) {
  return (
    <svg
      viewBox="0 0 640 480"
      preserveAspectRatio="none"
      className="skeleton"
      role="img"
      aria-label="Skeleton landmarks, with no video transmitted"
    >
      <g stroke="#8cc1ea" fill="#ffffff" strokeLinecap="round">
        {edges.map(
          ([a, b]) =>
            pose[a] &&
            pose[b] && (
              <line
                key={`${a}-${b}`}
                x1={pose[a].x * 640}
                y1={pose[a].y * 480}
                x2={pose[b].x * 640}
                y2={pose[b].y * 480}
                strokeWidth="3"
                opacity={Math.min(opacity(pose[a]), opacity(pose[b]))}
              />
            ),
        )}
        {joints.map((j) => pose[j] && (
          <circle
            key={j}
            cx={pose[j].x * 640}
            cy={pose[j].y * 480}
            r={j === 0 ? 8 : 3}
            stroke="none"
            opacity={opacity(pose[j])}
          />
        ))}
      </g>
    </svg>
  );
}
