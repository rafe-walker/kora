import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Globe,
  HelpCircle,
  Inbox,
  RefreshCw,
  ShieldX,
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
  WebhookEvent,
  WebhookEventStatus,
  WebhookEventsResponse,
} from "@/lib/api";

type StatusFilter = "all" | WebhookEventStatus;

const STATUS_TONE: Record<WebhookEventStatus, "success" | "warning" | "destructive"> = {
  verified: "success",
  dead_letter: "destructive",
  rate_limited: "warning",
  handler_error: "destructive",
};

const STATUS_LABEL: Record<WebhookEventStatus, string> = {
  verified: "verified",
  dead_letter: "dead letter",
  rate_limited: "rate limited",
  handler_error: "handler error",
};

function StatusIcon({ status }: { status: WebhookEventStatus }) {
  switch (status) {
    case "verified":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "dead_letter":
      return <ShieldX className="h-4 w-4 text-destructive" />;
    case "rate_limited":
      return <AlertTriangle className="h-4 w-4 text-warning" />;
    case "handler_error":
      return <AlertOctagon className="h-4 w-4 text-destructive" />;
  }
}

function shortEndpoint(endpoint: string): string {
  // "/api/webhooks/slack/events" → "/slack/events"
  // "/api/webhooks/email/inbound" → "/email/inbound"
  return endpoint.replace(/^\/api\/webhooks/, "");
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

interface EventRowProps {
  event: WebhookEvent;
  expanded: boolean;
  onToggle: () => void;
}

function EventRow({ event, expanded, onToggle }: EventRowProps) {
  const detailEntries = Object.entries(event.details);
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
          <StatusIcon status={event.status} />
          <Badge tone={STATUS_TONE[event.status]}>
            {STATUS_LABEL[event.status]}
          </Badge>
          <code className="text-xs font-mono">
            {shortEndpoint(event.endpoint)}
          </code>
          {event.event_type && (
            <span className="text-xs text-muted-foreground truncate">
              {event.event_type}
            </span>
          )}
          <span className="text-xs text-muted-foreground ml-auto flex items-center gap-3">
            <span className="flex items-center gap-1">
              <Globe className="h-3 w-3" />
              <code className="font-mono">{event.source_ip}</code>
            </span>
            <span className="flex items-center gap-1">
              <Clock className="h-3 w-3" />
              {formatRelative(event.received_at)}
            </span>
          </span>
        </button>

        {expanded && (
          <div className="ml-7 flex flex-col gap-1.5 pt-2 border-t border-border text-xs">
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
              <span>
                <code>id</code>: {event.id}
              </span>
              <span>
                <code>endpoint</code>: {event.endpoint}
              </span>
              <span>
                <code>received_at</code>: {formatTimestamp(event.received_at)}
              </span>
              <span>
                <code>source_ip</code>: {event.source_ip}{" "}
                <span className="italic">(octet-masked for PII)</span>
              </span>
            </div>
            <div className="flex flex-col gap-1">
              <span className="font-medium">Details</span>
              {detailEntries.length === 0 ? (
                <span className="text-muted-foreground">none</span>
              ) : (
                <pre className="rounded border border-border bg-background/60 p-2 overflow-x-auto text-[11px]">
                  {JSON.stringify(event.details, null, 2)}
                </pre>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const FILTER_OPTIONS: Array<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "verified", label: "Verified" },
  { value: "dead_letter", label: "Dead Letter" },
  { value: "rate_limited", label: "Rate Limited" },
  { value: "handler_error", label: "Handler Error" },
];

export default function WebhookEventsPanel() {
  const [data, setData] = useState<WebhookEventsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<StatusFilter>("all");
  const { toast, showToast } = useToast();

  const loadEvents = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getRecentWebhookEvents()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load webhook events: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadEvents(false);
  }, [loadEvents]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // FE-only filter — operator scopes the already-fetched list without
  // re-hitting the backend. Real-time tail isn't the goal here.
  const filteredEvents = useMemo(() => {
    if (!data) return [];
    if (filter === "all") return data.events;
    return data.events.filter((e) => e.status === filter);
  }, [data, filter]);

  // Aggregate counts for the stats strip.
  const counts = useMemo(() => {
    if (!data) return null;
    const c: Record<WebhookEventStatus, number> = {
      verified: 0,
      dead_letter: 0,
      rate_limited: 0,
      handler_error: 0,
    };
    for (const e of data.events) c[e.status]++;
    return c;
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
          <H2>Recent Webhook Events</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Public-port traffic on{" "}
            <code className="text-xs">/api/webhooks/*</code> — verified,
            dead-lettered, and rate-limited events.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadEvents(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load webhook events</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <Inbox className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — per-event recording wires in via CC#3 follow-on
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample data. CC#3 will add
                per-event chain-event recording OR a substrate{" "}
                <code>webhook_events</code> table (blocked on the
                substrate-team coord ask for the dead-letter ledger
                shape).
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && counts && (
        <>
          {/* ── Stats strip ─────────────────────────────────────── */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
              <Inbox className="h-4 w-4 text-primary" />
              <span className="font-medium">
                {data.total_recent_24h} event
                {data.total_recent_24h === 1 ? "" : "s"} in last 24h
              </span>
              <span className="flex items-center gap-1.5 text-xs">
                <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                {counts.verified} verified
              </span>
              <span className="flex items-center gap-1.5 text-xs">
                <ShieldX className="h-3.5 w-3.5 text-destructive" />
                {counts.dead_letter} dead-lettered
              </span>
              <span className="flex items-center gap-1.5 text-xs">
                <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                {counts.rate_limited} rate-limited
              </span>
              {counts.handler_error > 0 && (
                <span className="flex items-center gap-1.5 text-xs text-destructive">
                  <AlertOctagon className="h-3.5 w-3.5" />
                  {counts.handler_error} handler error
                </span>
              )}
              <span className="text-xs text-muted-foreground ml-auto italic">
                Source IPs octet-masked for PII.
              </span>
            </CardContent>
          </Card>

          {/* ── Filter pills ────────────────────────────────────── */}
          <div className="flex flex-wrap gap-1.5">
            {FILTER_OPTIONS.map((opt) => {
              const isActive = filter === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setFilter(opt.value)}
                  className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                    isActive
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground"
                  }`}
                  aria-pressed={isActive}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>

          {/* ── Events list (timeline, newest first) ────────────── */}
          {filteredEvents.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                <HelpCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
                {data.events.length === 0
                  ? "No webhook events yet. Public webhook plane is on port 9118; verify daemon is running."
                  : `No events matching filter "${filter}".`}
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {filteredEvents.map((event) => (
                <EventRow
                  key={event.id}
                  event={event}
                  expanded={expandedIds.has(event.id)}
                  onToggle={() => toggleExpand(event.id)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
