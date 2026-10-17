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
export function Skeleton({
  poses,
  selected = 0,
}: {
  poses: Landmark[][];
  selected?: number;
}) {
  return (
    <svg
      viewBox="0 0 640 480"
      className="skeleton"
      role="img"
      aria-label="Skeleton landmarks, with no video transmitted"
    >
      {poses.map((pose, i) => (
        <g
          key={i}
          stroke={i === selected ? "#8cc1ea" : "#6f7f96"}
          fill={i === selected ? "#ffffff" : "#6f7f96"}
          opacity={i === selected ? 1 : 0.6}
        >
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
          {pose[0] && (
            <text
              x={pose[0].x * 640 + 18}
              y={pose[0].y * 480}
              stroke="none"
              fontSize="13"
            >
              {i + 1}
            </text>
          )}
        </g>
      ))}
    </svg>
  );
}
