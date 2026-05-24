// Email-intent audit log lens — KR-FE-EMAIL-INTENT-LOG-PANEL.
//
// Surfaces the intent.email_to_sea_ticket audit stream (PR #176)
// so operator can see — at a glance — which inbound emails Kora
// converted to Sea_Tickets, which were logged-only (future
// promotion-loop training data), and which failed (triage).
//
// Action filter chips: All / Created / Logged-only / Failed /
//   Dry-run / Cap-exceeded. Chip order matches the operator
//   mental model: success → training-data → failure → diagnostic.
//
// Sparkline of daily 'created' counts (last 14 days): plain SVG,
// no chart-library dep — same discipline as CostTelemetryPage's
// in-house charts (CC#2 established this in PR #164).
//
// Deep-link from created-action rows → /sea-tickets?focus=<ticket_id>.
// SeaTicketsPage doesn't currently consume `focus`; the param is
// forward-compatible so a follow-on bucket can teach the page to
// scroll/highlight without requiring a re-write of this panel.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  Ban,
  CheckCircle2,
  Clock,
  ExternalLink,
  Inbox,
  Mail,
  RefreshCw,
  Sparkles,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import {
  EMAIL_INTENT_ACTION_VALUES,
  type EmailIntentAction,
  type EmailIntentDailyCount,
  type EmailIntentEvent,
  type EmailIntentEventsResponse,
} from "@/lib/api";

// nous-research/ui Badge tones — keep in sync with the library's
// discriminated union (node_modules/@nous-research/ui/dist/ui/
// components/badge.d.ts). "outline" is the neutral chip.
type BadgeTone =
  | "default"
  | "destructive"
  | "outline"
  | "secondary"
  | "success"
  | "warning";

// Filter chip values — "all" is the FE-only sentinel that means
// "don't filter." Other values must come from EMAIL_INTENT_ACTION_VALUES
// (the drift-guarded canonical list).
type FilterValue = "all" | EmailIntentAction;

interface ActionVisual {
  label: string;
  tone: BadgeTone;
  Icon: typeof CheckCircle2;
  // Color used in the sparkline + summary chips. Library tones
  // don't expose raw colors; mirror them as Tailwind classes for
  // the SVG fill.
  fillClass: string;
}

const ACTION_VISUALS: Record<EmailIntentAction, ActionVisual> = {
  created: {
    label: "Created",
    tone: "success",
    Icon: CheckCircle2,
    fillClass: "fill-green-500",
  },
  logged_only: {
    label: "Logged-only",
    tone: "outline",
    Icon: Inbox,
    fillClass: "fill-muted-foreground",
  },
  dry_run: {
    label: "Dry-run",
    tone: "secondary",
    Icon: Clock,
    fillClass: "fill-blue-500",
  },
  cap_exceeded: {
    label: "Cap-exceeded",
    tone: "warning",
    Icon: Ban,
    fillClass: "fill-yellow-500",
  },
  failed: {
    label: "Failed",
    tone: "destructive",
    Icon: XCircle,
    fillClass: "fill-destructive",
  },
  unknown: {
    label: "Unknown",
    tone: "outline",
    Icon: AlertTriangle,
    fillClass: "fill-muted-foreground",
  },
};

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function formatRelative(iso: string): string {
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const day = Math.floor(hr / 24);
    return `${day}d ago`;
  } catch {
    return iso;
  }
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

// ----- Sparkline (plain SVG, no chart-library dep) -----

interface SparklineProps {
  points: EmailIntentDailyCount[];
  width?: number;
  height?: number;
}

function Sparkline({ points, width = 220, height = 36 }: SparklineProps) {
  if (points.length === 0) return null;
  const max = Math.max(1, ...points.map((p) => p.count));
  // Layout: bars (one per day) with 1px gap. Bar width derived
  // from container width; bars rendered as rects.
  const barWidth = Math.max(2, (width - (points.length - 1) * 1) / points.length);
  const total = points.reduce((acc, p) => acc + p.count, 0);

  return (
    <div className="flex items-center gap-2">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        aria-label={`Daily 'created' counts over the last ${points.length} days`}
        role="img"
      >
        {points.map((p, i) => {
          const x = i * (barWidth + 1);
          // Empty days render as 1px-tall ghost bar so operator
          // sees the day-grid position; non-empty bars scale
          // linearly to height.
          const h = p.count === 0 ? 1 : Math.max(2, (p.count / max) * height);
          const y = height - h;
          return (
            <rect
              key={p.date}
              x={x}
              y={y}
              width={barWidth}
              height={h}
              className={p.count === 0 ? "fill-muted/40" : "fill-green-500"}
            >
              <title>{`${p.date}: ${p.count} created`}</title>
            </rect>
          );
        })}
      </svg>
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {total} created · 14d
      </span>
    </div>
  );
}

// ----- Summary chips (by-action counts in the 24h window) -----

interface SummaryChipsProps {
  byAction: Record<string, number>;
  total: number;
}

function SummaryChips({ byAction, total }: SummaryChipsProps) {
  return (
    <div className="flex items-center gap-3 flex-wrap text-sm">
      <span>
        <strong>{total}</strong>{" "}
        <span className="text-muted-foreground">events · last 24h</span>
      </span>
      {total > 0 && (
        <span className="text-muted-foreground">·</span>
      )}
      {EMAIL_INTENT_ACTION_VALUES.map((action) => {
        const count = byAction[action] ?? 0;
        if (count === 0) return null;
        const v = ACTION_VISUALS[action];
        return (
          <div
            key={action}
            className="flex items-center gap-1 text-xs"
            title={`${count} ${v.label.toLowerCase()} in last 24h`}
          >
            <v.Icon
              className={`h-3 w-3 ${
                v.tone === "success"
                  ? "text-green-500"
                  : v.tone === "destructive"
                    ? "text-destructive"
                    : v.tone === "warning"
                      ? "text-yellow-500"
                      : "text-muted-foreground"
              }`}
            />
            <span className="font-medium">{count}</span>
            <span className="text-muted-foreground">{v.label.toLowerCase()}</span>
          </div>
        );
      })}
    </div>
  );
}

// ----- Filter chips -----

interface FilterChipsProps {
  current: FilterValue;
  counts: Record<string, number>;
  onChange: (next: FilterValue) => void;
}

function FilterChips({ current, counts, onChange }: FilterChipsProps) {
  const totalAll = EMAIL_INTENT_ACTION_VALUES.reduce(
    (acc, a) => acc + (counts[a] ?? 0),
    0,
  );
  return (
    <div className="flex items-center gap-1 flex-wrap">
      <button
        onClick={() => onChange("all")}
        className={`px-2.5 py-1 text-xs rounded-md border transition-colors ${
          current === "all"
            ? "bg-primary text-primary-foreground border-primary"
            : "border-border hover:bg-accent"
        }`}
        aria-pressed={current === "all"}
      >
        All <span className="opacity-70">({totalAll})</span>
      </button>
      {EMAIL_INTENT_ACTION_VALUES.map((action) => {
        const v = ACTION_VISUALS[action];
        const count = counts[action] ?? 0;
        const active = current === action;
        return (
          <button
            key={action}
            onClick={() => onChange(action)}
            className={`px-2.5 py-1 text-xs rounded-md border transition-colors inline-flex items-center gap-1 ${
              active
                ? "bg-primary text-primary-foreground border-primary"
                : "border-border hover:bg-accent"
            }`}
            aria-pressed={active}
          >
            <v.Icon className="h-3 w-3" />
            {v.label} <span className="opacity-70">({count})</span>
          </button>
        );
      })}
    </div>
  );
}

// ----- Per-row card -----

function EventCard({ event }: { event: EmailIntentEvent }) {
  const v = ACTION_VISUALS[event.action];
  const hasTicket = event.action === "created" && event.ticket_id;
  return (
    <Card>
      <CardContent className="p-3 flex items-start gap-3">
        <Mail className="h-4 w-4 mt-0.5 text-muted-foreground flex-shrink-0" />
        <div className="flex-1 min-w-0 flex flex-col gap-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-xs text-muted-foreground"
              title={formatTimestamp(event.emitted_at)}
            >
              {formatRelative(event.emitted_at)}
            </span>
            <span className="text-sm font-medium truncate" title={event.subject}>
              {truncate(event.subject || "(no subject)", 70)}
            </span>
          </div>
          <div className="flex items-center gap-2 flex-wrap text-xs">
            <Badge tone="outline" className="font-mono">
              {event.pattern_matched || "(no pattern)"}
            </Badge>
            <span className="text-muted-foreground">
              confidence: {event.confidence || "(none)"}
            </span>
            <Badge tone={v.tone}>
              <v.Icon className="h-3 w-3 mr-1 inline" />
              {v.label}
            </Badge>
            {hasTicket && (
              <Link
                to={`/sea-tickets?focus=${encodeURIComponent(event.ticket_id!)}`}
                className="inline-flex items-center gap-1 text-primary hover:underline"
                title={`Open Sea_Tickets panel (forward-compatible focus param: ${event.ticket_id})`}
              >
                <ExternalLink className="h-3 w-3" />
                {event.ticket_id}
              </Link>
            )}
            {event.action === "cap_exceeded" && event.hourly_cap !== undefined && (
              <span className="text-muted-foreground italic">
                hourly cap: {event.hourly_cap ?? "?"}
              </span>
            )}
            {event.action === "logged_only" && event.reason && (
              <span className="text-muted-foreground italic">
                reason: {event.reason}
              </span>
            )}
            {event.action === "dry_run" && event.proposed_title && (
              <span className="text-muted-foreground italic">
                proposed: {truncate(event.proposed_title, 50)}
              </span>
            )}
          </div>
          {event.action === "failed" && event.error && (
            <div className="text-xs text-destructive font-mono break-words">
              {event.error}
            </div>
          )}
          {event.tags && event.tags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-0.5">
              {event.tags.map((t) => (
                <Badge key={t} tone="outline" className="text-[10px]">
                  {t}
                </Badge>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ----- Page -----

export default function EmailIntentLogPage() {
  usePanelView("EmailIntentLogPage");

  const [data, setData] = useState<EmailIntentEventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterValue>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getEmailIntentEventsRecent();
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filteredEvents = useMemo(() => {
    if (data === null) return [];
    if (filter === "all") return data.events;
    return data.events.filter((e) => e.action === filter);
  }, [data, filter]);

  return (
    <div className="space-y-4 p-4 max-w-6xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2>Email Intent Log</H2>
        <Button outlined size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw
            className={`h-3 w-3 mr-1 ${loading ? "animate-spin" : ""}`}
          />
          Refresh
        </Button>
      </div>

      <p className="text-sm text-muted-foreground">
        Audit stream of inbound emails Kora evaluated for Sea_Ticket
        conversion (PR #176 KR-INTENT-EMAIL-TO-SEA-TICKET). The{" "}
        <span className="font-mono">logged_only</span> action is the
        future promotion-loop training-data lens; {" "}
        <span className="font-mono">failed</span> is the triage surface.
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
              Failed to load email-intent events:{" "}
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
                byAction={data.by_action_24h}
                total={data.total_recent_24h}
              />
              <Sparkline points={data.daily_created_14d} />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-3">
              <FilterChips
                current={filter}
                counts={data.by_action_24h}
                onChange={setFilter}
              />
            </CardContent>
          </Card>

          {filteredEvents.length === 0 ? (
            <Card className="border-green-500/30 bg-green-500/5">
              <CardContent className="p-8 flex flex-col items-center text-center gap-3">
                <Sparkles className="h-7 w-7 text-green-500" />
                <H2 className="text-base">
                  {filter === "all"
                    ? "No email-intent events recorded yet"
                    : `No "${ACTION_VISUALS[filter as EmailIntentAction]?.label}" events match this filter in the current window`}
                </H2>
                <p className="text-sm text-muted-foreground max-w-md">
                  {filter === "all" ? (
                    <>
                      The intent.email_to_sea_ticket audit stream is empty
                      for now. This page will populate when Kora evaluates
                      inbound emails for Sea_Ticket conversion.
                    </>
                  ) : (
                    <>
                      Try switching to a different filter, or click{" "}
                      <button
                        onClick={() => setFilter("all")}
                        className="underline text-primary"
                      >
                        All
                      </button>{" "}
                      to see every event.
                    </>
                  )}
                </p>
              </CardContent>
            </Card>
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
