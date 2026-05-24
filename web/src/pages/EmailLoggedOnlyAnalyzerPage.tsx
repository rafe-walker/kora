// KR-FE-EMAIL-LOGGED-ONLY-ANALYZER — un-acted-on email-intent lens.
//
// Surfaces intent.email_to_sea_ticket rows where action="logged_only"
// — emails from operator that DIDN'T match a high-confidence intent
// pattern. Two operator-trust framings:
//
//   1. Triage: did Kora miss something it should have recognized?
//   2. Training-data lens for the eventual KR-PROMOTE-EMAIL-INTENT
//      loop — these are the corpus that loop will observe.
//
// Built as a NEW page rather than a filter on EmailIntentLogPage
// because the framing is fundamentally different ("what didn't
// Kora act on" vs "everything Kora evaluated"). New page = clearer
// nav signal + room for the future "Suggest pattern" affordance
// without crowding the parent panel.
//
// "Suggest pattern" CTA is a stub for v1 — flag as future bucket
// KR-FE-PATTERN-SUGGESTION (would queue suggestions for
// KR-PROMOTE-EMAIL-INTENT when that ships).
//
// Reuses the existing /api/email-intent/recent endpoint + filters
// client-side. Per-seam endpoint stays the source-of-truth — no
// new BE endpoint needed.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  HelpCircle,
  Inbox,
  Lightbulb,
  Mail,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { useActiveTenant } from "@/hooks/useActiveTenant";
import { api } from "@/lib/api";
import type {
  EmailIntentEvent,
  EmailIntentEventsResponse,
} from "@/lib/api";
import {
  EmptyFilteredMessage,
  formatRelative,
  formatTimestamp,
  truncate,
} from "@/components/AuditPanelKit";

// ----- Per-row card -----

function LoggedOnlyCard({ event }: { event: EmailIntentEvent }) {
  return (
    <Card>
      <CardContent className="p-3 flex items-start gap-3">
        <Mail className="h-4 w-4 mt-0.5 text-muted-foreground flex-shrink-0" />
        <div className="flex-1 min-w-0 flex flex-col gap-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-xs text-muted-foreground"
              title={formatTimestamp(event.emitted_at)}
            >
              {formatRelative(event.emitted_at)}
            </span>
            <span
              className="text-sm font-medium truncate"
              title={event.subject}
            >
              {truncate(event.subject || "(no subject)", 80)}
            </span>
          </div>
          <div className="flex items-center gap-2 flex-wrap text-xs">
            <Badge tone="outline" className="font-mono">
              pattern: {event.pattern_matched || "(none)"}
            </Badge>
            <span className="text-muted-foreground">
              confidence: {event.confidence || "(n/a)"}
            </span>
            {event.reason && (
              <span className="text-muted-foreground italic">
                reason: {event.reason}
              </span>
            )}
          </div>
          <div className="text-[10px] text-muted-foreground">
            <span className="font-mono">
              caller_session_id: {event.caller_session_id || "(unset)"}
            </span>
          </div>
          {/* Future affordance — flagged as STOP-ASK in §4 of the bucket
              spec. Stub UI only; clicking does nothing in v1. The button
              is rendered disabled with a tooltip so operator can SEE
              that this surface is coming (sets expectations) without
              implying functionality that doesn't exist yet. */}
          <div className="pt-1">
            <Button
              size="sm"
              ghost
              disabled
              title="Coming in KR-FE-PATTERN-SUGGESTION — operator-sketched patterns will queue for the eventual KR-PROMOTE-EMAIL-INTENT loop."
              className="text-xs"
            >
              <Lightbulb className="h-3 w-3 mr-1" />
              Suggest pattern (coming soon)
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ----- Page -----

export default function EmailLoggedOnlyAnalyzerPage() {
  usePanelView("EmailLoggedOnlyAnalyzerPage");

  const { activeTenant, isAllTenants } = useActiveTenant();
  const tenantForRead = isAllTenants ? undefined : activeTenant;

  const [data, setData] = useState<EmailIntentEventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Bump limit past the default 100 — un-acted-on is the
      // long-tail of intent events; operator wants the full corpus
      // when building a training-data view. 500 matches the BE cap.
      const resp = await api.getEmailIntentEventsRecent({
        limit: 500,
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

  const loggedOnlyEvents = useMemo(() => {
    if (data === null) return [];
    return data.events.filter((e) => e.action === "logged_only");
  }, [data]);

  const last7dCount = useMemo(() => {
    const sevenDaysAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
    return loggedOnlyEvents.filter((e) => {
      const t = Date.parse(e.emitted_at);
      return !Number.isNaN(t) && t >= sevenDaysAgo;
    }).length;
  }, [loggedOnlyEvents]);

  return (
    <div className="space-y-4 p-4 max-w-5xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2 className="flex items-center gap-2">
          <Inbox className="h-5 w-5" />
          Email Intent · Logged-Only
        </H2>
        <Button
          outlined
          size="sm"
          onClick={() => void load()}
          disabled={loading}
        >
          <RefreshCw
            className={`h-3 w-3 mr-1 ${loading ? "animate-spin" : ""}`}
          />
          Refresh
        </Button>
      </div>

      <Card className="border-blue-500/30 bg-blue-500/5">
        <CardContent className="p-3 flex items-start gap-2 text-xs">
          <HelpCircle className="h-4 w-4 text-blue-500 flex-shrink-0 mt-0.5" />
          <div className="space-y-1 text-muted-foreground">
            <div className="text-foreground font-medium">
              Why this view
            </div>
            <div>
              Inbound emails Kora evaluated that{" "}
              <span className="font-mono">DIDN&apos;T</span> match a
              high-confidence intent pattern (action=
              <span className="font-mono">logged_only</span> from PR
              #176&apos;s intent.email_to_sea_ticket seam). Two operator
              uses:
            </div>
            <ul className="list-disc ml-4 space-y-0.5">
              <li>
                <strong className="text-foreground">Triage:</strong>{" "}
                spot patterns Kora SHOULD have recognized — &ldquo;this
                forward asked me to save an article; the pattern was
                <em> too narrow.&rdquo;</em>
              </li>
              <li>
                <strong className="text-foreground">Training data:</strong>{" "}
                this corpus is what the eventual KR-PROMOTE-EMAIL-INTENT
                loop will observe to propose new patterns.
              </li>
            </ul>
          </div>
        </CardContent>
      </Card>

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
            <CardContent className="p-3 flex items-center gap-3 text-sm">
              <Inbox className="h-4 w-4 text-muted-foreground" />
              <span>
                <strong>{last7dCount}</strong> logged-only events in the
                last 7 days
              </span>
              <span className="text-muted-foreground">·</span>
              <span className="text-muted-foreground">
                {loggedOnlyEvents.length} total in the audit window
              </span>
            </CardContent>
          </Card>

          {loggedOnlyEvents.length === 0 ? (
            <EmptyFilteredMessage
              isAllFilter={true}
              titleAll="No logged-only events yet"
              titleFiltered=""
              bodyAll="Every inbound email Kora evaluated matched an intent pattern (or there are no events in the window). When operator forwards or asks Kora something that doesn't fit a known pattern, it'll appear here as training data for the future promotion loop."
              onResetToAll={() => {
                /* no-op — single-filter lens */
              }}
            />
          ) : (
            <div className="space-y-2">
              {loggedOnlyEvents.map((event) => (
                <LoggedOnlyCard key={event.id} event={event} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
