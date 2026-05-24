import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Clock,
  Hourglass,
  Inbox,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import { usePanelView } from "@/hooks/usePanelView";
import type {
  Criticality,
  FailedOrBlockedTicket,
  KoraAssignedSeaTicketsResponse,
  ModelTier,
  QueuedTicket,
  Resolution,
} from "@/lib/api";

const CRITICALITY_TONE: Record<Criticality, "destructive" | "warning" | "outline" | "success"> = {
  frontier: "destructive",
  high: "warning",
  normal: "outline",
  low: "success",
};

const CRITICALITY_RANK: Record<Criticality, number> = {
  frontier: 0,
  high: 1,
  normal: 2,
  low: 3,
};

const MODEL_TIER_TONE: Record<ModelTier, "destructive" | "warning" | "outline"> = {
  opus: "destructive",
  sonnet: "warning",
  haiku: "outline",
};

const RESOLUTION_TONE: Record<Resolution, "success" | "warning" | "destructive" | "outline"> = {
  completed: "success",
  released: "outline",
  failed_retryable: "warning",
  failed_terminal: "destructive",
  blocked_needs_operator: "destructive",
  deferred_cost_limit: "warning",
};

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

function sortQueued(queued: QueuedTicket[]): QueuedTicket[] {
  // Bucket §3: criticality desc + assigned_at asc (matches G-8 priority).
  return [...queued].sort((a, b) => {
    const c = CRITICALITY_RANK[a.criticality] - CRITICALITY_RANK[b.criticality];
    if (c !== 0) return c;
    return (a.assigned_at ?? "").localeCompare(b.assigned_at ?? "");
  });
}

function failureChips(
  failure_count_by_reason: FailedOrBlockedTicket["failure_count_by_reason"],
): { reason: string; count: number }[] {
  return Object.entries(failure_count_by_reason)
    .filter(([, count]) => count > 0)
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count);
}

export default function SeaTicketsPage() {
  usePanelView("SeaTicketsPage");

  const [data, setData] = useState<KoraAssignedSeaTicketsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [resolvedExpanded, setResolvedExpanded] = useState(false);
  const { toast, showToast } = useToast();

  const loadTickets = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getKoraAssignedSeaTickets()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load Sea_Tickets: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadTickets(false);
  }, [loadTickets]);

  const sortedQueued = useMemo(
    () => (data ? sortQueued(data.queued) : []),
    [data],
  );

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
          <H2>Sea Tickets — Kora's queue</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Tickets assigned to Kora — what she's working on, queued, recently
            resolved, or stuck.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadTickets(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load Sea_Tickets</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <ShieldAlert className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB DATA — Sea_Ticket read wire-in pending (KR-P2-E +
                IsoKronMemoryProvider helper)
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Panel is a UI preview; values shown are sample data, not real
                tickets.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── In progress ────────────────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Hourglass className="h-4 w-4" />
              In progress
              <span className="text-xs text-muted-foreground">
                ({data.in_progress.length})
              </span>
            </div>
            {data.in_progress.length === 0 ? (
              <Card>
                <CardContent className="py-4 text-sm text-muted-foreground">
                  Nothing currently in progress.
                </CardContent>
              </Card>
            ) : (
              data.in_progress.map((t) => (
                <Card key={t.id} className="border-primary/30">
                  <CardContent className="flex flex-col gap-2 py-4">
                    <div className="flex items-center gap-2">
                      <span
                        className="h-2 w-2 rounded-full bg-success animate-pulse"
                        aria-label="active"
                      />
                      <span className="font-medium">{t.title}</span>
                      <Badge tone={CRITICALITY_TONE[t.criticality]}>
                        {t.criticality}
                      </Badge>
                    </div>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                      <span>
                        claimed {formatRelative(t.claimed_at)} ({formatTimestamp(t.claimed_at)})
                      </span>
                      <span>claim #{t.claim_count}</span>
                      <span>attempt #{t.work_attempt_count}</span>
                      <code className="text-xs">{t.id}</code>
                    </div>
                  </CardContent>
                </Card>
              ))
            )}
          </section>

          {/* ── Queued ─────────────────────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Inbox className="h-4 w-4" />
              Queued
              <span className="text-xs text-muted-foreground">
                ({sortedQueued.length})
              </span>
            </div>
            {sortedQueued.length === 0 ? (
              <Card>
                <CardContent className="py-4 text-sm text-muted-foreground">
                  Queue is empty.
                </CardContent>
              </Card>
            ) : (
              <Card>
                <CardContent className="py-3 overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-xs text-muted-foreground border-b">
                        <th className="py-2 pr-3 font-medium">Title</th>
                        <th className="py-2 pr-3 font-medium">Criticality</th>
                        <th className="py-2 pr-3 font-medium">Assigned</th>
                        <th className="py-2 font-medium">Next eligible</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortedQueued.map((t) => (
                        <tr key={t.id} className="border-b last:border-0 align-top">
                          <td className="py-2 pr-3">
                            <div>{t.title}</div>
                            <code className="text-xs text-muted-foreground">{t.id}</code>
                          </td>
                          <td className="py-2 pr-3">
                            <Badge tone={CRITICALITY_TONE[t.criticality]}>
                              {t.criticality}
                            </Badge>
                          </td>
                          <td className="py-2 pr-3 text-xs">
                            <div>{formatRelative(t.assigned_at)}</div>
                            <div className="text-muted-foreground">
                              {formatTimestamp(t.assigned_at)}
                            </div>
                          </td>
                          <td className="py-2 text-xs">
                            {t.next_eligible_at ? (
                              <span className="text-warning">
                                deferred — {formatRelative(t.next_eligible_at)}
                              </span>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </CardContent>
              </Card>
            )}
          </section>

          {/* ── Recently resolved (collapsible, default-collapsed) ── */}
          <section className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setResolvedExpanded((v) => !v)}
              className="flex items-center gap-2 text-sm font-medium w-fit"
              aria-expanded={resolvedExpanded}
            >
              {resolvedExpanded ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
              <Clock className="h-4 w-4" />
              Recently resolved
              <span className="text-xs text-muted-foreground">
                ({data.recently_resolved.length})
              </span>
            </button>
            {resolvedExpanded &&
              (data.recently_resolved.length === 0 ? (
                <Card>
                  <CardContent className="py-4 text-sm text-muted-foreground">
                    No recently resolved tickets.
                  </CardContent>
                </Card>
              ) : (
                <Card>
                  <CardContent className="py-3 overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-left text-xs text-muted-foreground border-b">
                          <th className="py-2 pr-3 font-medium">Title</th>
                          <th className="py-2 pr-3 font-medium">Resolved</th>
                          <th className="py-2 pr-3 font-medium">Resolution</th>
                          <th className="py-2 font-medium">Model</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.recently_resolved.map((t) => (
                          <tr key={t.id} className="border-b last:border-0">
                            <td className="py-2 pr-3">
                              <div>{t.title}</div>
                              <code className="text-xs text-muted-foreground">{t.id}</code>
                            </td>
                            <td className="py-2 pr-3 text-xs">
                              <div>{formatRelative(t.resolved_at)}</div>
                              <div className="text-muted-foreground">
                                {formatTimestamp(t.resolved_at)}
                              </div>
                            </td>
                            <td className="py-2 pr-3">
                              <Badge tone={RESOLUTION_TONE[t.resolution]}>
                                {t.resolution}
                              </Badge>
                            </td>
                            <td className="py-2">
                              <Badge tone={MODEL_TIER_TONE[t.model_tier_used]}>
                                {t.model_tier_used}
                              </Badge>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </CardContent>
                </Card>
              ))}
          </section>

          {/* ── Failed or blocked (always visible) ─────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <AlertCircle className="h-4 w-4" />
              Failed or blocked
              <span className="text-xs text-muted-foreground">
                ({data.failed_or_blocked.length})
              </span>
            </div>
            <p className="text-xs text-muted-foreground -mt-1">
              Operator interventions happen in the cockpit, not here.
            </p>
            {data.failed_or_blocked.length === 0 ? (
              <Card>
                <CardContent className="py-4 text-sm text-muted-foreground">
                  No failed or blocked tickets.
                </CardContent>
              </Card>
            ) : (
              data.failed_or_blocked.map((t) => {
                const chips = failureChips(t.failure_count_by_reason);
                return (
                  <Card key={t.id} className="border-destructive/30">
                    <CardContent className="flex flex-col gap-2 py-3">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium">{t.title}</span>
                        <Badge tone={CRITICALITY_TONE[t.criticality]}>
                          {t.criticality}
                        </Badge>
                        <Badge tone="destructive">{t.state}</Badge>
                      </div>
                      <div className="flex items-center gap-2 text-xs">
                        <code className="text-muted-foreground">{t.id}</code>
                      </div>
                      {chips.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {chips.map(({ reason, count }) => (
                            <span
                              key={reason}
                              className="rounded border border-destructive/40 bg-destructive/10 px-2 py-0.5 text-xs"
                            >
                              {reason} × {count}
                            </span>
                          ))}
                        </div>
                      )}
                    </CardContent>
                  </Card>
                );
              })
            )}
          </section>
        </>
      )}
    </div>
  );
}
