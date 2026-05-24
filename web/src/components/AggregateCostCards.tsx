// KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — side-by-side
// per-tenant cost cards rendered when the operator picks the
// "All tenants" aggregate option in the cockpit chrome.
//
// Data source: ``snapshot.cost_ladder_by_tenant`` (snapshot v6
// sibling block from #206). The block is keyed by tenant_id; each
// value carries current_tier / monthly_budget_pct_used /
// spent_to_date_usd / credit_pool_usd. ``model_default`` is router-
// side (not per-tenant in v6) — we don't surface it in the
// aggregate cards.
//
// Order: "default" first (canonical anchor), then alphabetical.
// availableTenants from useActiveTenant supplies the canonical
// sorted list (already default-first per #207 BE); we render an
// empty-state card for tenants listed there but absent from
// cost_ladder_by_tenant (means no activity / no holder
// initialization yet — distinct from "tenant unknown").
//
// Aggregate footer (A.4): total spent + combined credit pool
// across all tenants whose data is present. Skipped when only one
// tenant has data (no aggregate value over a single number).
//
// Why a snapshot read rather than fan-out per-tenant /api/cost-
// state: snapshot is $0-cost (already cached on disk for 10 min),
// returns every tenant in one shot, and the per-tenant block
// shape was designed exactly for this consumer in #206. Fan-out
// would be N round-trips for N tenants.

import { useEffect, useState } from "react";
import { AlertTriangle, DollarSign, Users } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { SnapshotResponse } from "@/lib/api";
import {
  DEFAULT_TENANT_ID,
  useActiveTenant,
} from "@/hooks/useActiveTenant";

// Snapshot v6 by-tenant block shape (per kora_cli/snapshot/state_snapshot.py
// _project_holder_for_snapshot). credit_pool_usd is number; the
// other two USD-related fields degrade to the "unknown" literal
// when the holder hasn't initialized yet.
interface TenantCostBlock {
  current_tier: string;
  monthly_budget_pct_used: number | null;
  spent_to_date_usd: number | "unknown";
  credit_pool_usd: number;
}

const RUNG_LABEL: Record<string, string> = {
  normal: "NORMAL",
  warn_75: "WARN 75%",
  downshift_90: "DOWNSHIFT 90%",
  hard_stop_100: "HARD STOP 100%",
};

const RUNG_TONE: Record<string, "success" | "warning" | "destructive"> = {
  normal: "success",
  warn_75: "warning",
  downshift_90: "warning",
  hard_stop_100: "destructive",
};

function formatUsd(value: number | "unknown"): string {
  if (value === "unknown" || typeof value !== "number") return "—";
  return `$${value.toFixed(2)}`;
}

function rungTone(tier: string): "success" | "warning" | "destructive" {
  return RUNG_TONE[tier] ?? "warning";
}

function rungLabel(tier: string): string {
  return RUNG_LABEL[tier] ?? tier.toUpperCase();
}

interface AggregateCostCardsProps {
  /** Force a snapshot reload — pass an incrementing counter from the
   *  parent Refresh button. Optional; cards also reload on tab focus
   *  via useActiveTenant's available-tenants refetch path. */
  reloadKey?: number;
}

export function AggregateCostCards({ reloadKey }: AggregateCostCardsProps) {
  const { availableTenants } = useActiveTenant();
  const [snap, setSnap] = useState<SnapshotResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getSnapshot()
      .then((resp) => {
        if (cancelled) return;
        if ("error" in resp) {
          setSnap(null);
          setError("Snapshot unavailable — aggregate view needs the daemon snapshot");
        } else {
          setSnap(resp);
        }
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  if (error || snap === null) {
    return (
      <Card>
        <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
          <AlertTriangle className="h-4 w-4 mt-0.5" />
          <div>
            <div className="font-medium">Failed to load aggregate cost view</div>
            <div className="text-xs opacity-80">
              {error ?? "Snapshot read returned no data"}
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  const byTenant = (snap.cost_ladder_by_tenant ?? {}) as Record<
    string,
    TenantCostBlock
  >;

  // Render order: default first (canonical), then alphabetical for
  // the rest. Use availableTenants as the canonical list — the
  // snapshot's by-tenant block may lag (holder initialized but
  // snapshot from 5 min ago), but availableTenants is live.
  const tenants = [...availableTenants].sort((a, b) => {
    if (a === DEFAULT_TENANT_ID) return -1;
    if (b === DEFAULT_TENANT_ID) return 1;
    return a.localeCompare(b);
  });

  // Aggregate footer math — sum only the tenants with numeric data.
  // Mixing "unknown" sentinels into a sum would silently zero them;
  // skip them and surface a count of skipped tenants instead.
  const summable = tenants
    .map((t) => byTenant[t])
    .filter((b): b is TenantCostBlock & { spent_to_date_usd: number } => {
      return b !== undefined && typeof b.spent_to_date_usd === "number";
    });
  const totalSpent = summable.reduce(
    (acc, b) => acc + b.spent_to_date_usd,
    0,
  );
  const totalPool = summable.reduce(
    (acc, b) => acc + (b.credit_pool_usd ?? 0),
    0,
  );
  const skippedCount = tenants.length - summable.length;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {tenants.map((tenantId) => (
          <TenantCostCard
            key={tenantId}
            tenantId={tenantId}
            block={byTenant[tenantId]}
          />
        ))}
      </div>

      {/* Aggregate footer — only meaningful with ≥ 2 summable
          tenants. Single-tenant case shows nothing (the card itself
          already carries the same numbers). */}
      {summable.length >= 2 && (
        <Card className="border-primary/30 bg-primary/5">
          <CardContent className="py-3 flex items-center gap-3 text-sm flex-wrap">
            <Users className="h-4 w-4 text-primary" />
            <span>
              <span className="font-semibold">{formatUsd(totalSpent)}</span>{" "}
              spent across {summable.length} tenants this period
            </span>
            <span className="text-muted-foreground">·</span>
            <span className="text-muted-foreground">
              {formatUsd(totalPool)} combined credit pool
            </span>
            {skippedCount > 0 && (
              <>
                <span className="text-muted-foreground">·</span>
                <span className="text-xs text-muted-foreground italic">
                  {skippedCount} tenant{skippedCount === 1 ? "" : "s"} with no
                  data yet (excluded from totals)
                </span>
              </>
            )}
          </CardContent>
        </Card>
      )}

      <p className="text-xs text-muted-foreground italic">
        Aggregate view reads the daemon snapshot (≤ 10 min old). Per-tenant
        deferred tickets, reconciliation history, and rate-limit pulses live
        on the single-tenant view — switch the tenant picker from{" "}
        <span className="font-mono">All tenants</span> to a specific tenant
        for the full per-tenant detail.
      </p>
    </div>
  );
}

interface TenantCostCardProps {
  tenantId: string;
  block?: TenantCostBlock;
}

function TenantCostCard({ tenantId, block }: TenantCostCardProps) {
  if (!block) {
    // Tenant listed by /api/tenants/list but absent from the
    // snapshot's by-tenant block. Either the holder hasn't
    // initialized yet OR the snapshot is older than the holder
    // registration. Render a calm empty state rather than hiding
    // the card (the operator picked "All tenants" — they want to
    // see all of them, including the ones with no data yet).
    return (
      <Card className="border-current/10">
        <CardContent className="py-4 flex flex-col gap-2">
          <div className="flex items-center gap-1.5">
            <DollarSign className="h-4 w-4 text-muted-foreground" />
            <span className="font-mono text-sm truncate">{tenantId}</span>
          </div>
          <p className="text-xs text-muted-foreground italic">
            No activity in the current snapshot.
          </p>
        </CardContent>
      </Card>
    );
  }

  const tone = rungTone(block.current_tier);
  const label = rungLabel(block.current_tier);
  const pct = block.monthly_budget_pct_used ?? 0;

  return (
    <Card>
      <CardContent className="py-4 flex flex-col gap-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <DollarSign className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="font-mono text-sm truncate">{tenantId}</span>
            {tenantId === DEFAULT_TENANT_ID && (
              <span className="text-[10px] text-muted-foreground italic">
                (canonical)
              </span>
            )}
          </div>
          <Badge tone={tone}>{label}</Badge>
        </div>

        <div className="flex items-baseline gap-1.5">
          <span className="text-xl font-semibold">
            {formatUsd(block.spent_to_date_usd)}
          </span>
          <span className="text-xs text-muted-foreground">
            / {formatUsd(block.credit_pool_usd)}
          </span>
        </div>

        {/* Simple progress bar — pct-used over budget. Tone tracks
            the rung so a downshifted tenant glows warning-yellow. */}
        <div
          className="h-1.5 w-full overflow-hidden rounded-full bg-muted/40"
          role="progressbar"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${tenantId} budget usage`}
        >
          <div
            className={
              tone === "destructive"
                ? "h-full bg-destructive"
                : tone === "warning"
                  ? "h-full bg-warning"
                  : "h-full bg-success"
            }
            style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
          />
        </div>
        <div className="text-[10px] text-muted-foreground uppercase tracking-wide">
          {pct.toFixed(1)}% of monthly budget used
        </div>
      </CardContent>
    </Card>
  );
}
