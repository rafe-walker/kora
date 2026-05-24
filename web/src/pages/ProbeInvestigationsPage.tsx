// Probe investigations xref viewer — KR-FE-PROBE-INVESTIGATION-VIEWER.
//
// Operator-facing lens onto the "Kora actually acts" loop wired by
// PR #163 (wake emitter) + PR #166 (wake consumer). Joins three
// data sources per wake event:
//
//   1. probe.wake_requested audit row (the wake itself)
//   2. reasoning.tool_called audit rows keyed by caller_session_id
//      == "probe:{probe}:{category}" (the investigation tool calls)
//   3. snapshot.service_health[probe] (current health → resolution)
//
// Empty state matters here: PR #166 just merged today (2026-05-23)
// so most installations will see zero wakes for a while. The empty
// state is a calm reassuring message, not an empty card list — see
// the spec's empty-state criterion.
//
// v1 deferred (surfaced to operator via the v1_notes banner so the
// roadmap is visible, not hidden):
//   * per-call cost_usd / model_used aren't durably recorded
//   * probe DMs bypass slack_dm_log.jsonl — no DM-sent confirmation
//   * resolution simplified to "currently healthy" (not time-windowed)

import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  HelpCircle,
  Info,
  RefreshCw,
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
import type {
  ProbeInvestigationItem,
  ProbeInvestigationsResponse,
  ProbeResolutionStatus,
} from "@/lib/api";

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

function toneCardBorderClass(tone: BadgeTone): string {
  if (tone === "destructive") return "border-destructive/40";
  if (tone === "warning") return "border-yellow-500/40";
  if (tone === "success") return "border-green-500/40";
  return "";
}

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
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
        hasn't picked it up yet, or the engine returned
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

function InvestigationCard({ item }: { item: ProbeInvestigationItem }) {
  const sev = severityVisual(item.severity);
  const res = resolutionVisual(item.resolution_status);

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
          <div className="text-xs text-muted-foreground">
            <span className="font-mono">
              caller_session_id: {item.caller_session_id}
            </span>
            <span className="mx-2">·</span>
            <span>
              envelope:{" "}
              {item.envelope_enabled
                ? `${item.envelope_fix_name} (ENABLED)`
                : "diagnose-only"}
            </span>
            <span className="mx-2">·</span>
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

function EmptyState({ window }: { window: Window }) {
  // The point of this view is to surface attention events. Zero
  // events == nothing demanding operator attention == healthy.
  // Don't render a sad "no data" — render reassurance.
  const windowLabel =
    window === "all"
      ? "in any window"
      : window === "7d"
        ? "in the last 7 days"
        : "in the last 24 hours";
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

function V1NotesBanner({
  notes,
}: {
  notes: ProbeInvestigationsResponse["v1_notes"];
}) {
  return (
    <Card className="border-blue-500/30 bg-blue-500/5">
      <CardContent className="p-3">
        <div className="flex items-start gap-2 text-xs">
          <Info className="h-4 w-4 text-blue-500 flex-shrink-0 mt-0.5" />
          <div className="space-y-1 text-muted-foreground">
            <div>
              <span className="font-medium text-foreground">v1 scope:</span>{" "}
              this panel joins audit rows + snapshot health. Per-call cost
              and DM-sent confirmation aren't yet recorded —{" "}
              <span className="italic">{notes.per_call_cost_usd}</span>;{" "}
              <span className="italic">{notes.dm_sent_confirmation}</span>.
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function ProbeInvestigationsPage() {
  usePanelView("ProbeInvestigationsPage");

  const [window, setWindow] = useState<Window>("24h");
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
        made to investigate them and current probe health for resolution
        status. Joined on{" "}
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
          <V1NotesBanner notes={data.v1_notes} />
          {data.items.length === 0 ? (
            <EmptyState window={window} />
          ) : (
            <div className="space-y-3">
              {data.items.map((item) => (
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
