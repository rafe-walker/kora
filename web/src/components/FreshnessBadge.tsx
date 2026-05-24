// Dashboard snapshot freshness + cost-economy indicator —
// KR-FE-DASHBOARD-SNAPSHOT-WIRE.
//
// Per the unified-operator-interface lens + cheap-substrate thesis
// (feedback-opus-escalation-must-be-earned), $0 paths should be
// visible to the operator AS $0. The badge makes the difference
// between "page-load read the daemon snapshot" ($0) and "page-load
// fanned out 8 live API calls" (cents) explicit.
//
// Renders one of four shapes:
//   * snapshot  — "Snapshot from N min ago · $0 cost view"
//   * live      — "Live (just refreshed) · live fetch"
//   * unavailable — "Live fetch · backend snapshot unavailable"
//                  (no shame about the fallback; just transparent state)
//   * mixed     — "Snapshot · K field(s) force-refreshed"
//                 (per-card refresh overrode some snapshot-projected values)
//
// The Force-refresh button is always present and triggers a full
// live fan-out (the explicit "I want live data even though
// snapshot was fresh" path).

import { RefreshCw } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { formatRelative } from "@/lib/panelHelpers";

// Local helper since KR-FE-OPS-QUALITY-PASS (#155) — which exports
// timestampAbsoluteUtc from panelHelpers.ts — hasn't merged into
// this branch's base. Inlined to keep this PR's diff focused; a
// follow-on can swap to the shared helper once #155 lands.
function tooltipIso(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export type FreshnessMode = "snapshot" | "live" | "unavailable";

export interface FreshnessBadgeProps {
  mode: FreshnessMode;
  /** ISO timestamp; null when not from a snapshot read. */
  snapshotAt: string | null;
  /** ISO timestamp of the most-recent live-fetch action (force or
   *  per-card refresh). null on initial mount before any live work. */
  liveAt: string | null;
  /** Per-card refresh count — fields that have been live-overridden
   *  on top of the snapshot baseline. Drives the "mixed" sub-mode. */
  liveOverrideCount: number;
  /** KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — count of the originally-
   *  spec'd dashboard hero fields actually projected from snapshot
   *  on this load (vs falling back to fan-out per-field for
   *  incomplete snapshot data). Used to render
   *  "(N of 4 from snapshot)" so the operator can tell at a glance
   *  whether the full warm-cache path is delivering or only partial.
   *  Pass 0 / undefined for the pre-fully-wired baseline. */
  snapshotProjectedHeroCount?: number;
  /** Total hero fields originally spec'd by KR-FE-DASHBOARD-SNAPSHOT-WIRE.
   *  Currently 4 (operational + alerts + cost + health). Pinned in
   *  the dashboard as a literal so the badge text is testable. */
  totalHeroFields?: number;
  /** Triggered by the badge's Force-refresh button. */
  onForceRefresh: () => void;
  /** Disabled when a refresh is in-flight (prevents re-entrancy). */
  refreshing?: boolean;
}

export function FreshnessBadge({
  mode,
  snapshotAt,
  liveAt,
  liveOverrideCount,
  snapshotProjectedHeroCount,
  totalHeroFields,
  onForceRefresh,
  refreshing = false,
}: FreshnessBadgeProps) {
  // Compose the headline + tone per mode. Tones use the panel
  // palette (success/muted/warning) so the badge slots into the
  // dashboard chrome without standing out.
  let headline: string;
  let costHint: string;
  let toneClass: string;

  if (mode === "snapshot") {
    if (liveOverrideCount > 0) {
      // Mixed sub-mode — operator has refreshed at least one card
      // on top of the snapshot baseline.
      headline = `Snapshot from ${formatRelative(snapshotAt)} · ${liveOverrideCount} field${liveOverrideCount === 1 ? "" : "s"} force-refreshed`;
      costHint = "mixed cost view";
      toneClass =
        "border-warning/30 bg-warning/5 text-foreground";
    } else {
      headline = `Snapshot from ${formatRelative(snapshotAt)}`;
      // KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — when the caller
      // tells us how many hero fields actually came from snapshot
      // (vs partial-snapshot fan-out fallback), surface that
      // explicitly so the operator can tell "all 4 fields fresh"
      // from "snapshot fresh but cost still fanned out because
      // holder hadn't initialized."
      const heroN = snapshotProjectedHeroCount;
      const heroTotal = totalHeroFields;
      if (
        typeof heroN === "number" &&
        typeof heroTotal === "number" &&
        heroTotal > 0
      ) {
        costHint =
          heroN >= heroTotal
            ? `$0 cost view · all ${heroTotal} hero fields from snapshot`
            : `$0 cost view · ${heroN} of ${heroTotal} hero fields from snapshot`;
      } else {
        costHint = "$0 cost view";
      }
      toneClass =
        "border-success/30 bg-success/5 text-foreground";
    }
  } else if (mode === "live") {
    headline = liveAt
      ? `Live (refreshed ${formatRelative(liveAt)})`
      : "Live (just refreshed)";
    costHint = "live fetch";
    toneClass = "border-border bg-card text-foreground";
  } else {
    // unavailable
    headline = "Live fetch";
    costHint = "backend snapshot unavailable";
    toneClass =
      "border-muted-foreground/30 bg-muted/30 text-muted-foreground";
  }

  const titleHint =
    mode === "snapshot" && snapshotAt
      ? `Snapshot computed_at: ${tooltipIso(snapshotAt)}`
      : mode === "live" && liveAt
        ? `Last live fetch: ${tooltipIso(liveAt)}`
        : undefined;

  return (
    <div
      className={`inline-flex items-center gap-2 px-2.5 py-1 rounded-md border text-xs ${toneClass}`}
      title={titleHint}
    >
      <span className="font-medium">{headline}</span>
      <span aria-hidden="true">·</span>
      <span className="text-muted-foreground italic">{costHint}</span>
      <Button
        size="sm"
        ghost
        disabled={refreshing}
        onClick={onForceRefresh}
        title="Force a full live fan-out — bypasses the snapshot"
      >
        <RefreshCw
          className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`}
        />
        Force live refresh
      </Button>
    </div>
  );
}
