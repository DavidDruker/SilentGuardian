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
export function Skeleton({ pose }: { pose: Landmark[] }) {
  return (
    <svg
      viewBox="0 0 640 480"
      preserveAspectRatio="none"
      className="skeleton"
      role="img"
      aria-label="Skeleton landmarks, with no video transmitted"
    >
      <g stroke="#8cc1ea" fill="#ffffff">
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
              />
            ),
        )}
        {pose.map((p, j) => (
          <circle
            key={j}
            cx={p.x * 640}
            cy={p.y * 480}
            r={j === 0 ? 8 : 3}
            stroke="none"
          />
        ))}
      </g>
    </svg>
  );
}
