// Alert investigations xref viewer — KR-FE-ALERT-INVESTIGATIONS-VIEWER
// (forward-compat for CC#1 #420).
//
// Mirror of ProbeInvestigationsPage but for alerts: joins
// alert.wake_requested + alert.investigation_completed audits with
// the slack_dm_log.jsonl entry by caller_session_id
// ``alert:{category}:{severity}``. Today none of these rows exist
// (CC#1 #420 ships the emitter); the page renders an empty state
// cleanly until then.
//
// Each card has a "drill" link to the InvestigationDrillDown page
// (introduced in #194) — same UX as the probe variant.
//
// Drift-guard: PROBE_DM_STATUS_VALUES (api.ts) ↔ _DM_STATUS_VALUES
// (web_server.py) is reused via _ALERT_DM_STATUS_VALUES alias so
// alert + probe share the same source-of-truth tuple.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  Bell,
  BellRing,
  DollarSign,
  HelpCircle,
  Info,
  MailX,
  MessageSquare,
  RefreshCw,
  Search,
  Sparkles,
  Zap,
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
  type AlertInvestigationItem,
  type AlertInvestigationsResponse,
  type ProbeDmStatus,
} from "@/lib/api";
import {
  FilterChips,
  formatDurationMs,
  formatRelative,
  formatTimestamp,
  type BadgeTone,
  type CategoryDef,
  type FilterValue,
} from "@/components/AuditPanelKit";

type Window = "24h" | "7d" | "all";

const WINDOW_LABELS: Record<Window, string> = {
  "24h": "24h",
  "7d": "7d",
  all: "All",
};

// Reuses the probe dm_status enum since both wake consumers route
// through the same DM-dispatch path (per the BE _ALERT_DM_STATUS_
// VALUES alias).
const DM_STATUS_CATEGORIES: readonly CategoryDef<ProbeDmStatus>[] = [
  { key: "failed_send", label: "Failed", tone: "destructive", Icon: MailX },
  {
    key: "engine_unavailable_failed_send",
    label: "Engine unavail. + failed",
    tone: "destructive",
    Icon: AlertTriangle,
  },
  { key: "sent", label: "Sent", tone: "success", Icon: MessageSquare },
  {
    key: "engine_unavailable_fallback",
    label: "Engine unavail. (fallback)",
    tone: "warning",
    Icon: AlertCircle,
  },
  { key: "unknown", label: "Unknown", tone: "outline", Icon: HelpCircle },
];

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

function AlertCompletedSummary({ item }: { item: AlertInvestigationItem }) {
  const ic = item.investigation_completed;
  if (ic === null) return null;
  const dm = dmStatusVisual(ic.dm_status);
  return (
    <div className="rounded border border-border bg-muted/20 p-2 space-y-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={dm.tone}>{dm.label}</Badge>
        {ic.autoaction_attempted && (
          <Badge
            tone="warning"
            title="An alert-driven autoaction was attempted during this investigation (forward-compat surface — v1 emits false)"
          >
            <Zap className="h-3 w-3 mr-1 inline" />
            🚨 auto-action attempted
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

function AlertInvestigationCard({ item }: { item: AlertInvestigationItem }) {
  const sev = severityVisual(item.severity);
  const autoaction = item.investigation_completed?.autoaction_attempted === true;

  return (
    <Card className={toneCardBorderClass(sev.tone)}>
      <CardContent className="p-4 space-y-3">
        <div className="flex items-start gap-3">
          <BellRing
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
                {item.alert_category}
              </span>
              <span className="text-xs text-muted-foreground">
                {formatTimestamp(item.wake_timestamp)}
              </span>
              <Badge tone={sev.tone}>{sev.label}</Badge>
              {autoaction && (
                <Badge tone="warning" className="text-[10px]">
                  <Zap className="h-3 w-3 mr-1 inline" />
                  🚨 auto-action
                </Badge>
              )}
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
          <AlertCompletedSummary item={item} />
          <div className="text-xs text-muted-foreground flex items-center gap-2 flex-wrap">
            <span className="font-mono">
              caller_session_id: {item.caller_session_id}
            </span>
            <Link
              to={`/investigations/${encodeURIComponent(item.caller_session_id)}`}
              className="inline-flex items-center gap-1 text-primary hover:underline"
              title="Drill into the full audit timeline for this alert investigation"
            >
              <Search className="h-3 w-3" />
              drill
            </Link>
          </div>
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
          No alert investigations match the current DM-status filter{" "}
          {windowLabel}.{" "}
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
        <H2 className="text-lg">
          Kora hasn&apos;t been woken by alerts {windowLabel}
        </H2>
        <p className="text-sm text-muted-foreground max-w-md">
          Either no alert escalated the wake threshold, or the alert wake
          consumer hasn&apos;t shipped yet (forward-compat for CC#1
          #420). This page will populate when alert.wake_requested rows
          start landing in the audit log.
        </p>
      </CardContent>
    </Card>
  );
}

function SummaryHeader({ data }: { data: AlertInvestigationsResponse }) {
  const totalCritical = data.by_severity_24h.critical ?? 0;
  const totalWarning = data.by_severity_24h.warning ?? 0;
  const totalInfo = data.by_severity_24h.info ?? 0;
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <div className="flex items-center gap-2">
            <Bell className="h-4 w-4 text-muted-foreground" />
            <span className="font-medium">{data.total_count}</span>
            <span className="text-muted-foreground">alert wakes</span>
          </div>
          {data.total_count > 0 && (
            <>
              <span className="text-muted-foreground">·</span>
              <div className="flex items-center gap-1">
                <AlertCircle className="h-3.5 w-3.5 text-destructive" />
                <span className="font-medium">{totalCritical}</span>
                <span className="text-muted-foreground">critical</span>
              </div>
              <div className="flex items-center gap-1">
                <AlertTriangle className="h-3.5 w-3.5 text-yellow-500" />
                <span className="font-medium">{totalWarning}</span>
                <span className="text-muted-foreground">warning</span>
              </div>
              {totalInfo > 0 && (
                <div className="flex items-center gap-1">
                  <Info className="h-3.5 w-3.5 text-muted-foreground" />
                  <span className="font-medium">{totalInfo}</span>
                  <span className="text-muted-foreground">info</span>
                </div>
              )}
            </>
          )}
          <span className="ml-auto text-xs text-muted-foreground">
            generated {formatTimestamp(data.generated_at)}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

export default function AlertInvestigationsPage() {
  usePanelView("AlertInvestigationsPage");

  const [window, setWindow] = useState<Window>("24h");
  const [dmStatusFilter, setDmStatusFilter] = useState<FilterValue<ProbeDmStatus>>("all");
  const [data, setData] = useState<AlertInvestigationsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (w: Window) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getAlertInvestigations({ window: w });
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

  // Drift-guard grep — pins the constant import so
  // test_alert_dm_status_drift_guard catches a rename on either
  // side. Alert + probe share the same source-of-truth tuple
  // (PROBE_DM_STATUS_VALUES); the alert page imports it directly.
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
        <H2 className="flex items-center gap-2">
          <BellRing className="h-5 w-5" />
          Alert Investigations
        </H2>
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
        Alert-wake events joined with the per-investigation summary
        (cost / model / DM status / autoaction) + operator DM
        confirmation. Joined on{" "}
        <span className="font-mono">caller_session_id="alert:{"{category}"}:{"{severity}"}"</span>.
        Forward-compat surface for CC#1&apos;s #420 — once the alert
        wake consumer ships, the seam rows populate automatically
        and this page lights up.
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
              Failed to load alert investigations:{" "}
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
                <AlertInvestigationCard
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

