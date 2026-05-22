import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Radio,
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
import type { ChainEvent, ChainEventsResponse } from "@/lib/api";

// Built-in prefix presets sourced from the 33+ kora.* event_type
// literals shipped on the substrate side (foundation/0159). Picking
// these by category covers ~90% of operator investigation cases.
const PREFIX_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "kora.", label: "kora.* (all)" },
  { value: "kora.boot.", label: "kora.boot.*" },
  { value: "kora.operational_state.", label: "kora.operational_state.*" },
  { value: "kora.constitution.", label: "kora.constitution.*" },
  { value: "kora.sea_ticket.", label: "kora.sea_ticket.*" },
  { value: "kora.cron.", label: "kora.cron.*" },
  { value: "kora.dr.", label: "kora.dr.*" },
  { value: "kora.cost.", label: "kora.cost.*" },
  { value: "kora.chain.", label: "kora.chain.*" },
  { value: "kora.skill.", label: "kora.skill.*" },
  { value: "kora.session.", label: "kora.session.*" },
];

const DEFAULT_PREFIX = "kora.";
const DEFAULT_LIMIT = 100;

function familyOf(eventType: string): string {
  // "kora.operational_state.transitioned" → "operational_state"
  const parts = eventType.split(".");
  if (parts.length >= 2) return parts[1];
  return eventType;
}

// Color-tone families so operators can scan the table by category.
// Map common families to distinct tones; everything else falls through
// to outline. Avoids running out of distinct tones at 33+ families.
const FAMILY_TONE: Record<string, "success" | "warning" | "destructive" | "outline"> = {
  boot: "success",
  operational_state: "outline",
  constitution: "outline",
  cron: "outline",
  session: "outline",
  skill: "outline",
  chain: "outline",
  sea_ticket: "warning",
  cost: "warning",
  dr: "destructive",
  control: "destructive",
};

function familyTone(eventType: string): "success" | "warning" | "destructive" | "outline" {
  return FAMILY_TONE[familyOf(eventType)] ?? "outline";
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

function truncateMiddle(value: string, head: number): string {
  if (!value) return "—";
  if (value.length <= head + 4) return value;
  return `${value.slice(0, head)}…${value.slice(-4)}`;
}

function payloadPreview(payload: Record<string, unknown>): string {
  try {
    const json = JSON.stringify(payload);
    return json.length > 80 ? json.slice(0, 80) + "…" : json;
  } catch {
    return "<unrenderable payload>";
  }
}

interface ChainEventRowProps {
  event: ChainEvent;
  expanded: boolean;
  onToggle: () => void;
}

function ChainEventRowDisplay({ event, expanded, onToggle }: ChainEventRowProps) {
  const family = familyOf(event.event_type);
  return (
    <>
      <tr
        className="border-b last:border-0 align-top hover:bg-muted/30 cursor-pointer"
        onClick={onToggle}
      >
        <td className="py-2 pr-3 text-xs">
          <div className="flex items-center gap-1">
            {expanded ? (
              <ChevronDown className="h-3 w-3 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3 w-3 text-muted-foreground" />
            )}
            <div>
              <div>{formatRelative(event.occurred_at)}</div>
              <div className="text-muted-foreground">
                {formatTimestamp(event.occurred_at)}
              </div>
            </div>
          </div>
        </td>
        <td className="py-2 pr-3">
          <Badge tone={familyTone(event.event_type)}>{family}</Badge>
          <div className="text-xs font-mono mt-1 break-all">
            {event.event_type}
          </div>
        </td>
        <td className="py-2 pr-3 text-xs font-mono">
          <span title={event.actor_id ?? ""}>
            {truncateMiddle(event.actor_id ?? "—", 8)}
          </span>
        </td>
        <td className="py-2 text-xs text-muted-foreground font-mono">
          {payloadPreview(event.payload)}
        </td>
      </tr>
      {expanded && (
        <tr className="border-b last:border-0 bg-muted/20">
          <td colSpan={4} className="py-3 px-4">
            <div className="flex flex-col gap-2 text-xs">
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
                <span>
                  <code>event_id</code>: {event.event_id}
                </span>
                {event.actor_id && (
                  <span>
                    <code>actor_id</code>: {event.actor_id}
                  </span>
                )}
                <span>
                  <code>workspace_id</code>: {event.workspace_id}
                </span>
              </div>
              {event.envelope && Object.keys(event.envelope).length > 0 && (
                <div className="flex flex-col gap-1">
                  <span className="font-medium">Envelope</span>
                  <pre className="rounded border border-border bg-background/60 p-2 overflow-x-auto text-[11px]">
                    {JSON.stringify(event.envelope, null, 2)}
                  </pre>
                </div>
              )}
              <div className="flex flex-col gap-1">
                <span className="font-medium">Payload</span>
                <pre className="rounded border border-border bg-background/60 p-2 overflow-x-auto text-[11px] max-h-96">
                  {JSON.stringify(event.payload, null, 2)}
                </pre>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export default function ChainEventsPage() {
  const [prefix, setPrefix] = useState<string>(DEFAULT_PREFIX);
  const [events, setEvents] = useState<ChainEvent[]>([]);
  const [nextBeforeTs, setNextBeforeTs] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [response, setResponse] = useState<ChainEventsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);
  const { toast, showToast } = useToast();

  const loadFresh = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      setExpandedIds(new Set());
      api
        .getChainEvents({ prefix, limit: DEFAULT_LIMIT })
        .then((resp) => {
          setResponse(resp);
          setEvents(resp.events);
          setNextBeforeTs(resp.next_before_ts);
          setHasMore(resp.next_before_ts !== null && resp.events.length > 0);
          setLastRefreshedAt(new Date().toISOString());
        })
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load chain events: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [prefix, showToast],
  );

  const loadOlder = useCallback(() => {
    if (!nextBeforeTs || !hasMore) return;
    setLoadingOlder(true);
    api
      .getChainEvents({
        prefix,
        limit: DEFAULT_LIMIT,
        before_ts: nextBeforeTs,
      })
      .then((resp) => {
        if (resp.events.length === 0) {
          setHasMore(false);
          return;
        }
        setEvents((prev) => [...prev, ...resp.events]);
        setNextBeforeTs(resp.next_before_ts);
        setHasMore(resp.next_before_ts !== null);
      })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        showToast(`Failed to load older events: ${msg}`, "error");
      })
      .finally(() => setLoadingOlder(false));
  }, [hasMore, nextBeforeTs, prefix, showToast]);

  // Reload fresh when prefix changes — paging position would be
  // meaningless across a filter switch.
  useEffect(() => {
    loadFresh(false);
  }, [loadFresh]);

  const toggleExpand = useCallback((eventId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  }, []);

  const isStub = useMemo(() => response?.stub === true, [response]);
  const stubError = response?.error;

  if (response === null && !loadError) {
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
          <H2>Chain Events</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Live tail of Kora's audit stream — last {DEFAULT_LIMIT} events.
          </p>
        </div>
        <Button
          size="sm"
          ghost
          disabled={refreshing}
          onClick={() => loadFresh(true)}
        >
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load chain events</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {isStub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <AlertTriangle className="h-4 w-4 mt-0.5 text-warning shrink-0" />
            <div>
              <div className="font-medium">Event log unavailable</div>
              <div className="text-xs text-muted-foreground mt-0.5">
                {stubError ?? "The substrate event_log read returned a stub fallback."}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── Filter bar ─────────────────────────────────────────── */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-center gap-3">
          <Radio className="h-4 w-4 text-primary" />
          <span className="text-sm font-medium">Filter:</span>
          <select
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
            className="rounded border border-border bg-background px-2 py-1 text-xs"
          >
            {PREFIX_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <span className="text-xs text-muted-foreground ml-auto">
            {events.length} event{events.length === 1 ? "" : "s"} shown
          </span>
        </CardContent>
      </Card>

      {/* ── Events table ──────────────────────────────────────── */}
      {events.length === 0 && !isStub && !loadError ? (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No events matching <code>{prefix}*</code> in recent history.
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="py-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-muted-foreground border-b">
                  <th className="py-2 pr-3 font-medium">Time</th>
                  <th className="py-2 pr-3 font-medium">Event type</th>
                  <th className="py-2 pr-3 font-medium">Actor</th>
                  <th className="py-2 font-medium">Payload preview</th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <ChainEventRowDisplay
                    key={event.event_id}
                    event={event}
                    expanded={expandedIds.has(event.event_id)}
                    onToggle={() => toggleExpand(event.event_id)}
                  />
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {/* ── Pagination ────────────────────────────────────────── */}
      {hasMore && events.length > 0 && (
        <div className="flex justify-center">
          <Button
            size="sm"
            outlined
            disabled={loadingOlder}
            onClick={loadOlder}
          >
            {loadingOlder ? (
              <>
                <RefreshCw className="h-3 w-3 animate-spin" />
                Loading…
              </>
            ) : (
              <>Load older</>
            )}
          </Button>
        </div>
      )}

      {!hasMore && events.length > 0 && (
        <div className="text-center text-xs text-muted-foreground">
          No more older events.
        </div>
      )}

      {lastRefreshedAt && (
        <div className="text-center text-xs text-muted-foreground">
          Last refreshed {formatRelative(lastRefreshedAt)} (
          {formatTimestamp(lastRefreshedAt)})
        </div>
      )}
    </div>
  );
}
