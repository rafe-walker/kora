// Probe-autofix audit log lens — KR-FE-AUTOFIX-LOG-PANEL.
//
// 3rd consumer of AuditPanelKit (after #180 EmailIntent + #183
// OutboundEmail were retrofitted in this same megabucket).
// Surfaces tool.probe_autofix_attempted (PR #182) — the
// kora__attempt_probe_autofix reasoning-loop tool that lets
// Kora restart Fly machines / similar bounded fixes after a
// probe detects unhealthy state.
//
// Per-row card shows: probe + action + target + status chip +
// reason_from_reasoning snippet + before→after state transition
// + executor duration. Operator-trust surface: "Kora did
// something — here's why + what changed."

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronsUp,
  RefreshCw,
  ServerCrash,
  Wrench,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { useActiveTenant } from "@/hooks/useActiveTenant";
import { api } from "@/lib/api";
import {
  PROBE_AUTOFIX_STATUS_VALUES,
  type ProbeAutofixEvent,
  type ProbeAutofixEventsResponse,
  type ProbeAutofixStatus,
} from "@/lib/api";
import {
  EmptyFilteredMessage,
  FilterChips,
  Sparkline,
  SummaryChips,
  formatDurationMs,
  formatRelative,
  formatTimestamp,
  truncate,
  type CategoryDef,
  type FilterValue,
} from "@/components/AuditPanelKit";

const PROBE_AUTOFIX_CATEGORIES: readonly CategoryDef<ProbeAutofixStatus>[] = [
  {
    key: "attempted",
    label: "Attempted",
    tone: "success",
    Icon: CheckCircle2,
  },
  { key: "rejected", label: "Rejected", tone: "warning", Icon: Ban },
  {
    key: "execution_failed",
    label: "Execution-failed",
    tone: "destructive",
    Icon: ServerCrash,
  },
];

const PROBE_AUTOFIX_CATEGORY_MAP: Record<
  ProbeAutofixStatus,
  CategoryDef<ProbeAutofixStatus>
> = {
  attempted: PROBE_AUTOFIX_CATEGORIES[0],
  rejected: PROBE_AUTOFIX_CATEGORIES[1],
  execution_failed: PROBE_AUTOFIX_CATEGORIES[2],
  unknown: {
    key: "attempted" as ProbeAutofixStatus,
    label: "Unknown",
    tone: "outline",
    Icon: AlertTriangle,
  },
};

// ----- Per-row card -----

function EventCard({ event }: { event: ProbeAutofixEvent }) {
  const v = PROBE_AUTOFIX_CATEGORY_MAP[event.status];
  const Icon = v.Icon;
  const actionLabel =
    event.action_canonical || event.action_taken || event.action;
  return (
    <Card>
      <CardContent className="p-3 flex items-start gap-3">
        <Wrench className="h-4 w-4 mt-0.5 text-muted-foreground flex-shrink-0" />
        <div className="flex-1 min-w-0 flex flex-col gap-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-xs text-muted-foreground"
              title={formatTimestamp(event.emitted_at)}
            >
              {formatRelative(event.emitted_at)}
            </span>
            <span className="text-sm font-medium font-mono">
              {actionLabel}
            </span>
            <span className="text-xs text-muted-foreground">on</span>
            <span className="text-sm font-mono">
              {event.probe}
              <span className="text-muted-foreground">/</span>
              <span className="text-muted-foreground">{event.target_id}</span>
            </span>
            <Badge tone={v.tone}>
              <Icon className="h-3 w-3 mr-1 inline" />
              {v.label}
            </Badge>
          </div>

          {/* before → after state transition (attempted + sometimes
              execution_failed branches). */}
          {(event.before_state_label || event.after_state_label) && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>state:</span>
              <span className="font-mono">
                {event.before_state_label ?? "?"}
              </span>
              <span>→</span>
              <span className="font-mono">
                {event.after_state_label ?? "?"}
              </span>
              {event.executor_duration_ms !== undefined && (
                <>
                  <span>·</span>
                  <span>
                    executor: {formatDurationMs(event.executor_duration_ms)}
                  </span>
                </>
              )}
            </div>
          )}

          {/* reason_from_reasoning — operator-decision triage */}
          {event.reason_from_reasoning && (
            <div className="text-xs text-muted-foreground italic">
              <span className="not-italic text-foreground">Reason: </span>
              {truncate(event.reason_from_reasoning, 280)}
            </div>
          )}

          {/* Per-status detail row. */}
          {event.status === "rejected" && (
            <div className="flex flex-col gap-1 text-xs">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-yellow-500 inline-flex items-center gap-1">
                  <ChevronsUp className="h-3 w-3" />
                  reason:{" "}
                  <span className="font-mono">
                    {event.rejection_reason || "(unknown)"}
                  </span>
                </span>
              </div>
              {event.rejection_detail && (
                <div className="font-mono text-[10px] text-muted-foreground break-words">
                  detail: {event.rejection_detail}
                </div>
              )}
            </div>
          )}
          {event.status === "execution_failed" && event.error && (
            <div className="text-xs text-destructive font-mono break-words">
              {event.error}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ----- Page -----

export default function AutofixLogPage() {
  usePanelView("AutofixLogPage");

  const { activeTenant, isAllTenants } = useActiveTenant();
  const tenantForRead = isAllTenants ? undefined : activeTenant;

  const [data, setData] = useState<ProbeAutofixEventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterValue<ProbeAutofixStatus>>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getProbeAutofixRecent({ tenantId: tenantForRead });
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [tenantForRead]);

  useEffect(() => {
    void load();
  }, [load]);

  const filteredEvents = useMemo(() => {
    if (data === null) return [];
    if (filter === "all") return data.events;
    return data.events.filter((e) => e.status === filter);
  }, [data, filter]);

  void PROBE_AUTOFIX_STATUS_VALUES;

  return (
    <div className="space-y-4 p-4 max-w-6xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2>Probe Autofix Log</H2>
        <Button outlined size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw
            className={`h-3 w-3 mr-1 ${loading ? "animate-spin" : ""}`}
          />
          Refresh
        </Button>
      </div>

      <p className="text-sm text-muted-foreground">
        Audit stream of bounded autofixes Kora attempted via the{" "}
        <span className="font-mono">kora__attempt_probe_autofix</span> tool
        (PR #182). Each entry shows the action + target + reason +
        before→after state transition. The{" "}
        <span className="font-mono">rejected</span> filter shows where
        envelope-gating prevented an attempt (operator-tuning surface);{" "}
        <span className="font-mono">execution_failed</span> is the triage
        surface.
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
              Failed to load probe-autofix events:{" "}
              <span className="font-mono">{error}</span>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <Card>
            <CardContent className="p-4 flex flex-col gap-3">
              <SummaryChips
                categories={PROBE_AUTOFIX_CATEGORIES}
                counts={data.by_status_24h}
                total={data.total_recent_24h}
                totalNoun="autofix events"
              />
              <Sparkline
                points={data.daily_attempted_14d}
                totalSuffix="attempted · 14d"
                ariaLabel={`Daily 'attempted' counts over the last ${data.daily_attempted_14d.length} days`}
              />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-3">
              <FilterChips
                categories={PROBE_AUTOFIX_CATEGORIES}
                counts={data.by_status_24h}
                current={filter}
                onChange={setFilter}
              />
            </CardContent>
          </Card>

          {filteredEvents.length === 0 ? (
            <EmptyFilteredMessage
              isAllFilter={filter === "all"}
              titleAll="No probe-autofix attempts recorded yet"
              titleFiltered={`No "${PROBE_AUTOFIX_CATEGORY_MAP[filter as ProbeAutofixStatus]?.label}" events match this filter in the current window`}
              bodyAll="The tool.probe_autofix_attempted audit stream is empty for now. This page will populate when Kora attempts a bounded autofix in response to a probe-detected issue."
              onResetToAll={() => setFilter("all")}
            />
          ) : (
            <div className="space-y-2">
              {filteredEvents.map((event) => (
                <EventCard key={event.id} event={event} />
              ))}
              {filteredEvents.length < data.events.length && (
                <div className="text-xs text-muted-foreground italic text-center py-2">
                  Showing {filteredEvents.length} of {data.events.length}{" "}
                  events; clear filter to see all.
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
