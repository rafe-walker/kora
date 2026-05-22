import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Cloud,
  HelpCircle,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import type {
  HeartbeatService,
  HeartbeatServicesResponse,
  HeartbeatStatus,
} from "@/lib/api";

const STATUS_TONE: Record<HeartbeatStatus, "success" | "warning" | "destructive"> = {
  healthy: "success",
  degraded: "warning",
  unhealthy: "destructive",
};

function StatusIcon({ status }: { status: HeartbeatStatus }) {
  switch (status) {
    case "healthy":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "degraded":
      return <AlertTriangle className="h-4 w-4 text-warning" />;
    case "unhealthy":
      return <AlertOctagon className="h-4 w-4 text-destructive" />;
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

function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    // Show small floats with reasonable precision; ints as-is.
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

interface ServiceRowProps {
  service: HeartbeatService;
  expanded: boolean;
  onToggle: () => void;
}

function ServiceRow({ service, expanded, onToggle }: ServiceRowProps) {
  const detailEntries = Object.entries(service.details);
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
          <StatusIcon status={service.status} />
          <span className="font-medium uppercase tracking-wide">
            {service.name}
          </span>
          <Badge tone={STATUS_TONE[service.status]}>{service.status}</Badge>
          <span className="text-xs text-muted-foreground ml-auto">
            {service.latency_ms} ms · last checked{" "}
            {formatRelative(service.last_check_at)}
          </span>
        </button>

        {expanded && (
          <div className="ml-7 flex flex-col gap-1.5 pt-2 border-t border-border">
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 text-xs">
              {detailEntries.length === 0 ? (
                <div className="text-muted-foreground col-span-full">
                  no details surfaced
                </div>
              ) : (
                detailEntries.map(([key, value]) => (
                  <div key={key} className="flex gap-2">
                    <dt className="text-muted-foreground min-w-[140px]">
                      {key}
                    </dt>
                    <dd>{formatDetailValue(value)}</dd>
                  </div>
                ))
              )}
            </dl>
            <div className="text-xs text-muted-foreground pt-1">
              <code>last_check_at</code>: {formatTimestamp(service.last_check_at)}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function HeartbeatPanel() {
  const [data, setData] = useState<HeartbeatServicesResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedNames, setExpandedNames] = useState<Set<string>>(new Set());
  const { toast, showToast } = useToast();

  const loadHeartbeat = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getHeartbeatServices()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load heartbeat: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadHeartbeat(false);
  }, [loadHeartbeat]);

  const toggleExpand = useCallback((name: string) => {
    setExpandedNames((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  if (data === null && !loadError) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  // Aggregate counts for the header summary strip.
  const counts = data
    ? {
        healthy: data.services.filter((s) => s.status === "healthy").length,
        degraded: data.services.filter((s) => s.status === "degraded").length,
        unhealthy: data.services.filter((s) => s.status === "unhealthy").length,
      }
    : null;

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-start justify-between gap-4">
        <div>
          <H2>Backend Service Heartbeat</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Health of the SaaS backends Joshua's work depends on.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadHeartbeat(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load heartbeat</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <Activity className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — real data wires in via KR-FEAT-HEARTBEAT
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample data, not live polling.
                The Python heartbeat module that talks to each service's API
                lands as a follow-on bucket after KR-D-DAEMON ST2.
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
              <Cloud className="h-4 w-4 text-primary" />
              <span className="font-medium">
                {data.services.length} service{data.services.length === 1 ? "" : "s"}
              </span>
              {counts && (
                <>
                  <span className="flex items-center gap-1.5 text-xs">
                    <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                    {counts.healthy} healthy
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                    {counts.degraded} degraded
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <AlertOctagon className="h-3.5 w-3.5 text-destructive" />
                    {counts.unhealthy} unhealthy
                  </span>
                </>
              )}
              <span className="text-xs text-muted-foreground ml-auto">
                generated {formatRelative(data.generated_at)} (
                {formatTimestamp(data.generated_at)})
              </span>
            </CardContent>
          </Card>

          {/* ── Services list ──────────────────────────────────── */}
          {data.services.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                <HelpCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
                No services configured.
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {data.services.map((s) => (
                <ServiceRow
                  key={s.name}
                  service={s}
                  expanded={expandedNames.has(s.name)}
                  onToggle={() => toggleExpand(s.name)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
