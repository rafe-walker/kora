// Email-intent audit log lens — KR-FE-EMAIL-INTENT-LOG-PANEL.
//
// Surfaces the intent.email_to_sea_ticket audit stream (PR #176)
// so operator can see — at a glance — which inbound emails Kora
// converted to Sea_Tickets, which were logged-only (future
// promotion-loop training data), and which failed (triage).
//
// Retrofit (KR-FE-PANEL-KIT-AND-MUTATING-ACTIONS-MEGABUCKET):
// Sparkline / SummaryChips / FilterChips / formatters /
// BadgeTone / EmptyFilteredMessage now live in AuditPanelKit —
// see web/src/components/AuditPanelKit/README.md. The
// EmailIntent-specific code that stays here is: the category
// def + per-row card + page wiring.

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
  XCircle,
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
  EMAIL_INTENT_ACTION_VALUES,
  type EmailIntentAction,
  type EmailIntentEvent,
  type EmailIntentEventsResponse,
} from "@/lib/api";
import {
  EmptyFilteredMessage,
  FilterChips,
  Sparkline,
  SummaryChips,
  formatRelative,
  formatTimestamp,
  truncate,
  type CategoryDef,
  type FilterValue,
} from "@/components/AuditPanelKit";

// Per-action visual definition. Matches AuditPanelKit's
// CategoryDef<K> contract so the kit's chip components type-check
// over EmailIntentAction.
const EMAIL_INTENT_CATEGORIES: readonly CategoryDef<EmailIntentAction>[] = [
  { key: "created", label: "Created", tone: "success", Icon: CheckCircle2 },
  {
    key: "logged_only",
    label: "Logged-only",
    tone: "outline",
    Icon: Inbox,
  },
  { key: "dry_run", label: "Dry-run", tone: "secondary", Icon: Clock },
  {
    key: "cap_exceeded",
    label: "Cap-exceeded",
    tone: "warning",
    Icon: Ban,
  },
  { key: "failed", label: "Failed", tone: "destructive", Icon: XCircle },
];

// Helper for the per-row card's badge — indexes by enum value
// (falling back to a neutral chip for "unknown" / unrecognized).
const EMAIL_INTENT_CATEGORY_MAP: Record<
  EmailIntentAction,
  CategoryDef<EmailIntentAction>
> = {
  created: EMAIL_INTENT_CATEGORIES[0],
  logged_only: EMAIL_INTENT_CATEGORIES[1],
  dry_run: EMAIL_INTENT_CATEGORIES[2],
  cap_exceeded: EMAIL_INTENT_CATEGORIES[3],
  failed: EMAIL_INTENT_CATEGORIES[4],
  unknown: {
    key: "created" as EmailIntentAction, // sentinel; never used as a chip
    label: "Unknown",
    tone: "outline",
    Icon: AlertTriangle,
  },
};

// ----- Per-row card -----

function EventCard({ event }: { event: EmailIntentEvent }) {
  const v = EMAIL_INTENT_CATEGORY_MAP[event.action];
  const hasTicket = event.action === "created" && event.ticket_id;
  const Icon = v.Icon;
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
              <Icon className="h-3 w-3 mr-1 inline" />
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

  const { activeTenant, isAllTenants } = useActiveTenant();
  const tenantForRead = isAllTenants ? undefined : activeTenant;

  const [data, setData] = useState<EmailIntentEventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterValue<EmailIntentAction>>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getEmailIntentEventsRecent({
        tenantId: tenantForRead,
      });
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
    return data.events.filter((e) => e.action === filter);
  }, [data, filter]);

  // Defensive reference so EMAIL_INTENT_ACTION_VALUES (the
  // drift-guarded canonical list from api.ts) stays referenced
  // from this file — the drift-guard test greps the constant
  // here so it's worth keeping the import live.
  void EMAIL_INTENT_ACTION_VALUES;

  return (
    <div className="space-y-4 p-4 max-w-6xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <H2>Email Intent Log</H2>
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
                categories={EMAIL_INTENT_CATEGORIES}
                counts={data.by_action_24h}
                total={data.total_recent_24h}
              />
              <Sparkline
                points={data.daily_created_14d}
                totalSuffix="created · 14d"
                ariaLabel={`Daily 'created' counts over the last ${data.daily_created_14d.length} days`}
              />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-3">
              <FilterChips
                categories={EMAIL_INTENT_CATEGORIES}
                counts={data.by_action_24h}
                current={filter}
                onChange={setFilter}
              />
            </CardContent>
          </Card>

          {filteredEvents.length === 0 ? (
            <EmptyFilteredMessage
              isAllFilter={filter === "all"}
              titleAll="No email-intent events recorded yet"
              titleFiltered={`No "${EMAIL_INTENT_CATEGORY_MAP[filter as EmailIntentAction]?.label}" events match this filter in the current window`}
              bodyAll="The intent.email_to_sea_ticket audit stream is empty for now. This page will populate when Kora evaluates inbound emails for Sea_Ticket conversion."
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
