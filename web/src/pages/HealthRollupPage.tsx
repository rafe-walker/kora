import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  HelpCircle,
  PauseCircle,
  RefreshCw,
  ShieldAlert,
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
import type {
  HealthRollupResponse,
  HealthStatus,
  Subsignal,
  SubsignalStatus,
} from "@/lib/api";

const HEALTH_TONE: Record<HealthStatus, "success" | "warning" | "destructive" | "outline"> = {
  healthy: "success",
  degraded: "warning",
  stopped: "outline",
  outage: "destructive",
};

const SUBSIGNAL_TONE: Record<SubsignalStatus, "success" | "warning" | "outline" | "destructive"> = {
  fresh: "success",
  stale: "warning",
  missing: "outline",
  degraded: "destructive",
};

// Render order matches R4.1 §9.7 reading order; not alphabetical.
const SUBSIGNAL_ORDER = [
  "last_successful_write",
  "claim_state",
  "credit_burn",
  "breaker_state",
  "auth_validity_window",
  "dispatch_reachable",
  "last_heartbeat",
  "escalation_watcher_liveness",
] as const;

const SUBSIGNAL_LABEL: Record<(typeof SUBSIGNAL_ORDER)[number], string> = {
  last_successful_write: "Last successful write",
  claim_state: "Claim state",
  credit_burn: "Credit burn",
  breaker_state: "Breaker state",
  auth_validity_window: "Auth validity window",
  dispatch_reachable: "Dispatch reachable",
  last_heartbeat: "Last heartbeat",
  escalation_watcher_liveness: "Escalation watcher liveness",
};

function HealthIcon({ status }: { status: HealthStatus }) {
  switch (status) {
    case "healthy":
      return <CheckCircle2 className="h-5 w-5 text-success" />;
    case "degraded":
      return <AlertTriangle className="h-5 w-5 text-warning" />;
    case "stopped":
      return <PauseCircle className="h-5 w-5 text-muted-foreground" />;
    case "outage":
      return <AlertOctagon className="h-5 w-5 text-destructive" />;
  }
}

function SubsignalStatusIcon({ status }: { status: SubsignalStatus }) {
  switch (status) {
    case "fresh":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "stale":
      return <AlertTriangle className="h-4 w-4 text-warning" />;
    case "missing":
      return <HelpCircle className="h-4 w-4 text-muted-foreground" />;
    case "degraded":
      return <XCircle className="h-4 w-4 text-destructive" />;
  }
}

function formatTimestamp(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatRelative(iso: string | undefined): string {
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

interface SubsignalDetailRow {
  label: string;
  value: string;
}

function detailRowsFor(s: Subsignal): SubsignalDetailRow[] {
  const rows: SubsignalDetailRow[] = [];

  if (s.value !== undefined) {
    rows.push({ label: "value", value: String(s.value) });
  }
  if (s.claim_id) {
    rows.push({ label: "claim_id", value: s.claim_id });
  }
  if (s.value_pct !== undefined) {
    rows.push({ label: "% used", value: `${s.value_pct.toFixed(1)}%` });
  }
  if (s.rung) {
    rows.push({ label: "rung", value: s.rung });
  }
  if (s.threshold_seconds !== undefined && s.elapsed_seconds !== undefined) {
    rows.push({
      label: "freshness",
      value: `${s.elapsed_seconds}s / ${s.threshold_seconds}s threshold`,
    });
  } else if (s.threshold_seconds !== undefined) {
    rows.push({
      label: "threshold",
      value: `${s.threshold_seconds}s`,
    });
  }
  if (s.threshold_pct !== undefined) {
    rows.push({ label: "threshold", value: `${s.threshold_pct}%` });
  }
  if (s.threshold_days !== undefined && s.days_remaining !== undefined) {
    rows.push({
      label: "expiry",
      value: `${s.days_remaining}d remaining (warn at ${s.threshold_days}d)`,
    });
  }
  if (s.expires_at) {
    rows.push({ label: "expires", value: formatTimestamp(s.expires_at) });
  }
  if (s.value_at) {
    rows.push({
      label: "last seen",
      value: `${formatRelative(s.value_at)} (${formatTimestamp(s.value_at)})`,
    });
  }

  return rows;
}

function SubsignalCard({ name, signal }: { name: string; signal: Subsignal }) {
  const label =
    SUBSIGNAL_LABEL[name as keyof typeof SUBSIGNAL_LABEL] ?? name;
  const borderTone =
    signal.status === "fresh"
      ? "border-success/20"
      : signal.status === "stale"
        ? "border-warning/40"
        : signal.status === "degraded"
          ? "border-destructive/40"
          : "border-border";

  const rows = detailRowsFor(signal);

  return (
    <Card className={borderTone}>
      <CardContent className="flex flex-col gap-2 py-4">
        <div className="flex items-center gap-2 flex-wrap">
          <SubsignalStatusIcon status={signal.status} />
          <span className="text-sm font-medium">{label}</span>
          <Badge tone={SUBSIGNAL_TONE[signal.status]}>{signal.status}</Badge>
        </div>
        <code className="text-[10px] text-muted-foreground">{name}</code>
        <dl className="flex flex-col gap-0.5 text-xs">
          {rows.map(({ label: l, value }) => (
            <div key={l} className="flex gap-2">
              <dt className="text-muted-foreground min-w-[80px]">{l}</dt>
              <dd className="break-all">{value}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

export default function HealthRollupPage() {
  const [data, setData] = useState<HealthRollupResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const { toast, showToast } = useToast();

  const loadHealth = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getHealthRollup()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load health rollup: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadHealth(false);
  }, [loadHealth]);

  if (data === null && !loadError) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  // R4.1 §9.7 P6 surface: red banner whenever the escalation watcher is
  // stale — control-plane escalation is unavailable and operator must
  // use manual L4.
  const escalationStale =
    data?.subsignals?.escalation_watcher_liveness?.status === "stale";

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      {/* ── P6 banner ─ red, top of page, only when escalation is stale ── */}
      {escalationStale && (
        <Card className="border-destructive/60 bg-destructive/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <ShieldAlert className="h-5 w-5 mt-0.5 text-destructive shrink-0" />
            <div>
              <div className="font-semibold text-destructive">
                Control escalation unavailable — use manual L4
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                escalation_watcher_liveness is stale (R4.1 §9.7 P6). Any
                automated escalation path is not running; if a level-4 stop
                is needed, issue it manually from the cockpit.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex items-start justify-between gap-4">
        <div>
          <H2>Health Rollup</H2>
          <p className="text-sm text-muted-foreground mt-1">
            R4.1 §9.7 health rollup across control plane, worker, and 8
            subsignals.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadHealth(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load health rollup</div>
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
                STUB DATA — health probe wire-in pending (KR-P2-L)
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Panel is a UI preview; values shown are sample data, not the
                real runtime probe.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Overall + control-plane + worker ─────────────────── */}
          <Card>
            <CardContent className="flex flex-col gap-4 py-5">
              <div className="flex items-center gap-3 flex-wrap">
                <HealthIcon status={data.overall} />
                <span className="text-2xl font-semibold uppercase tracking-wide">
                  {data.overall}
                </span>
                <Badge tone={HEALTH_TONE[data.overall]}>overall</Badge>
              </div>
              <div className="flex items-center gap-3 flex-wrap">
                <Badge tone={HEALTH_TONE[data.control_plane]}>
                  control plane: {data.control_plane}
                </Badge>
                <Badge tone={HEALTH_TONE[data.worker]}>
                  worker: {data.worker}
                </Badge>
                {data.stopped_reason && (
                  <span className="text-xs text-muted-foreground">
                    reason: {data.stopped_reason}
                  </span>
                )}
              </div>
              {(data.overall === "stopped" || data.overall === "outage") &&
                !data.stopped_reason && (
                  <p className="text-xs text-warning">
                    Overall state is {data.overall} but no stopped_reason was
                    reported. This is unusual — check the runtime probe.
                  </p>
                )}
            </CardContent>
          </Card>

          {/* ── Subsignals grid (R4.1 §9.7 reading order) ────────── */}
          <section className="flex flex-col gap-3">
            <div className="text-sm font-medium">Subsignals (R4.1 §9.7)</div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {SUBSIGNAL_ORDER.map((key) => {
                const signal = data.subsignals[key];
                if (!signal) {
                  return (
                    <Card key={key} className="border-border">
                      <CardContent className="flex flex-col gap-1 py-4">
                        <div className="flex items-center gap-2">
                          <HelpCircle className="h-4 w-4 text-muted-foreground" />
                          <span className="text-sm font-medium">
                            {SUBSIGNAL_LABEL[key]}
                          </span>
                          <Badge tone="outline">missing</Badge>
                        </div>
                        <code className="text-[10px] text-muted-foreground">{key}</code>
                        <p className="text-xs text-muted-foreground">
                          Subsignal not present in this health rollup.
                        </p>
                      </CardContent>
                    </Card>
                  );
                }
                return <SubsignalCard key={key} name={key} signal={signal} />;
              })}

              {/* Any extra subsignals not in the canonical order, defensively. */}
              {Object.keys(data.subsignals)
                .filter(
                  (k) =>
                    !(SUBSIGNAL_ORDER as readonly string[]).includes(k),
                )
                .map((k) => (
                  <SubsignalCard key={k} name={k} signal={data.subsignals[k]} />
                ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
