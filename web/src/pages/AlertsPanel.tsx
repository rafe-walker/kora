import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertOctagon,
  AlertTriangle,
  ArrowRight,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Cloud,
  DollarSign,
  HelpCircle,
  Inbox,
  Info,
  PauseCircle,
  PowerSquare,
  RefreshCw,
  Workflow,
} from "lucide-react";
import type { ComponentType } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import type {
  Alert,
  AlertCategory,
  AlertSeverity,
  AlertsResponse,
} from "@/lib/api";

const SEVERITY_TONE: Record<
  AlertSeverity,
  "destructive" | "warning" | "outline"
> = {
  critical: "destructive",
  warning: "warning",
  // "outline" reads as muted-blue against the card chrome — close
  // enough to "info" without introducing a new tone in the badge
  // palette.
  info: "outline",
};

const SEVERITY_LABEL: Record<AlertSeverity, string> = {
  critical: "critical",
  warning: "warning",
  info: "info",
};

// Severity sort key — critical first, then warning, then info.
const SEVERITY_ORDER: Record<AlertSeverity, number> = {
  critical: 0,
  warning: 1,
  info: 2,
};

// Map known categories to lucide icons that match the source panel's
// own icon convention. Unknown categories fall back to AlertTriangle
// (the open-enum on AlertCategory means backend can add new ones
// without an FE deploy).
const CATEGORY_ICON: Record<string, ComponentType<{ className?: string }>> = {
  cost_ladder: DollarSign,
  operational_state: PauseCircle,
  webhook_dead_letter: Inbox,
  agent_capability_denied: Workflow,
  reasoning_halted: Brain,
  service_unhealthy: Cloud,
  boot_gate_failure: PowerSquare,
};

function categoryIcon(category: AlertCategory): ComponentType<{ className?: string }> {
  return CATEGORY_ICON[category] ?? AlertTriangle;
}

function SeverityIcon({ severity }: { severity: AlertSeverity }) {
  switch (severity) {
    case "critical":
      return <AlertOctagon className="h-4 w-4 text-destructive" />;
    case "warning":
      return <AlertTriangle className="h-4 w-4 text-warning" />;
    case "info":
      return <Info className="h-4 w-4 text-primary" />;
  }
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
  if (absSec < 60) {
    const n = Math.round(absSec);
    return deltaMs < 0 ? `${n}s ago` : `in ${n}s`;
  }
  const absMin = absSec / 60;
  if (absMin < 60) {
    const n = Math.round(absMin);
    return deltaMs < 0 ? `${n}m ago` : `in ${n}m`;
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

interface AlertRowProps {
  alert: Alert;
  expanded: boolean;
  onToggle: () => void;
}

function AlertRow({ alert, expanded, onToggle }: AlertRowProps) {
  const Icon = categoryIcon(alert.category);
  return (
    <Card>
      <CardContent className="flex flex-col gap-2 py-3">
        <div className="flex items-start gap-3">
          <button
            type="button"
            onClick={onToggle}
            className="flex items-start gap-3 text-left flex-1 min-w-0"
            aria-expanded={expanded}
          >
            {expanded ? (
              <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0 mt-1" />
            ) : (
              <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0 mt-1" />
            )}
            <SeverityIcon severity={alert.severity} />
            <Icon className="h-4 w-4 text-muted-foreground shrink-0 mt-0.5" />
            <div className="flex flex-col gap-1 flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <Badge tone={SEVERITY_TONE[alert.severity]}>
                  {SEVERITY_LABEL[alert.severity]}
                </Badge>
                <span className="text-xs text-muted-foreground font-mono">
                  {alert.category}
                </span>
                {/* Title — plain text. JSX child expression; React's
                    default escaping defangs any HTML/markdown/script
                    that future source-panel state might quote. HARD
                    CONSTRAINT: NEVER switch to dangerouslySetInnerHTML
                    here. */}
                <span className="font-semibold">{alert.title}</span>
                <span className="text-muted-foreground flex items-center gap-1 ml-auto text-xs">
                  <Clock className="h-3 w-3" />
                  <span title={formatTimestamp(alert.first_seen_at)}>
                    {formatRelative(alert.first_seen_at)}
                  </span>
                </span>
              </div>
              {/* Detail — also plain text. Truncated visually via
                  line-clamp-1 when collapsed; full in expanded. */}
              <div
                className={`text-xs text-muted-foreground ${
                  expanded ? "whitespace-pre-wrap" : "truncate"
                }`}
              >
                {alert.detail}
              </div>
            </div>
          </button>
          <Link
            to={alert.source_panel_route}
            className="shrink-0"
            title={`Open ${alert.source_panel} panel`}
          >
            <Button size="sm" ghost>
              Open {alert.source_panel}
              <ArrowRight className="h-3 w-3" />
            </Button>
          </Link>
        </div>

        {expanded && (
          <div className="ml-10 flex flex-col gap-1.5 pt-2 border-t border-border text-xs">
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                first_seen_at
              </span>
              <span>{formatTimestamp(alert.first_seen_at)}</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                category
              </span>
              <code className="font-mono">{alert.category}</code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                source_panel
              </span>
              <code className="font-mono">{alert.source_panel}</code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                source_panel_route
              </span>
              <code className="font-mono">{alert.source_panel_route}</code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">id</span>
              <code className="font-mono">{alert.id}</code>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

interface SeverityGroupProps {
  severity: AlertSeverity;
  alerts: Alert[];
  expandedIds: Set<string>;
  onToggle: (id: string) => void;
}

function SeverityGroup({
  severity,
  alerts,
  expandedIds,
  onToggle,
}: SeverityGroupProps) {
  if (alerts.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 text-sm">
        <SeverityIcon severity={severity} />
        <span className="font-medium uppercase tracking-wide">
          {SEVERITY_LABEL[severity]}
        </span>
        <span className="text-xs text-muted-foreground">
          ({alerts.length})
        </span>
      </div>
      {alerts.map((a) => (
        <AlertRow
          key={a.id}
          alert={a}
          expanded={expandedIds.has(a.id)}
          onToggle={() => onToggle(a.id)}
        />
      ))}
    </div>
  );
}

export default function AlertsPanel() {
  const [data, setData] = useState<AlertsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const { toast, showToast } = useToast();

  const loadAlerts = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getCurrentAlerts()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load alerts: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadAlerts(false);
  }, [loadAlerts]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const groupedAlerts = useMemo(() => {
    if (!data) return { critical: [], warning: [], info: [] };
    const sorted = [...data.alerts].sort(
      (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
    );
    return {
      critical: sorted.filter((a) => a.severity === "critical"),
      warning: sorted.filter((a) => a.severity === "warning"),
      info: sorted.filter((a) => a.severity === "info"),
    };
  }, [data]);

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
          <H2>Operator Alerts</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Aggregated "needs attention" signals from across the
            12 panels.
          </p>
        </div>
        <Button
          size="sm"
          ghost
          disabled={refreshing}
          onClick={() => loadAlerts(true)}
        >
          <RefreshCw
            className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`}
          />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load alerts</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <AlertTriangle className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — real alert collection wires in via a
                deferred backend bucket
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample alerts (deliberately
                spanning all three severity tiers + four categories
                so the operator sees the severity sort + category
                icon mapping + click-through nav). Real-data flip
                requires source panels to expose their alert state
                to a central collector.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && data.alerts.length === 0 && (
        <Card className="border-success/30 bg-success/5">
          <CardContent className="py-8 flex flex-col items-center gap-2 text-sm">
            <CheckCircle2 className="h-8 w-8 text-success" />
            <span className="font-medium">No active alerts.</span>
            <span className="text-xs text-muted-foreground">
              Daemon healthy.
            </span>
          </CardContent>
        </Card>
      )}

      {data && data.alerts.length > 0 && (
        <>
          {/* ── Aggregate strip ────────────────────────────── */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
              <AlertTriangle className="h-4 w-4 text-primary" />
              <span className="font-medium">
                {data.total_active} active alert
                {data.total_active === 1 ? "" : "s"}
              </span>
              {data.by_severity.critical > 0 && (
                <span className="flex items-center gap-1.5 text-xs text-destructive">
                  <AlertOctagon className="h-3.5 w-3.5" />
                  {data.by_severity.critical} critical
                </span>
              )}
              {data.by_severity.warning > 0 && (
                <span className="flex items-center gap-1.5 text-xs text-warning">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  {data.by_severity.warning} warning
                </span>
              )}
              {data.by_severity.info > 0 && (
                <span className="flex items-center gap-1.5 text-xs text-primary">
                  <Info className="h-3.5 w-3.5" />
                  {data.by_severity.info} info
                </span>
              )}
              <span className="text-xs text-muted-foreground ml-auto">
                generated {formatRelative(data.generated_at)} (
                {formatTimestamp(data.generated_at)})
              </span>
            </CardContent>
          </Card>

          {/* ── Severity groups (critical → warning → info) ── */}
          <SeverityGroup
            severity="critical"
            alerts={groupedAlerts.critical}
            expandedIds={expandedIds}
            onToggle={toggleExpand}
          />
          <SeverityGroup
            severity="warning"
            alerts={groupedAlerts.warning}
            expandedIds={expandedIds}
            onToggle={toggleExpand}
          />
          <SeverityGroup
            severity="info"
            alerts={groupedAlerts.info}
            expandedIds={expandedIds}
            onToggle={toggleExpand}
          />
        </>
      )}

      {!data && !loadError && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            <HelpCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
            No alerts data.
          </CardContent>
        </Card>
      )}
    </div>
  );
}
