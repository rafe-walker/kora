// Show More affordance for timeline panels — KR-FE-OPS-QUALITY-PASS.
//
// The 4 timeline endpoints (slack-dm / agent-activity / reasoning /
// webhook-events) accept ?limit=N (default 50; backend cap 200) but
// the operator can't see >50 rows without manual URL construction.
// This footer surfaces the cap-bump UX subtly at the bottom of each
// timeline.
//
// Tiers: 50 → 100 → 200 (backend cap). At cap, the button is replaced
// with a terminus line pointing the operator at the forensic data
// sources for older entries.

import { ChevronDown } from "lucide-react";

// Tier ladder. Mirrors the backend's 200 cap in
// kora_cli/web_server.py (every limit-aware endpoint capped at 200
// via `max(1, min(limit, 200))`). FE clamps to the same ceiling so
// the operator's click can't out-grow what the endpoint will serve.
export const SHOW_MORE_TIERS: ReadonlyArray<number> = [50, 100, 200];
export const SHOW_MORE_DEFAULT_LIMIT = SHOW_MORE_TIERS[0];
export const SHOW_MORE_BACKEND_CAP =
  SHOW_MORE_TIERS[SHOW_MORE_TIERS.length - 1];

export function nextShowMoreTier(current: number): number | null {
  const idx = SHOW_MORE_TIERS.indexOf(current);
  if (idx === -1) {
    // Operator-set limit that doesn't match a tier — find the next
    // tier above current, or null at-cap.
    const next = SHOW_MORE_TIERS.find((t) => t > current);
    return next ?? null;
  }
  if (idx + 1 >= SHOW_MORE_TIERS.length) return null;
  return SHOW_MORE_TIERS[idx + 1];
}

export function ShowMoreFooter({
  currentLimit,
  totalShown,
  onShowMore,
  unitLabel = "entries",
}: {
  currentLimit: number;
  totalShown: number;
  onShowMore: (next: number) => void;
  /** Per-panel unit name for the visible "Showing N <unit>" line. */
  unitLabel?: string;
}) {
  const next = nextShowMoreTier(currentLimit);

  // Don't render the footer at all if we haven't even filled one
  // tier — the operator can see all rows; the Show More button
  // would suggest more exist when none do.
  if (totalShown < currentLimit && totalShown < SHOW_MORE_BACKEND_CAP) {
    return null;
  }

  if (next === null) {
    // At backend cap — show forensic-entry-point terminus.
    return (
      <div className="text-center text-xs text-muted-foreground py-3 italic">
        Showing {totalShown} {unitLabel} (backend cap; older entries
        via JSONL / substrate forensics)
      </div>
    );
  }

  return (
    <div className="text-center text-xs text-muted-foreground py-3 flex items-center justify-center gap-2">
      <span>
        Showing {totalShown} {unitLabel}
      </span>
      <span aria-hidden="true">·</span>
      <button
        type="button"
        onClick={() => onShowMore(next)}
        className="inline-flex items-center gap-1 underline-offset-2 hover:underline hover:text-foreground"
      >
        Show more
        <ChevronDown className="h-3 w-3" />
      </button>
    </div>
  );
}
