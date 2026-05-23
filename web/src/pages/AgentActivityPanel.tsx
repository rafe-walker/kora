import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  HelpCircle,
  RefreshCw,
  ShieldOff,
  Timer,
  Workflow,
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
import {
  formatLatency,
  formatRelative,
  formatTimestamp,
  timestampAbsoluteUtc,
} from "@/lib/panelHelpers";
import {
  SHOW_MORE_DEFAULT_LIMIT,
  ShowMoreFooter,
} from "@/components/ShowMoreFooter";
import type {
  AgentActivityResponse,
  AgentCall,
  AgentCallStatus,
} from "@/lib/api";

const STATUS_ORDER: AgentCallStatus[] = [
  "ok",
  "capability_denied",
  "denied_prod_only",
  "tool_not_found",
  "handler_error",
  "timeout",
];

const STATUS_TONE: Record<
  AgentCallStatus,
  "success" | "warning" | "destructive" | "outline"
> = {
  ok: "success",
  capability_denied: "warning",
  denied_prod_only: "warning",
  tool_not_found: "outline",
  handler_error: "destructive",
  timeout: "destructive",
};

const STATUS_LABEL: Record<AgentCallStatus, string> = {
  ok: "ok",
  capability_denied: "capability denied",
  denied_prod_only: "denied · prod-only",
  tool_not_found: "tool not found",
  handler_error: "handler error",
  timeout: "timeout",
};

function StatusIcon({ status }: { status: AgentCallStatus }) {
  switch (status) {
    case "ok":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "capability_denied":
      return <ShieldOff className="h-4 w-4 text-warning" />;
    case "denied_prod_only":
      return <Ban className="h-4 w-4 text-warning" />;
    case "tool_not_found":
      return <HelpCircle className="h-4 w-4 text-muted-foreground" />;
    case "handler_error":
      return <AlertOctagon className="h-4 w-4 text-destructive" />;
    case "timeout":
      return <Timer className="h-4 w-4 text-destructive" />;
  }
}


// Visual "expensive call" bar — purely cosmetic, capped at 1000ms.
// Anything ≥ 1s gets a full bar to signal "this took real time".
function durationBarWidth(ms: number): string {
  const capped = Math.min(ms, 1000);
  return `${(capped / 1000) * 100}%`;
}

interface CallRowProps {
  call: AgentCall;
  expanded: boolean;
  onToggle: () => void;
}

function CallRow({ call, expanded, onToggle }: CallRowProps) {
  const isSlow = call.duration_ms >= 500;
  return (
    <Card>
      <CardContent className="flex flex-col gap-2 py-3">
        <button
          type="button"
          onClick={onToggle}
          className="flex items-center gap-3 text-left w-full"
          aria-expanded={expanded}
        >
          {expanded ? (
            <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0" />
          ) : (
            <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0" />
          )}
          <StatusIcon status={call.status} />
          <code className="font-mono text-sm">{call.tool_name}</code>
          <Badge tone="outline">{call.caller_actor_kind}</Badge>
          <Badge tone={STATUS_TONE[call.status]}>
            {STATUS_LABEL[call.status]}
          </Badge>
          <span className="text-xs text-muted-foreground ml-auto flex items-center gap-2">
            <Clock className="h-3 w-3" />
            {formatRelative(call.called_at)}
            <span
              className={`font-mono ${isSlow ? "text-warning" : ""}`}
              title={`${call.duration_ms} ms`}
            >
              {formatLatency(call.duration_ms)}
            </span>
          </span>
        </button>

        {expanded && (
          <div className="ml-7 flex flex-col gap-2 pt-2 border-t border-border text-xs">
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                called_at
              </span>
              <span title={timestampAbsoluteUtc(call.called_at)}>
                {formatTimestamp(call.called_at)}
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                duration
              </span>
              <span className="flex items-center gap-2 flex-1">
                <span className="font-mono">{call.duration_ms} ms</span>
                <span className="flex-1 max-w-[200px] h-1 bg-muted rounded-full overflow-hidden">
                  <span
                    className={`block h-full ${isSlow ? "bg-warning" : "bg-primary"}`}
                    style={{ width: durationBarWidth(call.duration_ms) }}
                  />
                </span>
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                result_summary
              </span>
              <span className="italic">{call.result_summary}</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">id</span>
              <code className="font-mono">{call.id}</code>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function AgentActivityPanel() {
  const [data, setData] = useState<AgentActivityResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [statusFilter, setStatusFilter] = useState<AgentCallStatus | "all">("all");
  const [callerFilter, setCallerFilter] = useState<string>("all");
  const [limit, setLimit] = useState<number>(SHOW_MORE_DEFAULT_LIMIT);
  const { toast, showToast } = useToast();

  const loadActivity = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getRecentAgentActivity(limit)
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load agent activity: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast, limit],
  );

  useEffect(() => {
    loadActivity(false);
  }, [loadActivity]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Derive caller options + status counts (from full unfiltered list).
  const { callerOptions, statusCounts } = useMemo(() => {
    if (!data) return { callerOptions: [] as string[], statusCounts: {} as Record<AgentCallStatus, number> };
    const callers = new Set<string>();
    const counts: Record<string, number> = {};
    for (const c of data.calls) {
      callers.add(c.caller_actor_kind);
      counts[c.status] = (counts[c.status] ?? 0) + 1;
    }
    return {
      callerOptions: Array.from(callers).sort(),
      statusCounts: counts as Record<AgentCallStatus, number>,
    };
  }, [data]);

  const filteredCalls = useMemo(() => {
    if (!data) return [];
    return data.calls.filter((c) => {
      if (statusFilter !== "all" && c.status !== statusFilter) return false;
      if (callerFilter !== "all" && c.caller_actor_kind !== callerFilter) return false;
      return true;
    });
  }, [data, statusFilter, callerFilter]);

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
          <H2>Agent Activity</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Recent agent-driven calls into Kora's /mcp endpoint.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadActivity(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load agent activity</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <Workflow className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — real data wires in via CC#3's KR-MCP-RUNTIME-SURFACE ST2
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample calls (deliberately spanning
                ok / capability_denied / denied_prod_only so operators see what
                failures look like). ST2 swaps the endpoint body to project
                from the live per-call ledger maintained by the /mcp handler.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Aggregate summary strip ─────────────────────────── */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
              <Workflow className="h-4 w-4 text-primary" />
              <span className="font-medium">
                {data.total_recent_24h} call
                {data.total_recent_24h === 1 ? "" : "s"} in last 24h
              </span>
              {Object.entries(data.by_caller_24h).map(([caller, count]) => (
                <span
                  key={caller}
                  className="flex items-center gap-1.5 text-xs"
                >
                  <Badge tone="outline">{caller}</Badge>
                  <span className="text-muted-foreground">{count}</span>
                </span>
              ))}
              <span className="text-xs text-muted-foreground ml-auto">
                generated {formatRelative(data.generated_at)} (
                {formatTimestamp(data.generated_at)})
              </span>
            </CardContent>
          </Card>

          {/* ── Filters ────────────────────────────────────────── */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-3 text-sm">
              <span className="text-xs text-muted-foreground uppercase tracking-wide">
                status
              </span>
              <div className="flex flex-wrap gap-1.5">
                <button
                  type="button"
                  onClick={() => setStatusFilter("all")}
                  className={`px-2 py-0.5 rounded-full text-xs border ${
                    statusFilter === "all"
                      ? "bg-primary text-primary-foreground border-primary"
                      : "border-border text-muted-foreground hover:text-foreground"
                  }`}
                >
                  all ({data.calls.length})
                </button>
                {STATUS_ORDER.filter((s) => statusCounts[s] > 0).map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setStatusFilter(s)}
                    className={`px-2 py-0.5 rounded-full text-xs border ${
                      statusFilter === s
                        ? "bg-primary text-primary-foreground border-primary"
                        : "border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {STATUS_LABEL[s]} ({statusCounts[s]})
                  </button>
                ))}
              </div>
              <span className="text-xs text-muted-foreground uppercase tracking-wide ml-2">
                caller
              </span>
              <select
                value={callerFilter}
                onChange={(e) => setCallerFilter(e.target.value)}
                className="text-xs bg-background border border-border rounded px-2 py-1"
              >
                <option value="all">all</option>
                {callerOptions.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </CardContent>
          </Card>

          {/* ── Timeline ───────────────────────────────────────── */}
          {/* Empty-state convergence (KR-FE-OPS-QUALITY-PASS):
              no recent agent activity on the /mcp surface is a
              healthy idle steady-state — use positive-reinforcement
              pattern from AlertsPanel. */}
          {data.calls.length === 0 ? (
            <Card className="border-success/30 bg-success/5">
              <CardContent className="py-8 text-center text-sm">
                <CheckCircle2 className="h-6 w-6 mx-auto mb-2 text-success" />
                <div className="font-medium">No agent activity.</div>
                <div className="text-xs text-muted-foreground mt-0.5">
                  /mcp endpoint healthy on port 9119.
                </div>
              </CardContent>
            </Card>
          ) : filteredCalls.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                <XCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
                No calls match the current filters.
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {filteredCalls.map((c) => (
                <CallRow
                  key={c.id}
                  call={c}
                  expanded={expandedIds.has(c.id)}
                  onToggle={() => toggleExpand(c.id)}
                />
              ))}
            </div>
          )}
          <ShowMoreFooter
            currentLimit={limit}
            totalShown={data.calls.length}
            onShowMore={setLimit}
            unitLabel="calls"
          />
        </>
      )}
    </div>
  );
}
