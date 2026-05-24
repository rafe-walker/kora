// Cost-telemetry visibility panel — KR-FE-COST-TELEMETRY-PANEL.
//
// Per the cost-economy thesis (feedback-opus-escalation-must-be-
// earned + cheap-substrate-first): the per-route burn / escalation
// rate / cache effectiveness has to be operator-visible OR the
// substrate work is invisible value.
//
// Three windows × three subsections:
//   * Windows: Lifetime / Rolling 24h / Monthly
//     - Lifetime always live ($cents) — process_lifetime is endpoint-only
//     - Rolling 24h + Monthly default to $0 snapshot read; Force-refresh
//       falls back to /api/cost_telemetry for fresh-as-of-now numbers
//   * Subsections per window:
//     - Cache effectiveness (horizontal stacked bar; read / write / uncached)
//     - Per-route breakdown table (calls, tokens, cost, escalation rate)
//     - Model breakdown (horizontal stacked bar by call count)
//
// Reserved-no-consumer routes (every KNOWN_ROUTES literal in
// kora_cli/telemetry/cost_telemetry.py:73-81 that has 0 calls AND
// has a downstream consumer bucket queued) render the cite-the-
// bucket "[Awaiting consumer]" marker so the panel doesn't look
// broken before the wiring lands.
//
// Chart-library choice: plain SVG + CSS-width divs. No new dep.
// The bars are aggregate-percentage breakdowns — a charting lib
// would be over-spec'd for that.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Info,
  RefreshCw,
  ZapOff,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import { formatRelative } from "@/lib/panelHelpers";
import type { CostTelemetryResponse, RouteCounters } from "@/lib/api";

// Known route literals from kora_cli/telemetry/cost_telemetry.py:73-81.
// Order is render-order: active first, then reserved-no-consumer,
// then unknown last. ROUTE_BUCKET_REF cites the queued bucket
// whose ship plugs this route into a real consumer — the panel
// surfaces the literal so operators know "this isn't broken, it's
// awaiting the wiring."
const KNOWN_ROUTES = [
  "slack_dm",
  "email_inbound",
  "email_outbound_compose",
  "mcp_tool",
  "alert_investigation",
  "probe_investigation",
  "tool_loop_iteration",
  "scheduled_task",
  "unknown",
] as const;

// Route disposition — drives the per-row rendering branch:
//   active    — calls_count > 0; render real numbers
//   awaiting  — known-reserved, calls_count == 0; show bucket ref
//   fallback  — "unknown" route (agent-side / unattributed)
const ROUTE_BUCKET_REF: Record<string, string> = {
  email_inbound: "KR-EMAIL-COST-BILL",
  email_outbound_compose: "KR-EMAIL-OUTBOUND-COST-BILL",
  mcp_tool: "KR-MCP-COST-BILL",
  alert_investigation: "KR-ALERT-INVESTIGATION-COST-BILL",
  probe_investigation: "KR-PROBE-INVESTIGATION-COST-BILL",
  tool_loop_iteration: "KR-TOOL-LOOP-COST-BILL",
  scheduled_task: "KR-SCHEDULED-TASK-COST-BILL",
};

type WindowKey = "process_lifetime" | "rolling_24h" | "monthly";

const WINDOW_LABEL: Record<WindowKey, string> = {
  process_lifetime: "Lifetime",
  rolling_24h: "Rolling 24h",
  monthly: "Monthly",
};

// Empty counters scaffold so every KNOWN_ROUTE has a row even when
// the telemetry singleton hasn't seen any calls yet.
const EMPTY_COUNTERS: RouteCounters = {
  calls_count: 0,
  input_tokens_total: 0,
  output_tokens_total: 0,
  cache_read_tokens_total: 0,
  cache_creation_tokens_total: 0,
  cost_estimate_usd_total: 0,
  escalation_count: 0,
  model_breakdown: {},
};

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `<$0.01`;
  return `$${n.toFixed(2)}`;
}

// Escalation rate color bands per feedback-opus-escalation-must-be-
// earned: 5-15% is the target band (Haiku-router earning Opus
// escalations at the right rate). Below 5% means classifier too
// tight (cheap-substrate not landing); above 30% means classifier
// too loose (cost-economy thesis broken).
function escalationToneClass(ratePct: number): string {
  if (ratePct >= 5 && ratePct <= 15) return "text-success";
  if (ratePct > 30) return "text-destructive";
  return "text-warning"; // <5 or 15-30
}

function escalationLabel(
  escalation_count: number,
  calls_count: number,
): { text: string; toneClass: string } {
  if (calls_count === 0) {
    return { text: "—", toneClass: "text-muted-foreground" };
  }
  // Pre-router state — no escalation logic shipped yet, so
  // escalation_count will always be 0 until KR-HAIKU-ROUTER lands
  // (CC#3 in-flight per spec §1). Render the pre-router note
  // instead of "0.0%" which would imply a (good!) 0% escalation
  // rate from a router that doesn't exist yet.
  if (escalation_count === 0) {
    return {
      text: "pre-router",
      toneClass: "text-muted-foreground italic",
    };
  }
  const pct = (escalation_count / calls_count) * 100;
  return {
    text: `${pct.toFixed(1)}%`,
    toneClass: escalationToneClass(pct),
  };
}

// ── Cache-effectiveness math ────────────────────────────────────
function sumWindow(counters: Record<string, RouteCounters>): {
  total_input: number;
  cache_read: number;
  cache_creation: number;
  uncached_input: number;
  total_output: number;
  total_cost_usd: number;
  total_calls: number;
  total_escalations: number;
  models: Record<string, number>;
} {
  let total_input = 0;
  let cache_read = 0;
  let cache_creation = 0;
  let total_output = 0;
  let total_cost_usd = 0;
  let total_calls = 0;
  let total_escalations = 0;
  const models: Record<string, number> = {};
  for (const counters_ of Object.values(counters)) {
    total_input += counters_.input_tokens_total;
    cache_read += counters_.cache_read_tokens_total;
    cache_creation += counters_.cache_creation_tokens_total;
    total_output += counters_.output_tokens_total;
    total_cost_usd += counters_.cost_estimate_usd_total;
    total_calls += counters_.calls_count;
    total_escalations += counters_.escalation_count;
    for (const [model, count] of Object.entries(
      counters_.model_breakdown ?? {},
    )) {
      models[model] = (models[model] ?? 0) + count;
    }
  }
  // uncached = total input - what was served from cache - what was
  // newly cached. Floor at 0 in case the writer's accounting drifts
  // (defensive; the counters should always add up exactly).
  const uncached_input = Math.max(
    0,
    total_input - cache_read - cache_creation,
  );
  return {
    total_input,
    cache_read,
    cache_creation,
    uncached_input,
    total_output,
    total_cost_usd,
    total_calls,
    total_escalations,
    models,
  };
}

// ── Cache effectiveness bar ─────────────────────────────────────
function CacheEffectivenessBar({
  counters,
}: {
  counters: Record<string, RouteCounters>;
}) {
  const s = sumWindow(counters);
  const denom = s.cache_read + s.cache_creation + s.uncached_input;
  if (denom === 0) {
    return (
      <div className="text-xs text-muted-foreground italic py-2">
        No input tokens recorded in this window yet.
      </div>
    );
  }
  const readPct = (s.cache_read / denom) * 100;
  const writePct = (s.cache_creation / denom) * 100;
  const uncachedPct = (s.uncached_input / denom) * 100;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex h-2 rounded overflow-hidden border border-border bg-muted">
        <div
          className="h-full bg-success"
          style={{ width: `${readPct}%` }}
          title={`cache reads: ${s.cache_read.toLocaleString()} tokens`}
        />
        <div
          className="h-full bg-warning"
          style={{ width: `${writePct}%` }}
          title={`cache writes: ${s.cache_creation.toLocaleString()} tokens`}
        />
        <div
          className="h-full bg-muted-foreground/40"
          style={{ width: `${uncachedPct}%` }}
          title={`uncached: ${s.uncached_input.toLocaleString()} tokens`}
        />
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <span className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-sm bg-success" />
          Cache reads: {readPct.toFixed(0)}% of input
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-sm bg-warning" />
          Cache writes: {writePct.toFixed(0)}%
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-sm bg-muted-foreground/40" />
          Uncached: {uncachedPct.toFixed(0)}%
        </span>
      </div>
    </div>
  );
}

// ── Per-route breakdown ─────────────────────────────────────────
function RouteBreakdownTable({
  counters,
}: {
  counters: Record<string, RouteCounters>;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-muted-foreground border-b border-border">
            <th className="text-left font-medium py-1.5 pr-3">Route</th>
            <th className="text-right font-medium py-1.5 pr-3">Calls</th>
            <th className="text-right font-medium py-1.5 pr-3">
              Tokens (in / out)
            </th>
            <th className="text-right font-medium py-1.5 pr-3">Cost</th>
            <th className="text-right font-medium py-1.5">
              Escalation rate
            </th>
          </tr>
        </thead>
        <tbody>
          {KNOWN_ROUTES.map((route) => {
            const c = counters[route] ?? EMPTY_COUNTERS;
            const isAwaiting =
              c.calls_count === 0 && route in ROUTE_BUCKET_REF;
            const isUnknown = route === "unknown";
            const esc = escalationLabel(c.escalation_count, c.calls_count);
            return (
              <tr
                key={route}
                className={`border-b border-border/40 ${
                  isAwaiting ? "text-muted-foreground" : ""
                }`}
              >
                <td className="py-1.5 pr-3 font-mono">
                  {route}
                  {isUnknown && (
                    <span
                      className="ml-2 text-[10px] italic text-muted-foreground"
                      title="Default fallback — agent-side / unattributed cost"
                    >
                      (agent-side fallback)
                    </span>
                  )}
                </td>
                {isAwaiting ? (
                  <td colSpan={4} className="py-1.5">
                    <span className="inline-flex items-center gap-1.5 italic">
                      <Info className="h-3 w-3" />
                      [Awaiting consumer]
                      <Badge tone="outline">
                        <span className="font-mono text-[10px]">
                          {ROUTE_BUCKET_REF[route]}
                        </span>
                      </Badge>
                    </span>
                  </td>
                ) : (
                  <>
                    <td className="py-1.5 pr-3 text-right font-mono">
                      {c.calls_count.toLocaleString()}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono">
                      {formatTokens(c.input_tokens_total)} /{" "}
                      {formatTokens(c.output_tokens_total)}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono">
                      {formatUsd(c.cost_estimate_usd_total)}
                    </td>
                    <td
                      className={`py-1.5 text-right font-mono ${esc.toneClass}`}
                    >
                      {esc.text}
                    </td>
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="mt-2 text-[10px] text-muted-foreground italic">
        Escalation rate target: 5–15% green · &lt;5% or 15–30% yellow · &gt;30% red
        (per feedback-opus-escalation-must-be-earned). "pre-router" until
        KR-HAIKU-ROUTER ships.
      </div>
    </div>
  );
}

// ── Model breakdown ─────────────────────────────────────────────
const MODEL_TIER_COLOR: Record<string, string> = {
  // Opus = top tier — surface in destructive-toned blue (high cost
  // attention; the operator should KNOW when Opus is being chosen)
  opus: "bg-blue-500",
  // Sonnet = mid tier — warning yellow so it's visible
  sonnet: "bg-yellow-400",
  // Haiku = cost-economy default — success green
  haiku: "bg-success",
  // short_circuit = no SDK call at all (cheapest path) — primary tone
  short_circuit: "bg-primary",
};

function tierColorForModel(model: string): string {
  const lower = model.toLowerCase();
  if (lower.includes("opus")) return MODEL_TIER_COLOR.opus;
  if (lower.includes("sonnet")) return MODEL_TIER_COLOR.sonnet;
  if (lower.includes("haiku")) return MODEL_TIER_COLOR.haiku;
  if (lower.includes("short_circuit") || lower.includes("short-circuit"))
    return MODEL_TIER_COLOR.short_circuit;
  return "bg-muted-foreground/50";
}

function ModelBreakdownBar({
  counters,
}: {
  counters: Record<string, RouteCounters>;
}) {
  const s = sumWindow(counters);
  const totalCalls = Object.values(s.models).reduce((sum, n) => sum + n, 0);
  if (totalCalls === 0) {
    return (
      <div className="text-xs text-muted-foreground italic py-2">
        No model attribution recorded in this window yet.
      </div>
    );
  }
  // Render in tier order so the chart reads cheap-to-expensive
  // left-to-right (matches operator's mental model of the cost ladder).
  const tierOrder = [
    "short_circuit",
    "haiku",
    "sonnet",
    "opus",
  ];
  const ordered = Object.entries(s.models).sort(([a], [b]) => {
    const ai = tierOrder.findIndex((t) => a.toLowerCase().includes(t));
    const bi = tierOrder.findIndex((t) => b.toLowerCase().includes(t));
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
  return (
    <div className="flex flex-col gap-2">
      <div className="flex h-2 rounded overflow-hidden border border-border bg-muted">
        {ordered.map(([model, count]) => {
          const pct = (count / totalCalls) * 100;
          return (
            <div
              key={model}
              className={`h-full ${tierColorForModel(model)}`}
              style={{ width: `${pct}%` }}
              title={`${model}: ${count} calls (${pct.toFixed(1)}%)`}
            />
          );
        })}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {ordered.map(([model, count]) => {
          const pct = (count / totalCalls) * 100;
          return (
            <span key={model} className="flex items-center gap-1.5">
              <span
                className={`w-2 h-2 rounded-sm ${tierColorForModel(model)}`}
              />
              <code className="font-mono text-[10px]">{model}</code>
              <span className="text-muted-foreground">
                {pct.toFixed(1)}%
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────
export default function CostTelemetryPage() {
  usePanelView("CostTelemetryPage");

  const [data, setData] = useState<CostTelemetryResponse | null>(null);
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [source, setSource] = useState<"snapshot" | "live" | "unavailable">(
    "live",
  );
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [windowKey, setWindowKey] = useState<WindowKey>("rolling_24h");
  const { toast, showToast } = useToast();

  // Snapshot-first load. Snapshot only carries rolling_24h + monthly
  // (process_lifetime is endpoint-only by design — keeps on-disk
  // snapshot bounded). Lifetime view always falls through to the
  // live endpoint regardless of snapshot freshness.
  const loadFromSnapshot = useCallback(async () => {
    try {
      const snap = await api.getSnapshot();
      if ("error" in snap) {
        return null;
      }
      const ct = snap.cost_telemetry;
      if (!ct) {
        return null;
      }
      return {
        snap,
        // process_lifetime intentionally empty from snapshot path;
        // operator hits the endpoint (Force-refresh OR pick Lifetime
        // tab) to populate it.
        data: {
          process_lifetime: {},
          rolling_24h: ct.rolling_24h,
          monthly: ct.monthly,
        } satisfies CostTelemetryResponse,
      };
    } catch {
      return null;
    }
  }, []);

  const loadFromEndpoint = useCallback(async () => {
    return await api.getCostTelemetry();
  }, []);

  const loadInitial = useCallback(async () => {
    setLoadError(null);
    const snap = await loadFromSnapshot();
    if (snap !== null) {
      setSource("snapshot");
      setSnapshotAt(snap.snap.computed_at);
      setData(snap.data);
      return;
    }
    // Snapshot path returned null: either v1 snapshot (no
    // cost_telemetry section yet), unavailable, OR threw. Fall back
    // to the live endpoint so the page still renders something
    // useful.
    try {
      const live = await loadFromEndpoint();
      setSource("unavailable");
      setSnapshotAt(null);
      setData(live);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setLoadError(msg);
      showToast(`Failed to load cost telemetry: ${msg}`, "error");
    }
  }, [loadFromSnapshot, loadFromEndpoint, showToast]);

  const forceLiveRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const live = await loadFromEndpoint();
      setSource("live");
      setSnapshotAt(null);
      setData(live);
      showToast("Cost telemetry refreshed (live)", "success");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setLoadError(msg);
      showToast(`Refresh failed: ${msg}`, "error");
    } finally {
      setRefreshing(false);
    }
  }, [loadFromEndpoint, showToast]);

  // Lifetime tab: if the user picks it from the snapshot path
  // (where process_lifetime is empty), auto-fall-through to the
  // endpoint so the tab content is meaningful. One-time fetch on
  // tab switch — operator clicks Force-refresh for re-reads.
  useEffect(() => {
    if (
      windowKey === "process_lifetime" &&
      data !== null &&
      Object.keys(data.process_lifetime).length === 0
    ) {
      void (async () => {
        try {
          const live = await loadFromEndpoint();
          setData(live);
          // Source stays "snapshot" for the other two windows; we
          // patched in just the lifetime data. Mode badge below
          // explains the mixed state via the per-window source label.
        } catch {
          // Silent — the Lifetime view will show the "no data" tile
          // with the existing error path handled below.
        }
      })();
    }
  }, [windowKey, data, loadFromEndpoint]);

  useEffect(() => {
    void loadInitial();
  }, [loadInitial]);

  const currentWindow = useMemo<Record<string, RouteCounters>>(() => {
    if (data === null) return {};
    return data[windowKey] ?? {};
  }, [data, windowKey]);

  const totalsHeadline = useMemo(() => sumWindow(currentWindow), [
    currentWindow,
  ]);

  // Window-source label for the badge below the tabs. Lifetime is
  // always live (endpoint-only); the other two windows reflect the
  // page-wide source state.
  function windowSourceLabel(): string {
    if (windowKey === "process_lifetime") return "live (endpoint)";
    if (source === "snapshot") return "$0 snapshot";
    if (source === "live") return "live (force-refreshed)";
    return "live (snapshot unavailable)";
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap">
          <H2>Cost Telemetry</H2>
          <span className="text-xs text-muted-foreground">
            Per-route burn, escalation rate, cache effectiveness — the
            cost-economy thesis made visible.
          </span>
        </div>
        <Button
          size="sm"
          ghost
          disabled={refreshing}
          onClick={() => void forceLiveRefresh()}
          title="Force live read of /api/cost_telemetry — bypasses the $0 snapshot path"
        >
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Force live refresh
        </Button>
      </div>

      {loadError && (
        <Card className="border-destructive/40">
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load cost telemetry</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data === null && !loadError && (
        <div className="flex items-center justify-center py-24">
          <Spinner className="text-2xl text-primary" />
        </div>
      )}

      {/* Window tabs */}
      {data !== null && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <div className="inline-flex rounded-md border border-border overflow-hidden">
              {(
                Object.keys(WINDOW_LABEL) as WindowKey[]
              ).map((key) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setWindowKey(key)}
                  className={`px-3 py-1.5 text-xs ${
                    windowKey === key
                      ? "bg-primary text-primary-foreground"
                      : "bg-card text-foreground hover:bg-muted/40"
                  }`}
                >
                  {WINDOW_LABEL[key]}
                </button>
              ))}
            </div>
            <span className="text-xs text-muted-foreground">
              Source: {windowSourceLabel()}
              {source === "snapshot" && snapshotAt && (
                <> · snapshot from {formatRelative(snapshotAt)}</>
              )}
            </span>
          </div>

          {/* Snapshot-v1 graceful fallback for non-lifetime windows */}
          {windowKey !== "process_lifetime" &&
            Object.keys(currentWindow).length === 0 && (
              <Card className="border-muted-foreground/20 bg-muted/20">
                <CardContent className="py-6 text-sm flex items-start gap-3">
                  <ZapOff className="h-4 w-4 mt-0.5 text-muted-foreground" />
                  <div>
                    <div className="font-medium">
                      Cost telemetry will appear here once the snapshot
                      refreshes (within 5 min).
                    </div>
                    <div className="text-xs text-muted-foreground mt-1">
                      The snapshot you loaded is from before the telemetry
                      writer started. Wait for the next refresh OR click
                      "Force live refresh" above.
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

          {/* Three subsections per window */}
          {Object.keys(currentWindow).length > 0 && (
            <>
              <Card>
                <CardContent className="py-4 flex flex-col gap-3">
                  <div className="flex items-baseline justify-between flex-wrap gap-2">
                    <div className="text-sm font-medium">
                      Cache effectiveness
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Total spend in window:{" "}
                      <span className="font-mono text-foreground">
                        {formatUsd(totalsHeadline.total_cost_usd)}
                      </span>{" "}
                      across{" "}
                      <span className="font-mono text-foreground">
                        {totalsHeadline.total_calls.toLocaleString()}
                      </span>{" "}
                      call{totalsHeadline.total_calls === 1 ? "" : "s"}
                    </div>
                  </div>
                  <CacheEffectivenessBar counters={currentWindow} />
                </CardContent>
              </Card>

              <Card>
                <CardContent className="py-4 flex flex-col gap-3">
                  <div className="text-sm font-medium">
                    Per-route breakdown
                  </div>
                  <RouteBreakdownTable counters={currentWindow} />
                </CardContent>
              </Card>

              <Card>
                <CardContent className="py-4 flex flex-col gap-3">
                  <div className="text-sm font-medium">Model breakdown</div>
                  <ModelBreakdownBar counters={currentWindow} />
                </CardContent>
              </Card>
            </>
          )}
        </>
      )}
    </div>
  );
}
