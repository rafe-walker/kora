// Probe investigations xref viewer — KR-FE-PROBE-INVESTIGATION-VIEWER-V2.
//
// V1 (PR #171) joined three sources per wake and surfaced a
// V1NotesBanner listing the three deferred fields. PR #184 closed
// those gaps BE-side (probe.investigation_completed seam +
// slack_dm_log.jsonl path for probe DMs). This V2 panel:
//
//   * Removes V1NotesBanner — the deferred fields are now live.
//   * Joins all 4 streams (KR-FE-PROBE-INVESTIGATION-VIEWER-V2
//     extended BE):
//
//       1. probe.wake_requested        (the wake itself)
//       2. reasoning.tool_called       (tool trace inside)
//       3. probe.investigation_completed (cost/model/dm_status/autofix)
//       4. slack_dm_log.jsonl           (DM-sent confirmation timestamp)
//
//   * Adds a dm_status chip-filter so the operator can triage
//     "failed_send" + "engine_unavailable_failed_send" first.
//   * Per-row card: cost (USD), model_used, DM-sent timestamp,
//     "🔧 fix attempted" badge when autofix_attempted=true.
//
// Drift-guard: PROBE_DM_STATUS_VALUES (api.ts) pinned against
// _DM_STATUS_VALUES in web_server.py.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  DollarSign,
  HelpCircle,
  Info,
  MailX,
  MessageSquare,
  RefreshCw,
  Search,
  Sparkles,
  Wrench,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import {
  PROBE_DM_STATUS_VALUES,
  type ProbeDmStatus,
  type ProbeInvestigationItem,
  type ProbeInvestigationsResponse,
  type ProbeResolutionStatus,
} from "@/lib/api";
import {
  FilterChips,
  formatDurationMs,
  formatRelative,
  formatTimestamp,
  type CategoryDef,
  type FilterValue,
} from "@/components/AuditPanelKit";

type Window = "24h" | "7d" | "all";

const WINDOW_LABELS: Record<Window, string> = {
  "24h": "24h",
  "7d": "7d",
  all: "All",
};

// nous-research/ui Badge tones — pin to the library's discriminated
// union (see node_modules/@nous-research/ui/dist/ui/components/badge.d.ts).
// "outline" is the neutral / non-toned chip.
type BadgeTone =
  | "default"
  | "destructive"
  | "outline"
  | "secondary"
  | "success"
  | "warning";

// KR-FE-PROBE-INVESTIGATION-VIEWER-V2 — dm_status chip categories.
// Ordering puts attention-demanding states first (failed_send +
// engine_unavailable_failed_send) per CC#1's #184 recommendation:
// the operator should see failures at the front of the filter row.
const DM_STATUS_CATEGORIES: readonly CategoryDef<ProbeDmStatus>[] = [
  {
    key: "failed_send",
    label: "Failed",
    tone: "destructive",
    Icon: MailX,
  },
  {
    key: "engine_unavailable_failed_send",
    label: "Engine unavail. + failed",
    tone: "destructive",
    Icon: AlertTriangle,
  },
  {
    key: "sent",
    label: "Sent",
    tone: "success",
    Icon: MessageSquare,
  },
  {
    key: "engine_unavailable_fallback",
    label: "Engine unavail. (fallback)",
    tone: "warning",
    Icon: AlertCircle,
  },
  {
    key: "unknown",
    label: "Unknown",
    tone: "outline",
    Icon: HelpCircle,
  },
];

// Map severity to icon + tone. Keys mirror the wake_consumer's
// _SEVERITY_EMOJI (kora_cli/probes/wake_consumer.py:430-434) so the
// FE colour-codes the same way the operator's Slack DM does.
function severityVisual(severity: string): {
  Icon: typeof AlertTriangle;
  tone: BadgeTone;
  label: string;
} {
  if (severity === "critical")
    return { Icon: AlertCircle, tone: "destructive", label: "critical" };
  if (severity === "warning")
    return { Icon: AlertTriangle, tone: "warning", label: "warning" };
  return { Icon: Info, tone: "outline", label: severity || "info" };
}

function resolutionVisual(status: ProbeResolutionStatus): {
  Icon: typeof CheckCircle2;
  tone: BadgeTone;
  label: string;
} {
  if (status === "resolved")
    return { Icon: CheckCircle2, tone: "success", label: "Resolved" };
  if (status === "active")
    return { Icon: XCircle, tone: "warning", label: "Active" };
  return { Icon: HelpCircle, tone: "outline", label: "Unknown" };
}

function dmStatusVisual(status: ProbeDmStatus | "unknown"): {
  tone: BadgeTone;
  label: string;
} {
  if (status === "sent") return { tone: "success", label: "DM sent" };
  if (status === "failed_send")
    return { tone: "destructive", label: "DM failed" };
  if (status === "engine_unavailable_fallback")
    return { tone: "warning", label: "Engine unavail. → fallback DM" };
  if (status === "engine_unavailable_failed_send")
    return { tone: "destructive", label: "Engine unavail. + DM failed" };
  return { tone: "outline", label: "DM unknown" };
}

function toneCardBorderClass(tone: BadgeTone): string {
  if (tone === "destructive") return "border-destructive/40";
  if (tone === "warning") return "border-yellow-500/40";
  if (tone === "success") return "border-green-500/40";
  return "";
}

function formatCostUSD(usd: number | null): string {
  if (usd === null) return "—";
  if (usd < 0.0001) return "<$0.0001";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

function InvestigationDetails({
  item,
}: {
  item: ProbeInvestigationItem;
}) {
  if (item.investigation === null) {
    return (
      <div className="text-xs italic text-muted-foreground">
        No reasoning calls recorded for this wake. Either the consumer
        hasn&apos;t picked it up yet, or the engine returned
        engine_unavailable / cost_ladder_halted before invoking any
        tools.
      </div>
    );
  }
  const inv = item.investigation;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Wrench className="h-3 w-3" />
          {inv.call_count} tool call{inv.call_count === 1 ? "" : "s"}
        </span>
        <span>·</span>
        <span>{formatDurationMs(inv.total_duration_ms)} total</span>
        {inv.any_errored && (
          <>
            <span>·</span>
            <span className="text-yellow-500 inline-flex items-center gap-1">
              <AlertTriangle className="h-3 w-3" />
              some calls errored
            </span>
          </>
        )}
      </div>
      <div className="flex flex-wrap gap-1">
        {inv.tool_calls.map((tc, idx) => {
          const errored =
            tc.exc_type !== undefined ||
            (tc.tool_status &&
              tc.tool_status.toLowerCase() !== "ok" &&
              tc.tool_status.toLowerCase() !== "success" &&
              tc.tool_status !== "");
          return (
            <Badge
              key={`${item.wake_event_id}-tc-${idx}`}
              tone={errored ? "warning" : "outline"}
              className="font-mono text-[10px]"
              title={`${tc.tool_name} · ${formatDurationMs(tc.tool_duration_ms)} · ${tc.tool_status || "ok"}${tc.exc_type ? ` · ${tc.exc_type}` : ""}`}
            >
              {tc.tool_name}
              <span className="ml-1 text-muted-foreground">
                {formatDurationMs(tc.tool_duration_ms)}
              </span>
            </Badge>
          );
        })}
      </div>
    </div>
  );
}

// KR-FE-PROBE-INVESTIGATION-VIEWER-V2 — investigation_completed
// summary band. Renders the per-investigation cost/model/dm/autofix
// fields the V1 banner used to apologize for missing. Only renders
// when the join populated investigation_completed for this wake;
// older wakes (pre-#184) silently get nothing here.
function InvestigationCompletedSummary({
  item,
}: {
  item: ProbeInvestigationItem;
}) {
  const ic = item.investigation_completed;
  if (ic === null) {
    return null;
  }
  const dm = dmStatusVisual(ic.dm_status);
  return (
    <div className="rounded border border-border bg-muted/20 p-2 space-y-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={dm.tone}>{dm.label}</Badge>
        {ic.autofix_attempted && (
          <Badge tone="warning" title="A probe_autofix attempt was made during this investigation">
            <Wrench className="h-3 w-3 mr-1 inline" />
            🔧 fix attempted
          </Badge>
        )}
        {ic.model_used && (
          <Badge tone="outline" className="font-mono">
            {ic.model_used}
          </Badge>
        )}
        {ic.total_cost_usd !== null && (
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            <DollarSign className="h-3 w-3" />
            {formatCostUSD(ic.total_cost_usd)}
          </span>
        )}
        {ic.investigation_duration_ms !== null && (
          <span className="text-muted-foreground">
            · {formatDurationMs(ic.investigation_duration_ms)}
          </span>
        )}
        {item.dm_entry && item.dm_entry.sent_at && (
          <span
            className="text-muted-foreground"
            title={formatTimestamp(item.dm_entry.sent_at)}
          >
            · DM {formatRelative(item.dm_entry.sent_at)}
          </span>
        )}
      </div>
      {ic.summary_text && (
        <div className="text-xs text-foreground/90 whitespace-pre-wrap">
          {ic.summary_text}
        </div>
      )}
      {ic.reasoning_error && (
        <div className="text-xs text-destructive font-mono">
          reasoning_error: {ic.reasoning_error}
        </div>
      )}
    </div>
  );
}

function InvestigationCard({ item }: { item: ProbeInvestigationItem }) {
  const sev = severityVisual(item.severity);
  const res = resolutionVisual(item.resolution_status);
  const autofix = item.investigation_completed?.autofix_attempted === true;

  return (
    <Card className={toneCardBorderClass(sev.tone)}>
      <CardContent className="p-4 space-y-3">
        <div className="flex items-start gap-3">
          <sev.Icon
            className={`h-5 w-5 flex-shrink-0 mt-0.5 ${
              sev.tone === "destructive"
                ? "text-destructive"
                : sev.tone === "warning"
                  ? "text-yellow-500"
                  : "text-muted-foreground"
            }`}
          />
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="font-mono text-sm font-medium">
                {item.probe_name}
              </span>
              <span className="text-xs text-muted-foreground">
                {formatTimestamp(item.wake_timestamp)}
              </span>
              <Badge tone={sev.tone}>{sev.label}</Badge>
              <Badge tone="outline">{item.issue_category}</Badge>
              {autofix && (
                <Badge tone="warning" className="text-[10px]">
                  <Wrench className="h-3 w-3 mr-1 inline" />
                  🔧 fix attempted
                </Badge>
              )}
              <span className="ml-auto" />
              <Badge tone={res.tone}>
                <res.Icon className="h-3 w-3 mr-1 inline" />
                {res.label}
              </Badge>
            </div>
            {item.title && (
              <div className="mt-1 text-sm font-medium">{item.title}</div>
            )}
            {item.detail && (
              <div className="mt-1 text-xs text-muted-foreground">
                {item.detail}
              </div>
            )}
          </div>
        </div>

        <div className="ml-8 space-y-2">
          <InvestigationCompletedSummary item={item} />
          <div className="text-xs text-muted-foreground flex items-center gap-2 flex-wrap">
            <span className="font-mono">
              caller_session_id: {item.caller_session_id}
            </span>
            {/* KR-FE-INVESTIGATION-DRILL-DOWN — drill into the unified
                per-session timeline (wake + autofix + completed + DM
                row, all in one chronological list with raw-JSON
                expansion per row). */}
            <Link
              to={`/investigations/${encodeURIComponent(item.caller_session_id)}`}
              className="inline-flex items-center gap-1 text-primary hover:underline"
              title="Drill into the full audit timeline for this investigation"
            >
              <Search className="h-3 w-3" />
              drill
            </Link>
            <span>·</span>
            <span>
              envelope:{" "}
              {item.envelope_enabled
                ? `${item.envelope_fix_name} (ENABLED)`
                : "diagnose-only"}
            </span>
            <span>·</span>
            <span>
              current probe health:{" "}
              <span className="font-mono">{item.current_probe_health}</span>
            </span>
          </div>
          <InvestigationDetails item={item} />
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyState({
  window,
  filterApplied,
  onReset,
}: {
  window: Window;
  filterApplied: boolean;
  onReset: () => void;
}) {
  // The point of this view is to surface attention events. Zero
  // events == nothing demanding operator attention == healthy.
  // Don't render a sad "no data" — render reassurance.
  const windowLabel =
    window === "all"
      ? "in any window"
      : window === "7d"
        ? "in the last 7 days"
        : "in the last 24 hours";
  if (filterApplied) {
    return (
      <Card className="border-border bg-muted/10">
        <CardContent className="p-6 flex flex-col items-center text-center gap-2 text-sm text-muted-foreground">
          <Info className="h-5 w-5" />
          No wakes match the current DM-status filter {windowLabel}.{" "}
          <button
            className="text-primary hover:underline"
            onClick={onReset}
          >
            Clear filter
          </button>
        </CardContent>
      </Card>
    );
  }
  return (
    <Card className="border-green-500/30 bg-green-500/5">
      <CardContent className="p-8 flex flex-col items-center text-center gap-3">
        <Sparkles className="h-8 w-8 text-green-500" />
        <H2 className="text-lg">No probe wakes {windowLabel}.</H2>
        <p className="text-sm text-muted-foreground max-w-md">
          Everything's healthy — none of the heartbeat probes have
          escalated an issue worth Kora investigating. This page will
          populate when probes detect something that needs attention.
        </p>
      </CardContent>
    </Card>
  );
}

function SummaryHeader({ data }: { data: ProbeInvestigationsResponse }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-muted-foreground" />
            <span className="font-medium">{data.total_count}</span>
            <span className="text-muted-foreground">wakes</span>
          </div>
          {data.total_count > 0 && (
            <>
              <span className="text-muted-foreground">·</span>
              <div className="flex items-center gap-1">
                <XCircle className="h-3.5 w-3.5 text-yellow-500" />
                <span className="font-medium">{data.active_count}</span>
                <span className="text-muted-foreground">active</span>
              </div>
              <div className="flex items-center gap-1">
                <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
                <span className="font-medium">{data.resolved_count}</span>
                <span className="text-muted-foreground">resolved</span>
              </div>
              {data.unknown_count > 0 && (
                <div className="flex items-center gap-1">
                  <HelpCircle className="h-3.5 w-3.5 text-muted-foreground" />
                  <span className="font-medium">{data.unknown_count}</span>
                  <span className="text-muted-foreground">unknown</span>
                </div>
              )}
            </>
          )}
          <span className="ml-auto text-xs text-muted-foreground">
            generated {formatTimestamp(data.generated_at)}
          </span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>Probe health right now:</span>
          {Object.entries(data.current_probe_health).map(([name, status]) => {
            const tone: BadgeTone =
              status === "healthy"
                ? "success"
                : status === "unhealthy" || status === "degraded"
                  ? "warning"
                  : "outline";
            return (
              <Badge key={name} tone={tone} className="font-mono">
                {name}: {status}
              </Badge>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

export default function ProbeInvestigationsPage() {
  usePanelView("ProbeInvestigationsPage");

  const [window, setWindow] = useState<Window>("24h");
  const [dmStatusFilter, setDmStatusFilter] = useState<
    FilterValue<ProbeDmStatus>
  >("all");
  const [data, setData] = useState<ProbeInvestigationsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (w: Window) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getProbeInvestigations({ window: w });
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(window);
  }, [load, window]);

  // Drift-guard grep — pins the constant import so the
  // test_probe_dm_status_drift_guard test catches a rename on
  // either side.
  void PROBE_DM_STATUS_VALUES;

  const filteredItems = useMemo(() => {
    if (data === null) return [];
    if (dmStatusFilter === "all") return data.items;
    return data.items.filter((it) => {
      const ic = it.investigation_completed;
      if (ic === null) return false;
      return ic.dm_status === dmStatusFilter;
    });
  }, [data, dmStatusFilter]);

  return (
    <div className="space-y-4 p-4 max-w-6xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2>Probe Investigations</H2>
        <div className="flex items-center gap-2">
          <div className="flex rounded-md border overflow-hidden">
            {(["24h", "7d", "all"] as Window[]).map((w) => (
              <button
                key={w}
                onClick={() => setWindow(w)}
                className={`px-3 py-1 text-xs font-mono transition-colors ${
                  window === w
                    ? "bg-primary text-primary-foreground"
                    : "hover:bg-accent"
                }`}
                aria-pressed={window === w}
              >
                {WINDOW_LABELS[w]}
              </button>
            ))}
          </div>
          <Button
            outlined
            size="sm"
            onClick={() => void load(window)}
            disabled={loading}
          >
            <RefreshCw
              className={`h-3 w-3 mr-1 ${loading ? "animate-spin" : ""}`}
            />
            Refresh
          </Button>
        </div>
      </div>

      <p className="text-sm text-muted-foreground">
        Heartbeat-probe wake events joined with the reasoning calls Kora
        made to investigate, the per-investigation completion summary
        (cost / model / DM status / autofix), and the operator&apos;s DM
        confirmation. All streams joined on{" "}
        <span className="font-mono">caller_session_id="probe:{"{probe}"}:{"{category}"}"</span>{" "}
        (set by{" "}
        <span className="font-mono">_derive_caller_session_id</span> in
        the reasoning engine).
      </p>

      {loading && data === null && (
        <div className="flex items-center justify-center p-8">
          <Spinner />
        </div>
      )}

      {error && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="p-4 flex items-start gap-2">
            <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0 mt-0.5" />
            <div className="text-sm">
              Failed to load probe investigations:{" "}
              <span className="font-mono">{error}</span>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <SummaryHeader data={data} />
          <Card>
            <CardContent className="p-3">
              <FilterChips
                categories={DM_STATUS_CATEGORIES}
                counts={data.by_dm_status_24h}
                current={dmStatusFilter}
                onChange={setDmStatusFilter}
                allLabel="All DM statuses"
              />
            </CardContent>
          </Card>
          {filteredItems.length === 0 ? (
            <EmptyState
              window={window}
              filterApplied={dmStatusFilter !== "all"}
              onReset={() => setDmStatusFilter("all")}
            />
          ) : (
            <div className="space-y-3">
              {filteredItems.map((item) => (
                <InvestigationCard
                  key={item.wake_event_id}
                  item={item}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
