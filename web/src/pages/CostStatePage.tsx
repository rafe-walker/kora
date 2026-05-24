import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import { usePanelView } from "@/hooks/usePanelView";
import type {
  CostRung,
  CostStateResponse,
  Criticality,
  DeferredTicket,
  ModelTier,
  RateLimitWindow,
  ReconciliationEntry,
} from "@/lib/api";

const RUNG_TONE: Record<CostRung, "success" | "warning" | "destructive"> = {
  normal: "success",
  warn_75: "warning",
  downshift_90: "warning",
  hard_stop_100: "destructive",
};

const RUNG_LABEL: Record<CostRung, string> = {
  normal: "NORMAL",
  warn_75: "WARN 75%",
  downshift_90: "DOWNSHIFT 90%",
  hard_stop_100: "HARD STOP 100%",
};

const MODEL_TIER_TONE: Record<ModelTier, "destructive" | "warning" | "outline"> = {
  opus: "destructive",
  sonnet: "warning",
  haiku: "outline",
};

const CRITICALITY_TONE: Record<Criticality, "destructive" | "warning" | "outline" | "success"> = {
  frontier: "destructive",
  high: "warning",
  normal: "outline",
  low: "success",
};

function formatUsd(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

function formatTimestamp(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatRelative(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const deltaMs = d.getTime() - Date.now();
  const absSec = Math.abs(deltaMs) / 1000;
  if (absSec < 60) return deltaMs < 0 ? "just now" : "in <1 min";
  const absMin = absSec / 60;
  if (absMin < 60) {
    const n = Math.round(absMin);
    return deltaMs < 0 ? `${n} min ago` : `in ${n} min`;
  }
  const absHr = absMin / 60;
  if (absHr < 24) {
    const n = Math.round(absHr);
    return deltaMs < 0 ? `${n}h ago` : `in ${n}h`;
  }
  const absDay = absHr / 24;
  const n = Math.round(absDay);
  return deltaMs < 0 ? `${n}d ago` : `in ${n}d`;
}

interface RungProgressBarProps {
  pctUsed: number;
  activeRung: CostRung;
}

function RungProgressBar({ pctUsed, activeRung }: RungProgressBarProps) {
  const clamped = Math.max(0, Math.min(100, pctUsed));
  // Fill colour follows the active rung — warn=amber, downshift=orange,
  // hard_stop=red, normal=green. We don't have an "orange" Tailwind
  // semantic token in this codebase, so downshift_90 reuses warning with
  // an opacity bump to read distinctly from warn_75.
  const fillClass =
    activeRung === "hard_stop_100"
      ? "bg-destructive"
      : activeRung === "downshift_90"
        ? "bg-warning"
        : activeRung === "warn_75"
          ? "bg-warning/70"
          : "bg-success";

  return (
    <div className="relative h-4 w-full rounded bg-muted overflow-hidden">
      {/* Fill */}
      <div
        className={`absolute inset-y-0 left-0 ${fillClass} transition-all`}
        style={{ width: `${clamped}%` }}
        aria-hidden
      />
      {/* Tick marks at 75 / 90 / 100 */}
      {[75, 90, 100].map((pct) => (
        <div
          key={pct}
          className="absolute inset-y-0 w-px bg-foreground/40"
          style={{ left: `${pct}%` }}
          aria-hidden
          title={`${pct}% rung`}
        />
      ))}
      {/* Pct label centered */}
      <div className="absolute inset-0 flex items-center justify-center text-[10px] font-medium text-foreground/80">
        {clamped.toFixed(1)}%
      </div>
    </div>
  );
}

interface ProjectionCalloutProps {
  projectedUsd: number;
  poolUsd: number;
}

function ProjectionCallout({ projectedUsd, poolUsd }: ProjectionCalloutProps) {
  const ratio = poolUsd > 0 ? projectedUsd / poolUsd : 0;
  if (ratio > 1.0) {
    return (
      <div className="rounded border border-destructive/40 bg-destructive/10 p-3 flex items-start gap-2 text-sm">
        <AlertTriangle className="h-4 w-4 mt-0.5 text-destructive shrink-0" />
        <div>
          <div className="font-medium text-destructive">
            Projected to exceed {formatUsd(poolUsd)} by month-end
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            Projection {formatUsd(projectedUsd)} (
            {(ratio * 100).toFixed(0)}% of pool)
          </div>
        </div>
      </div>
    );
  }
  if (ratio > 0.9) {
    return (
      <div className="rounded border border-warning/40 bg-warning/10 p-3 flex items-start gap-2 text-sm">
        <TrendingUp className="h-4 w-4 mt-0.5 text-warning shrink-0" />
        <div>
          <div className="font-medium">
            On track to cross the 90% rung
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            Projection {formatUsd(projectedUsd)} (
            {(ratio * 100).toFixed(0)}% of pool)
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="rounded border border-success/40 bg-success/10 p-3 flex items-start gap-2 text-sm">
      <ShieldCheck className="h-4 w-4 mt-0.5 text-success shrink-0" />
      <div>
        <div className="font-medium">On track within budget</div>
        <div className="text-xs text-muted-foreground mt-0.5">
          Projection {formatUsd(projectedUsd)} (
          {(ratio * 100).toFixed(0)}% of pool)
        </div>
      </div>
    </div>
  );
}

interface RateLimitBarProps {
  label: string;
  window: RateLimitWindow;
}

function RateLimitBar({ label, window: w }: RateLimitBarProps) {
  const remainingRatio = w.limit > 0 ? w.remaining / w.limit : 0;
  const remainingPct = Math.max(0, Math.min(100, remainingRatio * 100));
  const tone =
    remainingRatio < 0.1
      ? "bg-destructive"
      : remainingRatio < 0.25
        ? "bg-warning"
        : "bg-success";

  return (
    <div className="flex flex-col gap-1 flex-1 min-w-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium">{label}</span>
        <span className="text-xs text-muted-foreground">
          {w.remaining.toLocaleString()} / {w.limit.toLocaleString()}
        </span>
      </div>
      <div className="relative h-2 w-full rounded bg-muted overflow-hidden">
        <div
          className={`absolute inset-y-0 left-0 ${tone}`}
          style={{ width: `${remainingPct}%` }}
          aria-hidden
        />
      </div>
      <div className="text-[10px] text-muted-foreground">
        resets {formatRelative(w.reset_at)}
      </div>
    </div>
  );
}

interface DeferredTicketsTableProps {
  tickets: DeferredTicket[];
}

function DeferredTicketsTable({ tickets }: DeferredTicketsTableProps) {
  if (tickets.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">
        No tickets currently deferred for cost.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted-foreground border-b">
            <th className="py-2 pr-3 font-medium">Title</th>
            <th className="py-2 pr-3 font-medium">Criticality</th>
            <th className="py-2 pr-3 font-medium">Deferred</th>
            <th className="py-2 font-medium">Reason</th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((t) => (
            <tr key={t.id} className="border-b last:border-0 align-top">
              <td className="py-2 pr-3">
                <Link
                  to="/sea-tickets"
                  className="underline underline-offset-2 hover:text-primary"
                >
                  {t.title}
                </Link>
                <div>
                  <code className="text-xs text-muted-foreground">{t.id}</code>
                </div>
              </td>
              <td className="py-2 pr-3">
                <Badge tone={CRITICALITY_TONE[t.criticality]}>
                  {t.criticality}
                </Badge>
              </td>
              <td className="py-2 pr-3 text-xs">
                <div>{formatRelative(t.deferred_at)}</div>
                <div className="text-muted-foreground">
                  {formatTimestamp(t.deferred_at)}
                </div>
              </td>
              <td className="py-2 text-xs text-muted-foreground">{t.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface ReconciliationTableProps {
  entries: ReconciliationEntry[];
}

function ReconciliationTable({ entries }: ReconciliationTableProps) {
  if (entries.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">
        No reconciliations recorded.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted-foreground border-b">
            <th className="py-2 pr-3 font-medium">Reconciled</th>
            <th className="py-2 pr-3 font-medium">Local estimate</th>
            <th className="py-2 pr-3 font-medium">Anthropic reported</th>
            <th className="py-2 pr-3 font-medium">Delta</th>
            <th className="py-2 font-medium">Tolerance</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((r) => (
            <tr key={r.reconciled_at} className="border-b last:border-0 align-top">
              <td className="py-2 pr-3 text-xs">
                <div>{formatRelative(r.reconciled_at)}</div>
                <div className="text-muted-foreground">
                  {formatTimestamp(r.reconciled_at)}
                </div>
              </td>
              <td className="py-2 pr-3 text-xs">{formatUsd(r.local_estimator_usd)}</td>
              <td className="py-2 pr-3 text-xs">{formatUsd(r.anthropic_reported_usd)}</td>
              <td className="py-2 pr-3 text-xs">
                {formatUsd(r.delta_usd)} ({r.delta_pct.toFixed(2)}%)
              </td>
              <td className="py-2">
                {r.within_tolerance ? (
                  <CheckCircle2 className="h-4 w-4 text-success" />
                ) : (
                  <XCircle className="h-4 w-4 text-destructive" />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function CostStatePage() {
  usePanelView("CostStatePage");

  const [data, setData] = useState<CostStateResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  // Default-expanded if there are deferred tickets (operator should see
  // them immediately for context); collapsed otherwise.
  const [historyExpanded, setHistoryExpanded] = useState<boolean | null>(null);
  const { toast, showToast } = useToast();

  const loadState = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getCostState()
        .then((resp) => {
          setData(resp);
          if (historyExpanded === null) {
            setHistoryExpanded(resp.deferred_tickets.length > 0);
          }
        })
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load cost state: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [historyExpanded, showToast],
  );

  useEffect(() => {
    loadState(false);
    // Mount-only; loadState carries the cold-start guard.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sortedReconciliation = useMemo(
    () =>
      data
        ? [...data.reconciliation_history].sort((a, b) =>
            (b.reconciled_at ?? "").localeCompare(a.reconciled_at ?? ""),
          )
        : [],
    [data],
  );

  if (data === null && !loadError) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-start justify-between gap-4">
        <div>
          <H2>Cost State</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Kora's monthly Agent SDK credit burn — rung, downshift state,
            deferred tickets.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadState(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load cost state</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <ShieldAlert className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB DATA — cost-ladder runtime wire-in pending (KR-P2-K)
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Panel is a UI preview; values shown are sample data, not real
                burn.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Section 1: Burn summary ───────────────────────────── */}
          <Card>
            <CardContent className="flex flex-col gap-4 py-5">
              <div className="flex items-baseline gap-3 flex-wrap">
                <span className="text-3xl font-semibold">
                  {formatUsd(data.current.spent_to_date_usd)}
                </span>
                <span className="text-base text-muted-foreground">
                  / {formatUsd(data.current.credit_pool_usd)}
                </span>
                <Badge tone={RUNG_TONE[data.current.active_rung]}>
                  {RUNG_LABEL[data.current.active_rung]}
                </Badge>
              </div>

              <RungProgressBar
                pctUsed={data.current.current_pct_used}
                activeRung={data.current.active_rung}
              />

              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
                <span>burn rate: {formatUsd(data.current.burn_rate_usd_per_day)}/day</span>
                <span>
                  projected end-of-period:{" "}
                  {formatUsd(data.current.projected_end_of_period_usd)}
                </span>
                <span>{data.current.days_remaining} days remaining in period</span>
              </div>

              <ProjectionCallout
                projectedUsd={data.current.projected_end_of_period_usd}
                poolUsd={data.current.credit_pool_usd}
              />
            </CardContent>
          </Card>

          {/* ── Section 2: Active rung + downshift ───────────────── */}
          <Card>
            <CardContent className="flex flex-col gap-3 py-4">
              <div className="text-sm font-medium">Active rung &amp; downshift</div>
              <div className="flex items-center gap-3 flex-wrap">
                <Badge tone={RUNG_TONE[data.current.active_rung]}>
                  {RUNG_LABEL[data.current.active_rung]}
                </Badge>
                <Badge tone={MODEL_TIER_TONE[data.current.effective_model_tier]}>
                  model: {data.current.effective_model_tier}
                </Badge>
                {data.current.downshift_active && (
                  <Badge tone="warning">downshift active</Badge>
                )}
                {data.current.extra_usage_off ? (
                  <Badge tone="success">
                    <CheckCircle2 className="h-3 w-3 mr-1" />
                    extra-usage OFF (hard cap)
                  </Badge>
                ) : (
                  <Badge tone="destructive">
                    <AlertTriangle className="h-3 w-3 mr-1" />
                    extra-usage ON — risk of overflow
                  </Badge>
                )}
              </div>
              {data.current.downshift_reason && (
                <p className="text-xs text-muted-foreground">
                  {data.current.downshift_reason}
                </p>
              )}
            </CardContent>
          </Card>

          {/* ── Section 3: Rate-limit pulse ──────────────────────── */}
          <Card>
            <CardContent className="flex flex-col gap-3 py-4">
              <div className="text-sm font-medium">Rate-limit pulse</div>
              <div className="flex gap-6 flex-wrap">
                <RateLimitBar label="requests" window={data.rate_limit_pulse.requests} />
                <RateLimitBar label="tokens" window={data.rate_limit_pulse.tokens} />
              </div>
              <div className="text-xs text-muted-foreground">
                last seen {formatRelative(data.rate_limit_pulse.captured_at)} (
                {formatTimestamp(data.rate_limit_pulse.captured_at)})
              </div>
            </CardContent>
          </Card>

          {/* ── Section 4: Deferred + reconciliation (collapsible) ─ */}
          <section className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setHistoryExpanded((v) => !v)}
              className="flex items-center gap-2 text-sm font-medium w-fit"
              aria-expanded={historyExpanded ?? false}
            >
              {historyExpanded ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
              Deferred tickets &amp; reconciliation history
              <span className="text-xs text-muted-foreground">
                ({data.deferred_tickets.length} deferred,{" "}
                {data.reconciliation_history.length} reconciliations)
              </span>
            </button>
            {historyExpanded && (
              <div className="flex flex-col gap-4">
                <Card>
                  <CardContent className="py-3 flex flex-col gap-2">
                    <div className="text-sm font-medium">Deferred tickets</div>
                    <DeferredTicketsTable tickets={data.deferred_tickets} />
                  </CardContent>
                </Card>
                <Card>
                  <CardContent className="py-3 flex flex-col gap-2">
                    <div className="text-sm font-medium">Reconciliation history</div>
                    <ReconciliationTable entries={sortedReconciliation} />
                  </CardContent>
                </Card>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
