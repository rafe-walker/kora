// Outbound email audit log lens — KR-FE-OUTBOUND-EMAIL-LOG-PANEL.
//
// Symmetric to EmailIntentLogPage (PR #180) — surfaces the
// tool.email_to_operator_sent audit stream (PR #179, the
// kora__send_email_to_operator reasoning-loop tool).
//
// PRIVACY: body text + subject string are NEVER in the audit
// payload (PR #179's hard-coded posture). This panel renders
// only sizes (subject_chars / body_chars / attachment counts) +
// status + smtp_message_id (on success) or rejection_reason +
// truncated rejection_detail (on rejection) or error type (on
// smtp_failure). No reconstruction of text content is possible.
//
// Retrofit (KR-FE-PANEL-KIT-AND-MUTATING-ACTIONS-MEGABUCKET):
// Sparkline / SummaryChips / FilterChips / formatters /
// BadgeTone / EmptyFilteredMessage now live in AuditPanelKit.

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
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { useActiveTenant } from "@/hooks/useActiveTenant";
import { ActiveTenantBadge } from "@/components/ActiveTenantBadge";
import { api } from "@/lib/api";
import {
  OUTBOUND_EMAIL_STATUS_VALUES,
  type OutboundEmailEvent,
  type OutboundEmailEventsResponse,
  type OutboundEmailStatus,
} from "@/lib/api";
import {
  EmptyFilteredMessage,
  FilterChips,
  Sparkline,
  SummaryChips,
  formatBytes,
  formatChars,
  formatRelative,
  formatTimestamp,
  truncate,
  type CategoryDef,
  type FilterValue,
} from "@/components/AuditPanelKit";

const OUTBOUND_EMAIL_CATEGORIES: readonly CategoryDef<OutboundEmailStatus>[] = [
  { key: "sent", label: "Sent", tone: "success", Icon: CheckCircle2 },
  { key: "rejected", label: "Rejected", tone: "warning", Icon: Ban },
  {
    key: "smtp_failure",
    label: "SMTP-failure",
    tone: "destructive",
    Icon: ServerCrash,
  },
];

const OUTBOUND_EMAIL_CATEGORY_MAP: Record<
  OutboundEmailStatus,
  CategoryDef<OutboundEmailStatus>
> = {
  sent: OUTBOUND_EMAIL_CATEGORIES[0],
  rejected: OUTBOUND_EMAIL_CATEGORIES[1],
  smtp_failure: OUTBOUND_EMAIL_CATEGORIES[2],
  unknown: {
    key: "sent" as OutboundEmailStatus, // sentinel
    label: "Unknown",
    tone: "outline",
    Icon: AlertTriangle,
  },
};

// ----- Per-row card -----

function EventCard({ event }: { event: OutboundEmailEvent }) {
  const v = OUTBOUND_EMAIL_CATEGORY_MAP[event.status];
  const Icon = v.Icon;
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
              <Icon className="h-3 w-3 mr-1 inline" />
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

  const { activeTenant, isAllTenants } = useActiveTenant();
  const tenantForRead = isAllTenants ? undefined : activeTenant;

  const [data, setData] = useState<OutboundEmailEventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterValue<OutboundEmailStatus>>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getOutboundEmailRecent({ tenantId: tenantForRead });
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [tenantForRead]);

  useEffect(() => {
    void load();
  }, [load]);

  const filteredEvents = useMemo(() => {
    if (data === null) return [];
    if (filter === "all") return data.events;
    return data.events.filter((e) => e.status === filter);
  }, [data, filter]);

  // Drift-guard test greps for this constant import — keep
  // referenced even though FilterChips iterates a Category[]
  // (which was built from the same source-of-truth list).
  void OUTBOUND_EMAIL_STATUS_VALUES;

  return (
    <div className="space-y-4 p-4 max-w-6xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <H2>Outbound Email Log</H2>
          <ActiveTenantBadge />
        </div>
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
                categories={OUTBOUND_EMAIL_CATEGORIES}
                counts={data.by_status_24h}
                total={data.total_recent_24h}
                totalNoun="composed"
              />
              <Sparkline
                points={data.daily_sent_14d}
                totalSuffix="sent · 14d"
                ariaLabel={`Daily 'sent' counts over the last ${data.daily_sent_14d.length} days`}
              />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-3">
              <FilterChips
                categories={OUTBOUND_EMAIL_CATEGORIES}
                counts={data.by_status_24h}
                current={filter}
                onChange={setFilter}
              />
            </CardContent>
          </Card>

          {filteredEvents.length === 0 ? (
            <EmptyFilteredMessage
              isAllFilter={filter === "all"}
              titleAll="No outbound email events recorded yet"
              titleFiltered={`No "${OUTBOUND_EMAIL_CATEGORY_MAP[filter as OutboundEmailStatus]?.label}" events match this filter in the current window`}
              bodyAll="The tool.email_to_operator_sent audit stream is empty for now. This page will populate when Kora composes and sends email via her reasoning loop."
              onResetToAll={() => setFilter("all")}
            />
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
