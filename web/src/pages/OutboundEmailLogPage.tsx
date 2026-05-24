// Outbound email audit log lens — KR-FE-OUTBOUND-EMAIL-LOG-PANEL.
//
// Symmetric to EmailIntentLogPage (PR #180) — surfaces the
// tool.email_to_operator_sent audit stream (PR #179, the
// kora__send_email_to_operator reasoning-loop tool). Completes
// the cockpit's email-surface story: inbound (Email Intent Log)
// + outbound (this page) both visible.
//
// PRIVACY: body text + subject string are NEVER in the audit
// payload (PR #179's hard-coded posture). This panel renders
// only sizes (subject_chars / body_chars / attachment counts) +
// status + smtp_message_id (on success) or rejection_reason +
// truncated rejection_detail (on rejection) or error type (on
// smtp_failure). No reconstruction of text content is possible.
//
// Layout copied from EmailIntentLogPage (PR #180) per spec §4
// "copy first, refactor later" — see PR description for shared-
// utility extraction recommendation (Sparkline, FilterChips,
// SummaryChips, formatTimestamp/formatRelative helpers).

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronsUp,
  Paperclip,
  RefreshCw,
  Send,
  ServerCrash,
  Sparkles,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import {
  OUTBOUND_EMAIL_STATUS_VALUES,
  type OutboundEmailDailyCount,
  type OutboundEmailEvent,
  type OutboundEmailEventsResponse,
  type OutboundEmailStatus,
} from "@/lib/api";

type BadgeTone =
  | "default"
  | "destructive"
  | "outline"
  | "secondary"
  | "success"
  | "warning";

type FilterValue = "all" | OutboundEmailStatus;

interface StatusVisual {
  label: string;
  tone: BadgeTone;
  Icon: typeof CheckCircle2;
}

const STATUS_VISUALS: Record<OutboundEmailStatus, StatusVisual> = {
  sent: { label: "Sent", tone: "success", Icon: CheckCircle2 },
  rejected: { label: "Rejected", tone: "warning", Icon: Ban },
  smtp_failure: {
    label: "SMTP-failure",
    tone: "destructive",
    Icon: ServerCrash,
  },
  unknown: { label: "Unknown", tone: "outline", Icon: AlertTriangle },
};

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function formatRelative(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    return `${Math.floor(hr / 24)}d ago`;
  } catch {
    return iso;
  }
}

// Human-readable size formatter. Audit row gives raw counts of
// chars (subject + body) and bytes (attachments); render at the
// scale that fits.
function formatChars(n: number): string {
  if (n < 1000) return `${n} chars`;
  return `${(n / 1000).toFixed(1)}k chars`;
}

function formatBytes(n: number): string {
  if (n === 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

// ----- Sparkline (plain SVG, no chart-library dep — same
//       discipline as EmailIntentLogPage + CostTelemetryPage) -----

interface SparklineProps {
  points: OutboundEmailDailyCount[];
  width?: number;
  height?: number;
}

function Sparkline({ points, width = 220, height = 36 }: SparklineProps) {
  if (points.length === 0) return null;
  const max = Math.max(1, ...points.map((p) => p.count));
  const barWidth = Math.max(2, (width - (points.length - 1) * 1) / points.length);
  const total = points.reduce((acc, p) => acc + p.count, 0);
  return (
    <div className="flex items-center gap-2">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        aria-label={`Daily 'sent' counts over the last ${points.length} days`}
        role="img"
      >
        {points.map((p, i) => {
          const x = i * (barWidth + 1);
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
              <title>{`${p.date}: ${p.count} sent`}</title>
            </rect>
          );
        })}
      </svg>
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {total} sent · 14d
      </span>
    </div>
  );
}

// ----- Summary chips -----

interface SummaryChipsProps {
  byStatus: Record<string, number>;
  total: number;
}

function SummaryChips({ byStatus, total }: SummaryChipsProps) {
  return (
    <div className="flex items-center gap-3 flex-wrap text-sm">
      <span>
        <strong>{total}</strong>{" "}
        <span className="text-muted-foreground">composed · last 24h</span>
      </span>
      {total > 0 && (
        <span className="text-muted-foreground">·</span>
      )}
      {OUTBOUND_EMAIL_STATUS_VALUES.map((status) => {
        const count = byStatus[status] ?? 0;
        if (count === 0) return null;
        const v = STATUS_VISUALS[status];
        return (
          <div
            key={status}
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
  const totalAll = OUTBOUND_EMAIL_STATUS_VALUES.reduce(
    (acc, s) => acc + (counts[s] ?? 0),
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
      {OUTBOUND_EMAIL_STATUS_VALUES.map((status) => {
        const v = STATUS_VISUALS[status];
        const count = counts[status] ?? 0;
        const active = current === status;
        return (
          <button
            key={status}
            onClick={() => onChange(status)}
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

function EventCard({ event }: { event: OutboundEmailEvent }) {
  const v = STATUS_VISUALS[event.status];
  return (
    <Card>
      <CardContent className="p-3 flex items-start gap-3">
        <Send className="h-4 w-4 mt-0.5 text-muted-foreground flex-shrink-0" />
        <div className="flex-1 min-w-0 flex flex-col gap-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-xs text-muted-foreground"
              title={formatTimestamp(event.emitted_at)}
            >
              {formatRelative(event.emitted_at)}
            </span>
            <span className="text-sm font-medium">
              Email to operator
            </span>
            <Badge tone={v.tone}>
              <v.Icon className="h-3 w-3 mr-1 inline" />
              {v.label}
            </Badge>
          </div>
          {/* Privacy-preserved size row: subject_chars + body_chars
              + attachments. No text content — only counts. */}
          <div className="flex items-center gap-2 flex-wrap text-xs text-muted-foreground">
            <span title="subject character count (subject string is NOT in audit)">
              subject: {event.subject_chars} chars
            </span>
            <span>·</span>
            <span title="body character count (body content is NOT in audit)">
              body: {formatChars(event.body_chars)}
            </span>
            {event.attachment_count > 0 && (
              <>
                <span>·</span>
                <span className="inline-flex items-center gap-1">
                  <Paperclip className="h-3 w-3" />
                  {event.attachment_count} attachment
                  {event.attachment_count === 1 ? "" : "s"} ·{" "}
                  {formatBytes(event.attachment_total_bytes)}
                </span>
              </>
            )}
          </div>
          {/* Per-status detail row. */}
          {event.status === "sent" && (
            <div className="flex items-center gap-2 flex-wrap text-xs">
              {event.sent_at && (
                <span
                  className="text-muted-foreground"
                  title={`sent_at: ${event.sent_at}`}
                >
                  sent {formatRelative(event.sent_at)}
                </span>
              )}
              {event.smtp_message_id && (
                <span className="font-mono text-[10px] text-muted-foreground truncate"
                      title={event.smtp_message_id}>
                  SMTP-id: {truncate(event.smtp_message_id, 60)}
                </span>
              )}
            </div>
          )}
          {event.status === "rejected" && (
            <div className="flex flex-col gap-1 text-xs">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-yellow-500 inline-flex items-center gap-1">
                  <ChevronsUp className="h-3 w-3" />
                  reason: <span className="font-mono">{event.rejection_reason || "(unknown)"}</span>
                </span>
              </div>
              {event.rejection_detail && (
                <div className="font-mono text-[10px] text-muted-foreground break-words">
                  detail: {event.rejection_detail}
                </div>
              )}
            </div>
          )}
          {event.status === "smtp_failure" && (
            <div className="text-xs text-destructive font-mono break-words">
              {event.error || "(unknown error)"}
              {event.smtp_status && (
                <span className="text-muted-foreground ml-2">
                  (smtp_status: {event.smtp_status})
                </span>
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ----- Page -----

export default function OutboundEmailLogPage() {
  usePanelView("OutboundEmailLogPage");

  const [data, setData] = useState<OutboundEmailEventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterValue>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getOutboundEmailRecent();
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
    return data.events.filter((e) => e.status === filter);
  }, [data, filter]);

  return (
    <div className="space-y-4 p-4 max-w-6xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2>Outbound Email Log</H2>
        <Button outlined size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw
            className={`h-3 w-3 mr-1 ${loading ? "animate-spin" : ""}`}
          />
          Refresh
        </Button>
      </div>

      <p className="text-sm text-muted-foreground">
        Audit stream of emails Kora composed via the{" "}
        <span className="font-mono">kora__send_email_to_operator</span> tool
        (PR #179). <strong>Privacy-preserved:</strong> subject + body text
        are NOT recorded — only sizes, status, and triage metadata. Pairs
        with the inbound <span className="font-mono">/email-intent-log</span>{" "}
        panel for full cockpit visibility into Kora's email surface.
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
              Failed to load outbound email events:{" "}
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
                byStatus={data.by_status_24h}
                total={data.total_recent_24h}
              />
              <Sparkline points={data.daily_sent_14d} />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-3">
              <FilterChips
                current={filter}
                counts={data.by_status_24h}
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
                    ? "No outbound email events recorded yet"
                    : `No "${STATUS_VISUALS[filter as OutboundEmailStatus]?.label}" events match this filter in the current window`}
                </H2>
                <p className="text-sm text-muted-foreground max-w-md">
                  {filter === "all" ? (
                    <>
                      The tool.email_to_operator_sent audit stream is empty
                      for now. This page will populate when Kora composes
                      and sends email via her reasoning loop.
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
