// 14-day daily-count sparkline. Plain SVG (no chart-library
// dep — same discipline as CostTelemetryPage from PR #164).
//
// Extracted verbatim from EmailIntentLogPage (PR #180) +
// OutboundEmailLogPage (PR #183). The only previous variation
// was the trailing-suffix text ("created · 14d" vs "sent · 14d")
// — now parameterized as `totalSuffix`.

import type { DailyCountPoint } from "./types";

export interface SparklineProps {
  points: DailyCountPoint[];
  width?: number;
  height?: number;
  /** Suffix shown after the total count (e.g. "created · 14d"
   *  or "sent · 14d" or "actions · 14d"). Pluralization-aware
   *  callers should compose this themselves. */
  totalSuffix?: string;
  /** Optional aria-label override; default is built from
   *  points.length so screen readers get an accurate window
   *  description. */
  ariaLabel?: string;
}

export function Sparkline({
  points,
  width = 220,
  height = 36,
  totalSuffix = "events · 14d",
  ariaLabel,
}: SparklineProps) {
  if (points.length === 0) return null;
  const max = Math.max(1, ...points.map((p) => p.count));
  // Layout: bars (one per day) with 1px gap. Bar width derived
  // from container width; bars rendered as <rect>s.
  const barWidth = Math.max(2, (width - (points.length - 1) * 1) / points.length);
  const total = points.reduce((acc, p) => acc + p.count, 0);
  const label = ariaLabel ?? `Daily counts over the last ${points.length} days`;
  return (
    <div className="flex items-center gap-2">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        aria-label={label}
        role="img"
      >
        {points.map((p, i) => {
          const x = i * (barWidth + 1);
          // Empty days render as 1px-tall ghost bar so operator
          // sees the day-grid position; non-empty bars scale
          // linearly to height.
          const h = p.count === 0 ? 1 : Math.max(2, (p.count / max) * height);
          const y = height - h;
          return (
            <rect
              key={p.date}
              x={x}
              y={y}
              width={barWidth}
              height={h}
              className={p.count === 0 ? "fill-muted/40" : "fill-green-500"}
            >
              <title>{`${p.date}: ${p.count}`}</title>
            </rect>
          );
        })}
      </svg>
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {total} {totalSuffix}
      </span>
    </div>
  );
}
