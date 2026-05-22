import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertOctagon,
  AlertTriangle,
  Archive,
  ArrowRight,
  BookOpenCheck,
  CheckCircle2,
  DollarSign,
  Heart,
  HeartPulse,
  Hourglass,
  Info,
  OctagonAlert,
  PauseCircle,
  PowerSquare,
  Radio,
  RefreshCw,
  Scroll,
  ShieldAlert,
  ShieldCheck,
  Waves,
} from "lucide-react";
import type { ComponentType } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api, diagBundleHref } from "@/lib/api";
import type {
  BootStatusResponse,
  CapabilitiesResponse,
  ChainEventsResponse,
  CharterResponse,
  CostStateResponse,
  DRStateResponse,
  HealthRollupResponse,
  HealthStatus,
  HeartbeatServicesResponse,
  HeartbeatStatus,
  KoraAssignedSeaTicketsResponse,
  KoraControlObservedStateResponse,
  OperationalStateResponse,
  RunbooksManifest,
} from "@/lib/api";

type LoadStatus<T> =
  | { state: "loading" }
  | { state: "ready"; data: T }
  | { state: "error"; error: string };

interface DashboardData {
  // v1 row 1 (DASHBOARD #63 4d1bc11)
  health: LoadStatus<HealthRollupResponse>;
  operational: LoadStatus<OperationalStateResponse>;
  cost: LoadStatus<CostStateResponse>;
  sea: LoadStatus<KoraAssignedSeaTicketsResponse>;
  control: LoadStatus<KoraControlObservedStateResponse>;
  boot: LoadStatus<BootStatusResponse>;
  dr: LoadStatus<DRStateResponse>;
  // v2 row 2 — newer surfaces (KR-P2-DASHBOARD-V2)
  capabilities: LoadStatus<CapabilitiesResponse>;
  charter: LoadStatus<CharterResponse>;
  recentEvents: LoadStatus<ChainEventsResponse>;
  runbooks: LoadStatus<RunbooksManifest>;
  // KR-HB-PANEL — backend service heartbeat (stub)
  heartbeat: LoadStatus<HeartbeatServicesResponse>;
}

const INITIAL_DATA: DashboardData = {
  health: { state: "loading" },
  operational: { state: "loading" },
  cost: { state: "loading" },
  sea: { state: "loading" },
  control: { state: "loading" },
  boot: { state: "loading" },
  dr: { state: "loading" },
  capabilities: { state: "loading" },
  charter: { state: "loading" },
  recentEvents: { state: "loading" },
  runbooks: { state: "loading" },
  heartbeat: { state: "loading" },
};

const HEALTH_TONE: Record<HealthStatus, "success" | "warning" | "destructive" | "outline"> = {
  healthy: "success",
  degraded: "warning",
  stopped: "outline",
  outage: "destructive",
};

function healthHeroIcon(status: HealthStatus) {
  switch (status) {
    case "healthy":
      return <CheckCircle2 className="h-8 w-8 text-success" />;
    case "degraded":
      return <AlertTriangle className="h-8 w-8 text-warning" />;
    case "stopped":
      return <PauseCircle className="h-8 w-8 text-muted-foreground" />;
    case "outage":
      return <AlertOctagon className="h-8 w-8 text-destructive" />;
  }
}

function formatUsd(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const deltaMs = d.getTime() - Date.now();
  const absSec = Math.abs(deltaMs) / 1000;
  if (absSec < 60) return deltaMs < 0 ? "just now" : "in <1m";
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

function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

interface DashboardCardProps {
  title: string;
  icon: ComponentType<{ className?: string }>;
  to: string;
  status: LoadStatus<unknown>;
  stubbed?: boolean;
  onRetry: () => void;
  children: React.ReactNode;
}

function DashboardCard({
  title,
  icon: Icon,
  to,
  status,
  stubbed,
  onRetry,
  children,
}: DashboardCardProps) {
  const borderClass =
    status.state === "error"
      ? "border-destructive/40"
      : status.state === "ready"
        ? "border-border hover:border-primary/40 transition-colors"
        : "border-border";

  const cardBody = (
    <CardContent className="flex flex-col gap-3 py-4 h-full">
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 text-primary shrink-0" />
        <span className="text-sm font-medium">{title}</span>
        {stubbed && (
          <span
            className="text-[10px] text-muted-foreground uppercase tracking-wide ml-auto"
            title="Source endpoint is returning stub data — flips to real when its runtime bucket lands"
          >
            (stubbed)
          </span>
        )}
        {status.state === "ready" && !stubbed && (
          <ArrowRight className="h-3.5 w-3.5 text-muted-foreground ml-auto opacity-0 group-hover:opacity-100 transition-opacity" />
        )}
      </div>

      {status.state === "loading" && (
        <div className="flex items-center justify-center py-6">
          <Spinner className="text-lg text-primary" />
        </div>
      )}

      {status.state === "error" && (
        <div className="flex flex-col gap-2">
          <div className="flex items-start gap-2 text-xs text-destructive">
            <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
            <span className="truncate" title={status.error}>
              Failed to load: {status.error}
            </span>
          </div>
          <Button
            size="sm"
            ghost
            onClick={(e) => {
              e.preventDefault();
              onRetry();
            }}
          >
            <RefreshCw className="h-3 w-3" />
            Retry
          </Button>
        </div>
      )}

      {status.state === "ready" && children}
    </CardContent>
  );

  if (status.state === "ready") {
    return (
      <Link
        to={to}
        className={`block group rounded-lg border ${borderClass} bg-card`}
      >
        {cardBody}
      </Link>
    );
  }

  return <Card className={borderClass}>{cardBody}</Card>;
}

// ── Per-card body renderers ──────────────────────────────────────────────

function OperationalCardBody({ data }: { data: OperationalStateResponse }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 flex-wrap">
        <Badge tone={data.is_degraded ? "warning" : "success"}>
          {data.primary_state.toUpperCase()}
        </Badge>
        {data.is_degraded && <Badge tone="destructive">DEGRADED</Badge>}
      </div>
      <div className="text-xs text-muted-foreground">
        claim: {data.claim_permission}
      </div>
    </div>
  );
}

function CostCardBody({ data }: { data: CostStateResponse }) {
  const c = data.current;
  const rungTone =
    c.active_rung === "hard_stop_100"
      ? "destructive"
      : c.active_rung === "downshift_90" || c.active_rung === "warn_75"
        ? "warning"
        : "success";
  return (
    <div className="flex flex-col gap-2">
      <div className="text-xl font-semibold">
        {formatUsd(c.spent_to_date_usd)}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          / {formatUsd(c.credit_pool_usd)}
        </span>
      </div>
      <div className="flex items-center gap-1.5 flex-wrap">
        <Badge tone={rungTone}>{c.active_rung}</Badge>
        <Badge tone="outline">{c.effective_model_tier}</Badge>
      </div>
    </div>
  );
}

function SeaCardBody({ data }: { data: KoraAssignedSeaTicketsResponse }) {
  if (data.in_progress.length > 0) {
    const t = data.in_progress[0];
    return (
      <div className="flex flex-col gap-1">
        <div className="text-sm truncate" title={t.title}>
          {t.title}
        </div>
        <div className="text-xs text-muted-foreground">
          claim #{t.claim_count} · {data.queued.length} queued
        </div>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1">
      <div className="text-sm">
        Idle{" "}
        <span className="text-muted-foreground">
          ({data.queued.length} queued)
        </span>
      </div>
      <div className="text-xs text-muted-foreground">
        {data.failed_or_blocked.length > 0
          ? `${data.failed_or_blocked.length} blocked/failed`
          : "nothing blocked"}
      </div>
    </div>
  );
}

function ControlCardBody({ data }: { data: KoraControlObservedStateResponse }) {
  if (data.active.length > 0) {
    // Highest open level wins (matches the panel's R4.1 sort).
    const highest = [...data.active].sort((a, b) => b.level - a.level)[0];
    return (
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-1.5 flex-wrap">
          <Badge tone="destructive">
            L{highest.level} {highest.kind.toUpperCase()}
          </Badge>
          <Badge tone="warning">{highest.lifecycle_state}</Badge>
        </div>
        <div className="text-xs text-muted-foreground truncate" title={highest.reason}>
          {highest.reason}
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2">
      <CheckCircle2 className="h-4 w-4 text-success" />
      <span className="text-sm">No active commands</span>
    </div>
  );
}

function BootCardBody({ data }: { data: BootStatusResponse }) {
  const c = data.current;
  const tone =
    c.outcome === "ready"
      ? "success"
      : c.outcome === "booting"
        ? "warning"
        : "destructive";
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-1.5 flex-wrap">
        <Badge tone={tone}>{c.outcome.toUpperCase()}</Badge>
        <span className="text-xs text-muted-foreground">
          {formatElapsed(c.elapsed_ms)}
        </span>
      </div>
      <div className="text-xs text-muted-foreground">
        started {formatRelative(c.started_at)}
      </div>
    </div>
  );
}

function DRCardBody({ data }: { data: DRStateResponse }) {
  if (data.runbook_pending) {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-destructive" />
          <span className="text-sm font-semibold text-destructive">
            DR detected
          </span>
        </div>
        <div className="text-xs text-muted-foreground">runbook pending</div>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2">
      <ShieldCheck className="h-4 w-4 text-success" />
      <span className="text-sm">
        Clean{" "}
        <span className="text-muted-foreground">
          (epoch {data.current.substrate_epoch})
        </span>
      </span>
    </div>
  );
}

// ── Row-2 card body renderers (KR-P2-DASHBOARD-V2) ──────────────────────

function CapabilitiesCardBody({ data }: { data: CapabilitiesResponse }) {
  const { total_tools, total_caps, unmapped_count } = data;
  return (
    <div className="flex flex-col gap-2">
      <div className="text-xl font-semibold">
        {total_tools}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          tools / {total_caps} cap_* groups
        </span>
      </div>
      {unmapped_count > 0 ? (
        <Badge tone="warning">
          {unmapped_count} group{unmapped_count === 1 ? "" : "s"} escalating
        </Badge>
      ) : (
        <Badge tone="success">all caps mapped</Badge>
      )}
    </div>
  );
}

function CharterCardBody({ data }: { data: CharterResponse }) {
  if (data.active === null) {
    return (
      <div className="text-sm text-muted-foreground">
        No active Constitution loaded
      </div>
    );
  }
  const a = data.active;
  const revShort = a.revision_id ? truncateMiddle(a.revision_id, 8) : "—";
  const hashShort = a.rules_hash ? truncateMiddle(a.rules_hash, 8) : "—";
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <code
          className="text-xs font-mono bg-muted/40 px-1.5 py-0.5 rounded border border-border"
          title={a.revision_id ?? ""}
        >
          {revShort}
        </code>
        {!a.rules_available && (
          <Badge tone="warning">rules pending</Badge>
        )}
      </div>
      <div className="text-xs text-muted-foreground">
        rules_hash: <code className="text-xs">{hashShort}</code>
      </div>
      <div className="text-xs text-muted-foreground">
        loaded {formatRelative(a.loaded_at)}
      </div>
    </div>
  );
}

function RecentEventsCardBody({ data }: { data: ChainEventsResponse }) {
  const events = data.events.slice(0, 5);
  if (events.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">No recent events</div>
    );
  }
  return (
    <ul className="flex flex-col gap-1 text-xs">
      {events.map((e) => (
        <li key={e.event_id} className="flex items-baseline gap-2">
          <span className="text-muted-foreground shrink-0 w-12 truncate">
            {formatRelative(e.occurred_at)}
          </span>
          <code className="font-mono truncate" title={e.event_type}>
            {e.event_type.replace(/^kora\./, "")}
          </code>
        </li>
      ))}
    </ul>
  );
}

function RunbooksCardBody({ data }: { data: RunbooksManifest }) {
  const total = data.runbooks.length;
  const available = data.runbooks.filter((r) => r.available).length;
  const pending = total - available;
  // Most-recently-modified available runbook (best proxy for "what's
  // currently maintained"). When no runbooks are available, omit.
  const mostRecent = [...data.runbooks]
    .filter((r) => r.available && r.last_modified)
    .sort((a, b) =>
      (b.last_modified ?? "").localeCompare(a.last_modified ?? ""),
    )[0];
  return (
    <div className="flex flex-col gap-2">
      <div className="text-xl font-semibold">
        {available}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          available
        </span>
        {pending > 0 && (
          <span className="text-xs text-muted-foreground font-normal ml-1.5">
            · {pending} pending
          </span>
        )}
      </div>
      {mostRecent ? (
        <div className="text-xs text-muted-foreground truncate" title={mostRecent.title}>
          latest: {mostRecent.title} ({formatRelative(mostRecent.last_modified)})
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">
          no available runbooks yet
        </div>
      )}
    </div>
  );
}

function truncateMiddle(value: string, head: number): string {
  if (value.length <= head + 4) return value;
  return `${value.slice(0, head)}…${value.slice(-4)}`;
}

function HeartbeatCardBody({ data }: { data: HeartbeatServicesResponse }) {
  // Aggregate per-status counts. "5 services / 1 degraded / 0 unhealthy"
  // (the spec §3(c) example) matches what the panel itself shows in its
  // summary strip, kept consistent so the dashboard card + panel agree.
  const total = data.services.length;
  const counts: Record<HeartbeatStatus, number> = {
    healthy: 0,
    degraded: 0,
    unhealthy: 0,
  };
  for (const s of data.services) counts[s.status]++;
  // Worst-status tone drives the headline number colour: unhealthy >
  // degraded > healthy. operator scans the dashboard for "is anything
  // wrong" and this surfaces it without making them squint at chips.
  const worst =
    counts.unhealthy > 0
      ? "unhealthy"
      : counts.degraded > 0
        ? "degraded"
        : "healthy";
  const headlineClass =
    worst === "unhealthy"
      ? "text-destructive"
      : worst === "degraded"
        ? "text-warning"
        : "text-foreground";
  return (
    <div className="flex flex-col gap-2">
      <div className={`text-xl font-semibold ${headlineClass}`}>
        {total}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          service{total === 1 ? "" : "s"}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        {counts.healthy > 0 && (
          <Badge tone="success">{counts.healthy} healthy</Badge>
        )}
        {counts.degraded > 0 && (
          <Badge tone="warning">{counts.degraded} degraded</Badge>
        )}
        {counts.unhealthy > 0 && (
          <Badge tone="destructive">{counts.unhealthy} unhealthy</Badge>
        )}
      </div>
    </div>
  );
}

// ── Hero ─────────────────────────────────────────────────────────────────

interface HealthHeroProps {
  status: LoadStatus<HealthRollupResponse>;
  onRetry: () => void;
}

function HealthHero({ status, onRetry }: HealthHeroProps) {
  if (status.state === "loading") {
    return (
      <Card>
        <CardContent className="flex items-center justify-center py-10">
          <Spinner className="text-xl text-primary" />
        </CardContent>
      </Card>
    );
  }

  if (status.state === "error") {
    return (
      <Card className="border-destructive/40">
        <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
          <AlertTriangle className="h-5 w-5 mt-0.5" />
          <div className="flex-1">
            <div className="font-medium">Failed to load overall health</div>
            <div className="text-xs opacity-80 truncate" title={status.error}>
              {status.error}
            </div>
          </div>
          <Button size="sm" ghost onClick={onRetry}>
            <RefreshCw className="h-3 w-3" />
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const data = status.data;
  const borderTone =
    data.overall === "healthy"
      ? "border-success/30"
      : data.overall === "outage"
        ? "border-destructive/40"
        : "border-warning/40";

  return (
    <Link to="/health-rollup" className={`block rounded-lg border ${borderTone} bg-card`}>
      <CardContent className="flex items-center gap-5 py-6">
        {healthHeroIcon(data.overall)}
        <div className="flex flex-col gap-1 flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-3xl font-semibold uppercase tracking-wide">
              {data.overall}
            </span>
            <Badge tone={HEALTH_TONE[data.overall]}>overall</Badge>
          </div>
          <div className="flex items-center gap-2 flex-wrap text-xs">
            <Badge tone={HEALTH_TONE[data.control_plane]}>
              control plane: {data.control_plane}
            </Badge>
            <Badge tone={HEALTH_TONE[data.worker]}>worker: {data.worker}</Badge>
            {data.stopped_reason && (
              <span className="text-muted-foreground">
                reason: {data.stopped_reason}
              </span>
            )}
          </div>
        </div>
        <ArrowRight className="h-4 w-4 text-muted-foreground" />
      </CardContent>
    </Link>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────

function isStubbed(s: LoadStatus<unknown>): boolean {
  // Capabilities + Runbooks responses don't carry a stub field
  // (always-live by design). Safely read via property check rather
  // than narrowing the LoadStatus type — the dashboard mixes
  // responses with and without the flag.
  if (s.state !== "ready") return false;
  const data = s.data as { stub?: unknown };
  return data.stub === true;
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData>(INITIAL_DATA);
  const [refreshing, setRefreshing] = useState(false);
  const { toast, showToast } = useToast();

  const loadOne = useCallback(
    async <K extends keyof DashboardData>(
      key: K,
      fetcher: () => Promise<
        DashboardData[K] extends LoadStatus<infer T> ? T : never
      >,
    ) => {
      setData((prev) => ({ ...prev, [key]: { state: "loading" } }));
      try {
        const result = await fetcher();
        setData(
          (prev) =>
            ({
              ...prev,
              [key]: { state: "ready", data: result },
            }) as DashboardData,
        );
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setData((prev) => ({
          ...prev,
          [key]: { state: "error", error: msg },
        }));
      }
    },
    [],
  );

  const loadAll = useCallback(
    async (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      // Promise.allSettled so one slow/broken endpoint doesn't stall the rest.
      await Promise.allSettled([
        // Row 1 — v1 surfaces
        loadOne("health", () => api.getHealthRollup()),
        loadOne("operational", () => api.getOperationalState()),
        loadOne("cost", () => api.getCostState()),
        loadOne("sea", () => api.getKoraAssignedSeaTickets()),
        loadOne("control", () => api.getKoraControlObservedState()),
        loadOne("boot", () => api.getBootStatus()),
        loadOne("dr", () => api.getDRState()),
        // Row 2 — KR-P2-DASHBOARD-V2 surfaces
        loadOne("capabilities", () => api.getCapabilities()),
        loadOne("charter", () => api.getCharter()),
        loadOne("recentEvents", () => api.getChainEvents({ limit: 5 })),
        loadOne("runbooks", () => api.getRunbooks()),
        // KR-HB-PANEL
        loadOne("heartbeat", () => api.getHeartbeatServices()),
      ]);
      if (isManual) {
        setRefreshing(false);
        showToast("Dashboard refreshed", "success");
      }
    },
    [loadOne, showToast],
  );

  useEffect(() => {
    void loadAll(false);
  }, [loadAll]);

  // All sources the dashboard fetches. Used for the anyStubbed banner
  // + the footer's live/stubbed aggregate count. capabilities + runbooks
  // responses don't carry a stub flag (always-live by design) — their
  // ready states count toward "live"; loading/error don't count.
  const ALL_SOURCES = [
    data.health,
    data.operational,
    data.cost,
    data.sea,
    data.control,
    data.boot,
    data.dr,
    data.capabilities,
    data.charter,
    data.recentEvents,
    data.runbooks,
    data.heartbeat,
  ];

  const anyStubbed = ALL_SOURCES.some((s) => isStubbed(s));

  // Footer aggregate: a source counts as "live" when it returned ready
  // with stub:false (or no stub flag at all — caps/runbooks always
  // count as live when ready). "stubbed" requires ready + stub:true.
  // Loading/error sources are excluded from both counts.
  const liveCount = ALL_SOURCES.filter(
    (s) => s.state === "ready" && !isStubbed(s),
  ).length;
  const stubbedCount = ALL_SOURCES.filter((s) => isStubbed(s)).length;

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-start justify-between gap-4">
        <H2>Kora — Overview</H2>
        <div className="flex items-center gap-2">
          {/* Diag bundle: standard browser download via <a href download>;
              no JS fetcher needed. Operator click → zip with all 10
              panel sources + manifest for substrate-team triage. */}
          <a
            href={diagBundleHref()}
            download
            title="Download a zip of all panel data sources for substrate-team triage"
            className="inline-flex"
          >
            <Button size="sm" ghost>
              <Archive className="h-3 w-3" />
              Diag bundle
            </Button>
          </a>
          <Button size="sm" ghost disabled={refreshing} onClick={() => loadAll(true)}>
            <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
            Reload
          </Button>
        </div>
      </div>

      {anyStubbed && (
        <Card className="border-border bg-muted/30">
          <CardContent className="py-2 flex items-center gap-2 text-xs text-muted-foreground">
            <Info className="h-3.5 w-3.5 shrink-0" />
            <span>
              Some cards show preview data — flips to real as runtime buckets
              ship.
            </span>
          </CardContent>
        </Card>
      )}

      <HealthHero
        status={data.health}
        onRetry={() => void loadOne("health", () => api.getHealthRollup())}
      />

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        <DashboardCard
          title="Operational"
          icon={Activity}
          to="/operational-state"
          status={data.operational}
          stubbed={isStubbed(data.operational)}
          onRetry={() =>
            void loadOne("operational", () => api.getOperationalState())
          }
        >
          {data.operational.state === "ready" && (
            <OperationalCardBody data={data.operational.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Cost"
          icon={DollarSign}
          to="/cost-state"
          status={data.cost}
          stubbed={isStubbed(data.cost)}
          onRetry={() => void loadOne("cost", () => api.getCostState())}
        >
          {data.cost.state === "ready" && <CostCardBody data={data.cost.data} />}
        </DashboardCard>

        <DashboardCard
          title="Sea Tickets"
          icon={Waves}
          to="/sea-tickets"
          status={data.sea}
          stubbed={isStubbed(data.sea)}
          onRetry={() =>
            void loadOne("sea", () => api.getKoraAssignedSeaTickets())
          }
        >
          {data.sea.state === "ready" && <SeaCardBody data={data.sea.data} />}
        </DashboardCard>

        <DashboardCard
          title="STOP-KORA"
          icon={OctagonAlert}
          to="/kora-control"
          status={data.control}
          stubbed={isStubbed(data.control)}
          onRetry={() =>
            void loadOne("control", () => api.getKoraControlObservedState())
          }
        >
          {data.control.state === "ready" && (
            <ControlCardBody data={data.control.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Last boot"
          icon={PowerSquare}
          to="/boot-status"
          status={data.boot}
          stubbed={isStubbed(data.boot)}
          onRetry={() => void loadOne("boot", () => api.getBootStatus())}
        >
          {data.boot.state === "ready" && <BootCardBody data={data.boot.data} />}
        </DashboardCard>

        <DashboardCard
          title="DR"
          icon={ShieldAlert}
          to="/dr-state"
          status={data.dr}
          stubbed={isStubbed(data.dr)}
          onRetry={() => void loadOne("dr", () => api.getDRState())}
        >
          {data.dr.state === "ready" && <DRCardBody data={data.dr.data} />}
        </DashboardCard>
      </div>

      {/* ── Row 2: newer surfaces (KR-P2-DASHBOARD-V2 + KR-HB-PANEL) ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
        <DashboardCard
          title="Capabilities"
          icon={ShieldCheck}
          to="/capabilities"
          status={data.capabilities}
          stubbed={isStubbed(data.capabilities)}
          onRetry={() =>
            void loadOne("capabilities", () => api.getCapabilities())
          }
        >
          {data.capabilities.state === "ready" && (
            <CapabilitiesCardBody data={data.capabilities.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Charter"
          icon={Scroll}
          to="/charter"
          status={data.charter}
          stubbed={isStubbed(data.charter)}
          onRetry={() => void loadOne("charter", () => api.getCharter())}
        >
          {data.charter.state === "ready" && (
            <CharterCardBody data={data.charter.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Recent events"
          icon={Radio}
          to="/chain-events"
          status={data.recentEvents}
          stubbed={isStubbed(data.recentEvents)}
          onRetry={() =>
            void loadOne("recentEvents", () => api.getChainEvents({ limit: 5 }))
          }
        >
          {data.recentEvents.state === "ready" && (
            <RecentEventsCardBody data={data.recentEvents.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Runbooks"
          icon={BookOpenCheck}
          to="/runbooks"
          status={data.runbooks}
          stubbed={isStubbed(data.runbooks)}
          onRetry={() => void loadOne("runbooks", () => api.getRunbooks())}
        >
          {data.runbooks.state === "ready" && (
            <RunbooksCardBody data={data.runbooks.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Heartbeat"
          icon={Heart}
          to="/heartbeat"
          status={data.heartbeat}
          stubbed={isStubbed(data.heartbeat)}
          onRetry={() =>
            void loadOne("heartbeat", () => api.getHeartbeatServices())
          }
        >
          {data.heartbeat.state === "ready" && (
            <HeartbeatCardBody data={data.heartbeat.data} />
          )}
        </DashboardCard>
      </div>

      {/* ── Bottom strip: links to other (non-admin-panel) pages ─────── */}
      <div className="flex flex-wrap gap-3 text-xs text-muted-foreground pt-4 border-t">
        <Link to="/sessions" className="hover:text-foreground underline-offset-2 hover:underline">
          Sessions
        </Link>
        <Link to="/capabilities" className="hover:text-foreground underline-offset-2 hover:underline">
          Capabilities
        </Link>
        <Link to="/skills" className="hover:text-foreground underline-offset-2 hover:underline">
          Skills
        </Link>
        <Link to="/plugins" className="hover:text-foreground underline-offset-2 hover:underline">
          Plugins
        </Link>
        <Link to="/identity" className="hover:text-foreground underline-offset-2 hover:underline">
          Identity
        </Link>
        <Link to="/mcp" className="hover:text-foreground underline-offset-2 hover:underline">
          MCP
        </Link>
        <Link to="/cron" className="hover:text-foreground underline-offset-2 hover:underline">
          Cron
        </Link>
        <Link to="/profiles" className="hover:text-foreground underline-offset-2 hover:underline">
          Profiles
        </Link>
        <Link to="/config" className="hover:text-foreground underline-offset-2 hover:underline">
          Config
        </Link>
        <Link to="/env" className="hover:text-foreground underline-offset-2 hover:underline">
          Env
        </Link>
        <span className="ml-auto flex items-center gap-1.5">
          <HeartPulse className="h-3 w-3" />
          <Link to="/health-rollup" className="hover:text-foreground underline-offset-2 hover:underline">
            Detailed health
          </Link>
        </span>
        <span className="flex items-center gap-1.5">
          <Hourglass className="h-3 w-3" />
          {refreshing ? "refreshing…" : "manual refresh"}
        </span>
      </div>

      {/* ── v2 footer: live/stub aggregate across all dashboard sources ── */}
      <div className="text-center text-[11px] text-muted-foreground pt-2">
        Kora — Dashboard v2.{" "}
        <span className="text-success">{liveCount} live source{liveCount === 1 ? "" : "s"}</span>
        {" / "}
        <span className={stubbedCount > 0 ? "text-warning" : "text-muted-foreground"}>
          {stubbedCount} stubbed source{stubbedCount === 1 ? "" : "s"}
        </span>
      </div>
    </div>
  );
}
