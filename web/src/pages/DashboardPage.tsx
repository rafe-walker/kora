import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertOctagon,
  AlertTriangle,
  Archive,
  ArrowRight,
  BookOpenCheck,
  Brain,
  CheckCircle2,
  DollarSign,
  Cable,
  Heart,
  HeartPulse,
  Inbox,
  Hourglass,
  Info,
  OctagonAlert,
  PauseCircle,
  PowerSquare,
  Radio,
  RefreshCw,
  Mail,
  MessageCircle,
  Scroll,
  ShieldAlert,
  ShieldCheck,
  Waves,
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
  MCPClientsListResponse,
  OperationalStateResponse,
  RunbooksManifest,
  WebhookEventsResponse,
  WebhookEventStatus,
  AgentActivityResponse,
  AgentCallStatus,
  SlackDMResponse,
  SlackDMHandledStatus,
  EmailResponse,
  EmailHandledStatus,
  ReasoningResponse,
  AlertsResponse,
  SnapshotResponse,
} from "@/lib/api";
import { AlertsBanner } from "@/components/AlertsBanner";
import { FreshnessBadge } from "@/components/FreshnessBadge";

import { usePanelView } from "@/hooks/usePanelView";
import { useActiveTenant } from "@/hooks/useActiveTenant";
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
  // KR-MCP-3 — installed external MCP clients (stub)
  mcpClients: LoadStatus<MCPClientsListResponse>;
  // KR-WEBHOOK-EVENTS-PANEL — public-port traffic lens (stub)
  webhookEvents: LoadStatus<WebhookEventsResponse>;
  // KR-AGENT-ACTIVITY-PANEL — recent agent-driven /mcp calls (stub)
  agentActivity: LoadStatus<AgentActivityResponse>;
  // KR-SLACK-DM-PANEL — Kora ↔ Joshua DM conversation (stub)
  slackDM: LoadStatus<SlackDMResponse>;
  // KR-EMAIL-PANEL — Kora ↔ Joshua email inbox/outbox (stub)
  email: LoadStatus<EmailResponse>;
  // KR-REASONING-PANEL — Kora ReasoningEngine activity (stub)
  reasoning: LoadStatus<ReasoningResponse>;
  // KR-ALERTS-PANEL — operator-attention banner (stub)
  alerts: LoadStatus<AlertsResponse>;
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
  mcpClients: { state: "loading" },
  webhookEvents: { state: "loading" },
  agentActivity: { state: "loading" },
  slackDM: { state: "loading" },
  email: { state: "loading" },
  reasoning: { state: "loading" },
  alerts: { state: "loading" },
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

// KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — compact card
// body for the "All tenants" pseudo-tenant. The dashboard's small
// cost card can't host the side-by-side per-tenant grid (no
// room); aggregate detail lives on /cost-state. This body
// surfaces one-line aggregate totals + a hint to click through.
function AggregateCostCardBody({
  byTenant,
}: {
  byTenant: Record<string, { spent_to_date_usd: number | "unknown"; credit_pool_usd: number }>;
}) {
  const summable = Object.values(byTenant).filter(
    (b): b is { spent_to_date_usd: number; credit_pool_usd: number } =>
      typeof b.spent_to_date_usd === "number",
  );
  const totalSpent = summable.reduce((a, b) => a + b.spent_to_date_usd, 0);
  const totalPool = summable.reduce((a, b) => a + b.credit_pool_usd, 0);
  const count = Object.keys(byTenant).length;
  return (
    <div className="flex flex-col gap-2">
      <div className="text-xl font-semibold">
        {formatUsd(totalSpent)}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          / {formatUsd(totalPool)}
        </span>
      </div>
      <div className="text-xs text-muted-foreground">
        {count} tenant{count === 1 ? "" : "s"} · click for breakdown
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
  // Aggregate per-status counts. Matches the panel's summary strip
  // so the dashboard card + panel agree. KR-FEAT-HEARTBEAT ST2
  // added the "unknown" arm (probe pending; cold-start, auth-missing,
  // or in-flight) — it's bucketed separately so the operator can
  // distinguish "scheduler not done yet" from "service down".
  const total = data.services.length;
  const counts: Record<HeartbeatStatus, number> = {
    healthy: 0,
    degraded: 0,
    unhealthy: 0,
    unknown: 0,
  };
  for (const s of data.services) counts[s.status]++;
  // Worst-status tone drives the headline number colour: unhealthy >
  // degraded > healthy. "unknown" is intentionally NOT a worst-
  // status driver (it's pending, not failing) — cache_warming
  // suppresses any pseudo-outage signal when the daemon just booted.
  const worst =
    counts.unhealthy > 0
      ? "unhealthy"
      : counts.degraded > 0
        ? "degraded"
        : "healthy";
  const headlineClass = data.cache_warming
    ? "text-muted-foreground"
    : worst === "unhealthy"
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
          {data.cache_warming ? " · warming" : ""}
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
        {counts.unknown > 0 && (
          <Badge tone="outline">{counts.unknown} pending</Badge>
        )}
      </div>
    </div>
  );
}

function MCPClientsCardBody({ data }: { data: MCPClientsListResponse }) {
  // Aggregate per-status counts for the dashboard tile. Matches the
  // panel's summary strip + the bucket §3(c) headline shape
  // ("2 MCPs configured / 0 connected / 0 errors"). Token presence
  // intentionally NOT surfaced here — the panel detail view shows
  // the per-client presence/absence indicator; dashboard scans for
  // "is anything wrong with the MCP fleet" at the status level.
  const total = data.clients.length;
  const connected = data.clients.filter((c) => c.status === "connected").length;
  const errors = data.clients.filter(
    (c) => c.status === "error" || c.status === "unhealthy",
  ).length;
  const headlineClass = errors > 0 ? "text-destructive" : "text-foreground";
  return (
    <div className="flex flex-col gap-2">
      <div className={`text-xl font-semibold ${headlineClass}`}>
        {total}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          MCP{total === 1 ? "" : "s"} configured
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        <Badge tone={connected > 0 ? "success" : "outline"}>
          {connected} connected
        </Badge>
        {errors > 0 && (
          <Badge tone="destructive">
            {errors} error{errors === 1 ? "" : "s"}
          </Badge>
        )}
      </div>
    </div>
  );
}

function WebhookEventsCardBody({ data }: { data: WebhookEventsResponse }) {
  // Counts mirror the panel's stats strip; dashboard surface is more
  // compressed but the headline + signal pills match what the
  // operator sees in the full panel.
  const counts: Record<WebhookEventStatus, number> = {
    verified: 0,
    dead_letter: 0,
    rate_limited: 0,
    handler_error: 0,
  };
  for (const e of data.events) counts[e.status]++;
  // Bucket §3(c) operator-attention contract: dashboard card border
  // goes destructive when dead_letter > 5 in 24h. Headline tone
  // tracks the same trigger so the card visually screams from the
  // dashboard glance.
  const deadLetterAlert = counts.dead_letter > 5;
  const headlineClass = deadLetterAlert
    ? "text-destructive"
    : "text-foreground";
  return (
    <div className="flex flex-col gap-2">
      <div className={`text-xl font-semibold ${headlineClass}`}>
        {data.total_recent_24h}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          event{data.total_recent_24h === 1 ? "" : "s"} / 24h
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        {counts.verified > 0 && (
          <Badge tone="success">{counts.verified} verified</Badge>
        )}
        {counts.dead_letter > 0 && (
          <Badge tone="destructive">
            {counts.dead_letter} dead-letter
          </Badge>
        )}
        {counts.rate_limited > 0 && (
          <Badge tone="warning">{counts.rate_limited} rate-limited</Badge>
        )}
        {counts.handler_error > 0 && (
          <Badge tone="destructive">
            {counts.handler_error} handler-error
          </Badge>
        )}
      </div>
    </div>
  );
}

function AgentActivityCardBody({ data }: { data: AgentActivityResponse }) {
  // Operator-attention contract (mirrors WebhookEvents):
  // headline goes destructive when denied-class calls cross 10 in the
  // visible window OR any handler_error / timeout shows up — both are
  // "something is actively breaking, not just noisy".
  const counts: Record<AgentCallStatus, number> = {
    ok: 0,
    capability_denied: 0,
    denied_prod_only: 0,
    tool_not_found: 0,
    handler_error: 0,
    timeout: 0,
  };
  for (const c of data.calls) counts[c.status]++;
  const deniedTotal = counts.capability_denied + counts.denied_prod_only;
  const hardFailTotal = counts.handler_error + counts.timeout;
  const alert = deniedTotal > 10 || hardFailTotal > 0;
  const headlineClass = alert ? "text-destructive" : "text-foreground";
  return (
    <div className="flex flex-col gap-2">
      <div className={`text-xl font-semibold ${headlineClass}`}>
        {data.total_recent_24h}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          call{data.total_recent_24h === 1 ? "" : "s"} / 24h
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        {counts.ok > 0 && <Badge tone="success">{counts.ok} ok</Badge>}
        {deniedTotal > 0 && (
          <Badge tone="warning">{deniedTotal} denied</Badge>
        )}
        {counts.handler_error > 0 && (
          <Badge tone="destructive">
            {counts.handler_error} handler-error
          </Badge>
        )}
        {counts.timeout > 0 && (
          <Badge tone="destructive">{counts.timeout} timeout</Badge>
        )}
      </div>
    </div>
  );
}

function SlackDMCardBody({ data }: { data: SlackDMResponse }) {
  // Operator-attention contract: headline goes destructive when
  // filtered_non_joshua > 0 in 24h — someone other than Joshua is
  // trying to DM the bot, which warrants investigation per spec
  // §2(c). Other filter states (bot / subtype) are normal Slack
  // noise and stay muted.
  const filteredNonJoshua = data.by_status_24h["filtered_non_joshua"] ?? 0;
  const sentFailed = data.by_status_24h["sent_failed"] ?? 0;
  const handlerError = data.by_status_24h["handler_error"] ?? 0;
  const alert = filteredNonJoshua > 0 || sentFailed > 0 || handlerError > 0;
  const headlineClass = alert ? "text-destructive" : "text-foreground";
  // Sum of all filtered + error status counts for the "drops" line.
  const FILTER_OR_ERROR: SlackDMHandledStatus[] = [
    "filtered_non_joshua",
    "filtered_bot",
    "filtered_subtype",
    "sent_failed",
    "handler_error",
    "dropped_paused",
  ];
  const dropsTotal = FILTER_OR_ERROR.reduce(
    (sum, s) => sum + (data.by_status_24h[s] ?? 0),
    0,
  );
  return (
    <div className="flex flex-col gap-2">
      <div className={`text-xl font-semibold ${headlineClass}`}>
        {data.total_recent_24h}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          msg{data.total_recent_24h === 1 ? "" : "s"} / 24h
          {dropsTotal > 0 ? ` · ${dropsTotal} drop${dropsTotal === 1 ? "" : "s"}` : ""}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        <Badge tone="outline">
          ←{data.by_direction_24h.inbound} →{data.by_direction_24h.outbound}
        </Badge>
        {filteredNonJoshua > 0 && (
          <Badge tone="destructive">
            {filteredNonJoshua} non-Joshua
          </Badge>
        )}
        {sentFailed > 0 && (
          <Badge tone="destructive">{sentFailed} send-failed</Badge>
        )}
        {handlerError > 0 && (
          <Badge tone="destructive">{handlerError} handler-error</Badge>
        )}
      </div>
    </div>
  );
}

function EmailCardBody({ data }: { data: EmailResponse }) {
  // Operator-attention contract per spec §2(c): headline goes
  // destructive when filtered_non_allowlist > 0 (someone outside
  // the allowlist is emailing the bot — investigate) OR any
  // spoofing_warning fires in the visible window OR sent_failed /
  // handler_error appears. Same shape as the SlackDM headline rule.
  const filteredNonAllow = data.by_status_24h["filtered_non_allowlist"] ?? 0;
  const sentFailed = data.by_status_24h["sent_failed"] ?? 0;
  const handlerError = data.by_status_24h["handler_error"] ?? 0;
  // spoofing_warning is per-message; sum across the visible window
  // (by_status_24h doesn't break it out — it's an inbound-only red
  // flag orthogonal to handled_status).
  const spoofingCount = data.messages.filter(
    (m) => m.spoofing_warning === true,
  ).length;
  const alert =
    filteredNonAllow > 0 ||
    spoofingCount > 0 ||
    sentFailed > 0 ||
    handlerError > 0;
  const headlineClass = alert ? "text-destructive" : "text-foreground";
  const FLAGGED: EmailHandledStatus[] = [
    "filtered_non_allowlist",
    "filtered_wrong_recipient",
    "sent_failed",
    "handler_error",
    "dropped_paused",
  ];
  const flaggedTotal =
    FLAGGED.reduce((sum, s) => sum + (data.by_status_24h[s] ?? 0), 0) +
    spoofingCount;
  return (
    <div className="flex flex-col gap-2">
      <div className={`text-xl font-semibold ${headlineClass}`}>
        {data.total_recent_24h}
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          email{data.total_recent_24h === 1 ? "" : "s"} / 24h
          {flaggedTotal > 0
            ? ` · ${flaggedTotal} flagged`
            : ""}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        <Badge tone="outline">
          ←{data.by_direction_24h.inbound} →{data.by_direction_24h.outbound}
        </Badge>
        {filteredNonAllow > 0 && (
          <Badge tone="destructive">
            {filteredNonAllow} non-allowlist
          </Badge>
        )}
        {spoofingCount > 0 && (
          <Badge tone="destructive">{spoofingCount} spoofing</Badge>
        )}
        {sentFailed > 0 && (
          <Badge tone="destructive">{sentFailed} send-failed</Badge>
        )}
        {handlerError > 0 && (
          <Badge tone="destructive">{handlerError} handler-error</Badge>
        )}
      </div>
    </div>
  );
}

function ReasoningCardBody({ data }: { data: ReasoningResponse }) {
  // Operator-attention contract per bucket §3(c): headline goes
  // destructive when `halted > 0` in 24h — Kora was budget-locked,
  // operator should investigate cost-ladder rung. failed > 0 is
  // worth flagging but not as loudly (transport/SDK noise happens).
  const okCount = data.by_status_24h["ok"] ?? 0;
  const failedCount = data.by_status_24h["failed"] ?? 0;
  const haltedCount = data.by_status_24h["halted"] ?? 0;
  const tokensTotal =
    data.tokens_total_24h.input + data.tokens_total_24h.output;
  const alert = haltedCount > 0;
  const headlineClass = alert
    ? "text-destructive"
    : failedCount > 0
      ? "text-warning"
      : "text-foreground";
  return (
    <div className="flex flex-col gap-2">
      <div className={`text-xl font-semibold ${headlineClass}`}>
        {okCount}
        <span className="text-xs text-muted-foreground font-normal">
          /{data.total_recent_24h}
        </span>
        <span className="text-xs text-muted-foreground font-normal ml-1.5">
          call{data.total_recent_24h === 1 ? "" : "s"} ·{" "}
          {tokensTotal.toLocaleString()} tok
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        {haltedCount > 0 && (
          <Badge tone="destructive">{haltedCount} halted</Badge>
        )}
        {failedCount > 0 && (
          <Badge tone="destructive">{failedCount} failed</Badge>
        )}
        {haltedCount === 0 && failedCount === 0 && okCount > 0 && (
          <Badge tone="success">healthy</Badge>
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

// KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — total count of dashboard
// hero fields originally spec'd by KR-FE-DASHBOARD-SNAPSHOT-WIRE:
// operational + alerts + cost + health. All 4 are snapshot-driven
// on the warm-cache path after this bucket. Exported as a literal
// constant so the FreshnessBadge text can say "all 4 fields from
// snapshot" and tests can pin against the literal.
const DASHBOARD_HERO_FIELD_COUNT = 4;

// KR-FE-DASHBOARD-SNAPSHOT-WIRE + KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED
// — projection helpers that map the daemon snapshot's flat summary
// fields into the rich per-endpoint TS interfaces the dashboard
// cards consume.
//
// Coverage (after this bucket): operational + alerts + cost + health
// — the 4 fields the originating dashboard-snapshot spec called out.
// PR #169 (snapshot v3 cost_ladder.spent_to_date_usd +
// credit_pool_usd) + PR #170 (snapshot v4 daemon_health) closed the
// data gaps that kept cost + health on fan-out in PR #162. The
// remaining ~6 cards (boot / dr / capabilities / sea / control /
// charter / recentEvents / runbooks / heartbeat / mcpClients /
// webhookEvents / agentActivity / slackDM / email / reasoning) stay
// on fan-out — most don't have snapshot equivalents at all; the
// few that do (e.g. heartbeat ≈ service_health) are panel-level
// surfaces consumed by full-fidelity panels, not the dashboard
// hero cards.
//
// Anti-coercion discipline preserved per-field: each projection
// helper returns `null` when the snapshot's underlying field is
// "unknown" / incomplete, and the caller falls back to fan-out for
// THAT field specifically. Mixed-state outcomes (operational +
// alerts projected; cost falls back; health projected) are
// supported by tracking the projected-field set independently.

function projectOperationalFromSnapshot(
  snap: SnapshotResponse,
): OperationalStateResponse {
  const ops = snap.operational_state;
  return {
    primary_state: ops.primary as OperationalStateResponse["primary_state"],
    // Snapshot doesn't carry claim_permission; default per the
    // paused state ("none" blocks all claims when paused; "normal"
    // otherwise — both are valid ClaimPermission enum values per
    // api.ts:1004. OperationalCardBody renders the value verbatim
    // as small muted text under the primary_state badge.)
    claim_permission: ops.paused ? "none" : "normal",
    degradation_reasons: [],
    is_degraded: ops.paused,
    transition_history: [],
    valid_next_states: [],
    stub: false,
  } as OperationalStateResponse;
}

function projectAlertsFromSnapshot(snap: SnapshotResponse): AlertsResponse {
  // Snapshot carries aggregate counts only — per-alert array is
  // empty. AlertsBanner already handles this case (renders from
  // total_active when alerts.length === 0); the per-alert AlertsPanel
  // uses its own fan-out (api.getCurrentAlerts) so it isn't affected.
  return {
    alerts: [],
    stub: false,
    generated_at: snap.computed_at,
    total_active: snap.alerts.active_count,
    by_severity: snap.alerts.by_severity,
  };
}

// KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — cost projection from
// snapshot v3 cost_ladder. Returns null if any field CostCardBody
// renders is "unknown" — caller falls back to fan-out. This
// preserves the anti-coercion discipline at the field level (no
// $0.00 USD render when the holder hasn't initialized).
//
// CostCardBody reads: spent_to_date_usd, credit_pool_usd,
// active_rung (CostRung enum), effective_model_tier (ModelTier
// enum). All four must be present + mappable for the projection
// to succeed. Other CostStateResponse fields (rate_limit_pulse,
// deferred_tickets, reconciliation_history) aren't read by
// CostCardBody — filled with sentinel-empty values so the type
// contract holds; the CostStatePage still fans out for the full
// panel.
function projectCostFromSnapshot(
  snap: SnapshotResponse,
  activeTenantId?: string,
): CostStateResponse | null {
  // KR-FE-TENANT-PICKER-COCKPIT-CHROME — when the operator picked a
  // non-default tenant, project from cost_ladder_by_tenant[id]
  // rather than the legacy default-tenant cost_ladder block. The
  // by_tenant block lacks ``model_default`` (it's router-side, not
  // per-tenant in v6); we borrow it from the legacy block so the
  // tier render keeps working unchanged.
  const useTenantBlock =
    activeTenantId !== undefined &&
    activeTenantId !== "default" &&
    snap.cost_ladder_by_tenant !== undefined &&
    snap.cost_ladder_by_tenant[activeTenantId] !== undefined;
  const cl = useTenantBlock
    ? {
        ...snap.cost_ladder_by_tenant![activeTenantId!],
        model_default: snap.cost_ladder.model_default,
      }
    : snap.cost_ladder;
  // USD fields — "unknown" sentinel means the cost holder hasn't
  // initialized + we can't faithfully render. Fall back to
  // fan-out for cost rather than show misleading zeros.
  if (cl.spent_to_date_usd === "unknown") return null;
  // current_tier "unknown" can happen pre-router. Same call: don't
  // project; fan-out gives an accurate "not yet" via the live
  // holder's defaults.
  if (cl.current_tier === "unknown") return null;
  // Map snapshot's current_tier string → CostRung enum the FE
  // renders. Holder is the source-of-truth for these names
  // (agent/cost_state_holder.py CostRung).
  const KNOWN_RUNGS = new Set([
    "normal",
    "warn_75",
    "downshift_90",
    "hard_stop_100",
  ]);
  if (!KNOWN_RUNGS.has(cl.current_tier)) return null;
  // Map snapshot's model_default → ModelTier. The model_default
  // string may be a full model id (e.g. "claude-opus-4-7"); slim
  // to the tier word.
  let tier: "haiku" | "sonnet" | "opus" = "opus";
  const md = cl.model_default.toLowerCase();
  if (md === "unknown" || !md) return null;
  if (md.includes("haiku")) tier = "haiku";
  else if (md.includes("sonnet")) tier = "sonnet";
  else if (md.includes("opus")) tier = "opus";
  else return null;
  return {
    current: {
      // Period dates aren't carried in snapshot; CostCardBody
      // doesn't render them. Use the snapshot's computed_at as a
      // best-effort timestamp so consumers that DO read these get
      // a sensible date rather than the unix epoch.
      billing_period_start: snap.computed_at,
      billing_period_end: snap.computed_at,
      days_remaining: 0,
      credit_pool_usd: cl.credit_pool_usd,
      spent_to_date_usd: cl.spent_to_date_usd,
      burn_rate_usd_per_day: 0,
      projected_end_of_period_usd: 0,
      active_rung: cl.current_tier as CostStateResponse["current"]["active_rung"],
      active_rung_threshold_pct: 0,
      current_pct_used: cl.monthly_budget_pct_used ?? 0,
      effective_model_tier: tier,
      downshift_active: cl.current_tier === "downshift_90",
      downshift_reason: null,
      extra_usage_off: false,
    },
    rate_limit_pulse: {
      captured_at: snap.computed_at,
      requests: { limit: 0, remaining: 0, reset_at: snap.computed_at },
      tokens: { limit: 0, remaining: 0, reset_at: snap.computed_at },
    },
    deferred_tickets: [],
    reconciliation_history: [],
    stub: false,
  };
}

// KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — health projection from
// snapshot v4 daemon_health. Returns null when overall_status is
// "unknown" — caller falls back to fan-out.
//
// daemon_health doesn't separate control_plane / worker the way
// HealthRollupResponse does; the dashboard's HealthHero shows
// overall + control_plane + worker badges as one piece. Snapshot
// only knows overall, so we mirror it to both control_plane +
// worker (a deliberate "snapshot can't tell these apart yet"
// signal). The full HealthRollupPage panel keeps its fan-out
// path for the per-axis detail. Per spec §4: per-field
// granularity within the projection is acceptable when the card
// renders gracefully — the operator clicks through to the full
// panel for the breakdown anyway.
function projectHealthFromSnapshot(
  snap: SnapshotResponse,
): HealthRollupResponse | null {
  const dh = snap.daemon_health;
  if (!dh) return null;
  if (dh.overall_status === "unknown") return null;
  // Snapshot enum ("healthy" | "degraded" | "unhealthy") → FE
  // HealthStatus ("healthy" | "degraded" | "stopped" | "outage").
  // "unhealthy" maps to "outage" since the daemon is failing to
  // serve; the FE's outage tone is the right operator signal.
  const overall: HealthStatus =
    dh.overall_status === "healthy"
      ? "healthy"
      : dh.overall_status === "degraded"
        ? "degraded"
        : "outage";
  return {
    overall,
    // Snapshot doesn't distinguish control_plane vs worker yet;
    // mirror overall to both (semantic fidelity is preserved at
    // the hero level — operator click-through to /health-rollup
    // gives the full fan-out breakdown).
    control_plane: overall,
    worker: overall,
    stopped_reason: null,
    subsignals: {},
    stub: false,
  };
}

export default function DashboardPage() {
  usePanelView("DashboardPage");

  // KR-FE-TENANT-PICKER-COCKPIT-CHROME — route cost reads to the
  // operator-picked tenant. "All tenants" aggregate view falls back
  // to default for the live fan-out; the snapshot's per-tenant
  // block is rendered via projectCostFromSnapshot(snap, tenantId).
  const { activeTenant, isAllTenants } = useActiveTenant();
  const tenantForRead = isAllTenants ? "default" : activeTenant;

  const [data, setData] = useState<DashboardData>(INITIAL_DATA);
  const [refreshing, setRefreshing] = useState(false);
  // KR-FE-DASHBOARD-SNAPSHOT-WIRE: freshness-badge state. Tracks
  // whether we're on the $0 snapshot path vs live fan-out vs
  // unavailable-snapshot fallback, plus per-card live-override
  // count for the "mixed" sub-mode.
  const [snapshotMode, setSnapshotMode] = useState<
    "snapshot" | "live" | "unavailable"
  >("live"); // optimistic default; updated after first loadAll
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [liveAt, setLiveAt] = useState<string | null>(null);
  const [liveOverrides, setLiveOverrides] = useState<
    Set<keyof DashboardData>
  >(() => new Set());
  // Fields the snapshot projected (vs fanned out). Used to decide
  // whether a per-card retry counts as a "live override" — only
  // snapshot-projected fields can be overridden by live fetch.
  const [snapshotProjectedFields, setSnapshotProjectedFields] = useState<
    Set<keyof DashboardData>
  >(() => new Set());
  // KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — when the
  // operator picks "All tenants", the cost card renders aggregate
  // totals from snap.cost_ladder_by_tenant (the v6 sibling block
  // from #206). The dashboard fetches the snapshot already in
  // loadInitial — capture the block here so the cost card can
  // surface it without a second snapshot fetch.
  const [costLadderByTenant, setCostLadderByTenant] = useState<
    | Record<
        string,
        { spent_to_date_usd: number | "unknown"; credit_pool_usd: number }
      >
    | null
  >(null);
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
        // If this field was originally snapshot-projected and we're
        // now replacing it with a live fetch, count it as an
        // override for the badge's "mixed" sub-mode.
        setLiveOverrides((prev) => {
          if (!snapshotProjectedFields.has(key)) return prev;
          if (prev.has(key)) return prev;
          const next = new Set(prev);
          next.add(key);
          return next;
        });
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setData((prev) => ({
          ...prev,
          [key]: { state: "error", error: msg },
        }));
      }
    },
    [snapshotProjectedFields],
  );

  // Helper: full fan-out of every field. Used both as the initial
  // path when snapshot is unavailable AND as the force-refresh
  // path when the operator explicitly opts in to live data.
  const fanOutAll = useCallback(async () => {
    await Promise.allSettled([
      // Row 1 — v1 surfaces
      loadOne("health", () => api.getHealthRollup()),
      loadOne("operational", () => api.getOperationalState()),
      loadOne("cost", () => api.getCostState({ tenantId: tenantForRead })),
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
      // KR-MCP-3
      loadOne("mcpClients", () => api.getMCPClients()),
      // KR-WEBHOOK-EVENTS-PANEL
      loadOne("webhookEvents", () => api.getRecentWebhookEvents()),
      // KR-AGENT-ACTIVITY-PANEL
      loadOne("agentActivity", () => api.getRecentAgentActivity()),
      // KR-SLACK-DM-PANEL
      loadOne("slackDM", () => api.getRecentSlackDM()),
      // KR-EMAIL-PANEL
      loadOne("email", () => api.getRecentEmail()),
      // KR-REASONING-PANEL
      loadOne("reasoning", () => api.getRecentReasoning()),
      // KR-ALERTS-PANEL — drives the top-of-page banner
      loadOne("alerts", () => api.getCurrentAlerts()),
    ]);
  }, [loadOne, tenantForRead]);

  // Snapshot-first initial loader. Projects what the snapshot
  // covers cleanly, fans out everything else. Falls through to
  // full fan-out when snapshot is unavailable.
  const loadInitial = useCallback(async () => {
    try {
      const snap = await api.getSnapshot();
      if ("error" in snap) {
        // Snapshot unavailable — full fan-out fallback.
        setSnapshotMode("unavailable");
        setSnapshotAt(null);
        setSnapshotProjectedFields(new Set());
        setLiveOverrides(new Set());
        await fanOutAll();
        setLiveAt(new Date().toISOString());
        return;
      }

      // Project the fields we can cleanly map. Cost + health
      // project conditionally — null return ≡ "snapshot says
      // unknown, fan out for this field instead." Operational +
      // alerts always project (snapshot always carries their
      // shapes, fail-soft to empty defaults at the holder layer).
      const projectedCost = projectCostFromSnapshot(snap, tenantForRead);
      const projectedHealth = projectHealthFromSnapshot(snap);
      const projectedFields = new Set<keyof DashboardData>([
        "operational",
        "alerts",
      ]);
      if (projectedCost !== null) projectedFields.add("cost");
      if (projectedHealth !== null) projectedFields.add("health");
      setSnapshotMode("snapshot");
      setSnapshotAt(snap.computed_at);
      setSnapshotProjectedFields(projectedFields);
      setLiveOverrides(new Set());
      // KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — capture
      // the v6 by-tenant block for the aggregate cost-card body.
      // Empty object on single-tenant deployments (the block is
      // omitted on those daemons).
      setCostLadderByTenant(snap.cost_ladder_by_tenant ?? {});
      setData((prev) => ({
        ...prev,
        operational: {
          state: "ready",
          data: projectOperationalFromSnapshot(snap),
        },
        alerts: {
          state: "ready",
          data: projectAlertsFromSnapshot(snap),
        },
        ...(projectedCost !== null
          ? { cost: { state: "ready", data: projectedCost } as const }
          : {}),
        ...(projectedHealth !== null
          ? { health: { state: "ready", data: projectedHealth } as const }
          : {}),
      }));

      // Fan out everything NOT covered by the snapshot projection.
      // cost + health are conditional — fan out only if the snapshot
      // didn't have enough to project them.
      const remainingFetches: Promise<unknown>[] = [
        loadOne("sea", () => api.getKoraAssignedSeaTickets()),
        loadOne("control", () => api.getKoraControlObservedState()),
        loadOne("boot", () => api.getBootStatus()),
        loadOne("dr", () => api.getDRState()),
        loadOne("capabilities", () => api.getCapabilities()),
        loadOne("charter", () => api.getCharter()),
        loadOne("recentEvents", () => api.getChainEvents({ limit: 5 })),
        loadOne("runbooks", () => api.getRunbooks()),
        loadOne("heartbeat", () => api.getHeartbeatServices()),
        loadOne("mcpClients", () => api.getMCPClients()),
        loadOne("webhookEvents", () => api.getRecentWebhookEvents()),
        loadOne("agentActivity", () => api.getRecentAgentActivity()),
        loadOne("slackDM", () => api.getRecentSlackDM()),
        loadOne("email", () => api.getRecentEmail()),
        loadOne("reasoning", () => api.getRecentReasoning()),
      ];
      if (projectedCost === null) {
        remainingFetches.push(loadOne("cost", () => api.getCostState({ tenantId: tenantForRead })));
      }
      if (projectedHealth === null) {
        remainingFetches.push(loadOne("health", () => api.getHealthRollup()));
      }
      await Promise.allSettled(remainingFetches);
    } catch {
      // Snapshot fetch raised (network / 5xx). Treat same as
      // unavailable; full fan-out fallback.
      setSnapshotMode("unavailable");
      setSnapshotAt(null);
      setSnapshotProjectedFields(new Set());
      setLiveOverrides(new Set());
      await fanOutAll();
      setLiveAt(new Date().toISOString());
    }
    // tenantForRead: re-project + re-fan-out when operator switches
    // tenants (the snapshot's cost_ladder_by_tenant block is the
    // source of truth for the new view).
  }, [loadOne, fanOutAll, tenantForRead]);

  // Force-refresh path: bypass snapshot, do full live fan-out.
  // Triggered by the FreshnessBadge's Force-refresh button.
  const forceFullLiveRefresh = useCallback(async () => {
    setRefreshing(true);
    setSnapshotMode("live");
    setSnapshotAt(null);
    setSnapshotProjectedFields(new Set());
    setLiveOverrides(new Set());
    try {
      await fanOutAll();
      setLiveAt(new Date().toISOString());
      showToast("Dashboard refreshed (live fan-out)", "success");
    } finally {
      setRefreshing(false);
    }
  }, [fanOutAll, showToast]);

  // Hero-Reload button (existing top-right Reload above the grid).
  // Re-runs the snapshot-first path so the operator gets the $0
  // experience whenever they hit it — same as initial mount.
  const loadAll = useCallback(
    async (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      try {
        await loadInitial();
        if (isManual) showToast("Dashboard refreshed", "success");
      } finally {
        if (isManual) setRefreshing(false);
      }
    },
    [loadInitial, showToast],
  );

  useEffect(() => {
    void loadInitial();
  }, [loadInitial]);

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
    data.mcpClients,
    data.webhookEvents,
    data.agentActivity,
    data.slackDM,
    data.email,
    data.reasoning,
    data.alerts,
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

      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap">
          <H2>Kora — Overview</H2>
          {/* KR-FE-DASHBOARD-SNAPSHOT-WIRE: cost-economy thesis
              made visible. Badge shows whether this page-load was
              served from the $0 daemon snapshot or fanned out to
              live endpoints (cents), plus a Force-refresh button. */}
          <FreshnessBadge
            mode={snapshotMode}
            snapshotAt={snapshotAt}
            liveAt={liveAt}
            liveOverrideCount={liveOverrides.size}
            // KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — count the
            // originally-spec'd hero fields (operational + alerts
            // + cost + health = 4) that actually projected from
            // snapshot on this load. Drives "all 4 from snapshot"
            // vs "2 of 4 from snapshot" badge variants.
            snapshotProjectedHeroCount={
              (["operational", "alerts", "cost", "health"] as const).filter(
                (k) =>
                  snapshotProjectedFields.has(k) && !liveOverrides.has(k),
              ).length
            }
            totalHeroFields={DASHBOARD_HERO_FIELD_COUNT}
            onForceRefresh={() => void forceFullLiveRefresh()}
            refreshing={refreshing}
          />
        </div>
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

      {/* KR-ALERTS-PANEL banner — TOP placement per spec §1(c).
          Self-hides when no active alerts (no false-alarm trigger
          from absent data) and when operator dismissed for this tab. */}
      <AlertsBanner
        data={data.alerts.state === "ready" ? data.alerts.data : null}
      />

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
          title={isAllTenants ? "Cost (all tenants)" : "Cost"}
          icon={DollarSign}
          to="/cost-state"
          status={data.cost}
          stubbed={isStubbed(data.cost)}
          onRetry={() => void loadOne("cost", () => api.getCostState({ tenantId: tenantForRead }))}
        >
          {/* KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK —
              aggregate body when the operator picks All tenants;
              the full per-tenant side-by-side cards live on
              /cost-state (DashboardCard's `to` already points
              there). Falls back to the single-tenant body when
              the snapshot's by-tenant block isn't populated. */}
          {isAllTenants && costLadderByTenant !== null && Object.keys(costLadderByTenant).length > 0 ? (
            <AggregateCostCardBody byTenant={costLadderByTenant} />
          ) : (
            data.cost.state === "ready" && <CostCardBody data={data.cost.data} />
          )}
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
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
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

        <DashboardCard
          title="MCP Clients"
          icon={Cable}
          to="/mcp-clients"
          status={data.mcpClients}
          stubbed={isStubbed(data.mcpClients)}
          onRetry={() =>
            void loadOne("mcpClients", () => api.getMCPClients())
          }
        >
          {data.mcpClients.state === "ready" && (
            <MCPClientsCardBody data={data.mcpClients.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Webhook Events"
          icon={Inbox}
          to="/webhook-events"
          status={data.webhookEvents}
          stubbed={isStubbed(data.webhookEvents)}
          onRetry={() =>
            void loadOne("webhookEvents", () =>
              api.getRecentWebhookEvents(),
            )
          }
        >
          {data.webhookEvents.state === "ready" && (
            <WebhookEventsCardBody data={data.webhookEvents.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Agent Activity"
          icon={Workflow}
          to="/agent-activity"
          status={data.agentActivity}
          stubbed={isStubbed(data.agentActivity)}
          onRetry={() =>
            void loadOne("agentActivity", () =>
              api.getRecentAgentActivity(),
            )
          }
        >
          {data.agentActivity.state === "ready" && (
            <AgentActivityCardBody data={data.agentActivity.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Slack DM ↔ Joshua"
          icon={MessageCircle}
          to="/slack-dm"
          status={data.slackDM}
          stubbed={isStubbed(data.slackDM)}
          onRetry={() =>
            void loadOne("slackDM", () => api.getRecentSlackDM())
          }
        >
          {data.slackDM.state === "ready" && (
            <SlackDMCardBody data={data.slackDM.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Email ↔ Joshua"
          icon={Mail}
          to="/email"
          status={data.email}
          stubbed={isStubbed(data.email)}
          onRetry={() => void loadOne("email", () => api.getRecentEmail())}
        >
          {data.email.state === "ready" && (
            <EmailCardBody data={data.email.data} />
          )}
        </DashboardCard>

        <DashboardCard
          title="Reasoning"
          icon={Brain}
          to="/reasoning"
          status={data.reasoning}
          stubbed={isStubbed(data.reasoning)}
          onRetry={() =>
            void loadOne("reasoning", () => api.getRecentReasoning())
          }
        >
          {data.reasoning.state === "ready" && (
            <ReasoningCardBody data={data.reasoning.data} />
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
