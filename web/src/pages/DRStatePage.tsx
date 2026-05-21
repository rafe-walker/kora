import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Database,
  History,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  XCircle,
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
  DRCurrent,
  DRMatchStatus,
  DRStateResponse,
  EpochHistoryEntry,
} from "@/lib/api";

const MATCH_STATUS_TONE: Record<DRMatchStatus, "success" | "destructive" | "warning" | "outline"> = {
  clean: "success",
  mismatch_detected: "destructive",
  pending_runbook: "warning",
  unknown: "outline",
};

const MATCH_STATUS_LABEL: Record<DRMatchStatus, string> = {
  clean: "clean",
  mismatch_detected: "mismatch detected",
  pending_runbook: "pending runbook",
  unknown: "unknown",
};

function MatchStatusIcon({ status }: { status: DRMatchStatus }) {
  switch (status) {
    case "clean":
      return <ShieldCheck className="h-5 w-5 text-success" />;
    case "mismatch_detected":
      return <ShieldX className="h-5 w-5 text-destructive" />;
    case "pending_runbook":
      return <ShieldAlert className="h-5 w-5 text-warning" />;
    case "unknown":
      return <AlertTriangle className="h-5 w-5 text-muted-foreground" />;
  }
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatRelative(iso: string | null | undefined): string {
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

interface CurrentEpochCardProps {
  current: DRCurrent;
}

function CurrentEpochCard({ current }: CurrentEpochCardProps) {
  const matches =
    current.kora_known_epoch !== null &&
    current.substrate_epoch === current.kora_known_epoch;

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 py-5">
        <div className="flex items-center gap-2">
          <MatchStatusIcon status={current.match_status} />
          <span className="text-sm font-medium">Current epoch</span>
          <Badge tone={MATCH_STATUS_TONE[current.match_status]}>
            {MATCH_STATUS_LABEL[current.match_status]}
          </Badge>
          {current.kora_paused_substrate && (
            <Badge tone="destructive">PAUSED&#123;substrate&#125;</Badge>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="flex flex-col gap-1">
            <div className="text-xs text-muted-foreground">substrate_epoch</div>
            <div
              className={`text-4xl font-semibold ${matches ? "text-success" : "text-destructive"}`}
            >
              {current.substrate_epoch}
            </div>
            <div className="text-xs text-muted-foreground">
              authoritative — the substrate's current PITR-aware epoch
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <div className="text-xs text-muted-foreground">kora_known_epoch</div>
            <div
              className={`text-4xl font-semibold ${matches ? "text-success" : "text-warning"}`}
            >
              {current.kora_known_epoch ?? "—"}
            </div>
            <div className="text-xs text-muted-foreground">
              what the runtime believes is current; gate 3b compares the two
              on every boot
            </div>
          </div>
        </div>

        <div className="text-xs text-muted-foreground">
          last checked {formatRelative(current.last_check_at)} (
          {formatTimestamp(current.last_check_at)})
        </div>
      </CardContent>
    </Card>
  );
}

interface EpochHistoryTableProps {
  history: EpochHistoryEntry[];
}

function EpochHistoryTable({ history }: EpochHistoryTableProps) {
  // Sort epoch desc so the most recent is at the top.
  const sorted = useMemo(
    () => [...history].sort((a, b) => b.epoch - a.epoch),
    [history],
  );

  if (sorted.length === 0) {
    return (
      <Card>
        <CardContent className="py-4 text-sm text-muted-foreground">
          No epoch history recorded.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="py-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted-foreground border-b">
              <th className="py-2 pr-3 font-medium">Epoch</th>
              <th className="py-2 pr-3 font-medium">Observed</th>
              <th className="py-2 pr-3 font-medium">Kora caught up</th>
              <th className="py-2 font-medium">Source</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((e) => (
              <tr key={e.epoch} className="border-b last:border-0 align-top">
                <td className="py-2 pr-3 text-2xl font-mono">{e.epoch}</td>
                <td className="py-2 pr-3 text-xs">
                  <div>{formatRelative(e.observed_at)}</div>
                  <div className="text-muted-foreground">
                    {formatTimestamp(e.observed_at)}
                  </div>
                </td>
                <td className="py-2 pr-3 text-xs">
                  {e.kora_known_at ? (
                    <>
                      <div>{formatRelative(e.kora_known_at)}</div>
                      <div className="text-muted-foreground">
                        {formatTimestamp(e.kora_known_at)}
                      </div>
                    </>
                  ) : (
                    <span className="text-warning">not yet</span>
                  )}
                </td>
                <td className="py-2">
                  <Badge tone="outline">{e.source}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

interface DRObservedEventsTableProps {
  events: DRStateResponse["recent_dr_events"];
}

function DRObservedEventsTable({ events }: DRObservedEventsTableProps) {
  const sorted = useMemo(
    () =>
      [...events].sort((a, b) =>
        (b.occurred_at ?? "").localeCompare(a.occurred_at ?? ""),
      ),
    [events],
  );

  if (sorted.length === 0) {
    return (
      <Card>
        <CardContent className="py-4 text-sm text-muted-foreground">
          No DR events recorded — operationally this means no PITR has
          fired on this deployment.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="py-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted-foreground border-b">
              <th className="py-2 pr-3 font-medium">Occurred</th>
              <th className="py-2 pr-3 font-medium">Epoch jump</th>
              <th className="py-2 pr-3 font-medium">Discarded</th>
              <th className="py-2 font-medium">Cleared</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((ev, i) => (
              <tr
                key={`${ev.occurred_at}-${i}`}
                className="border-b last:border-0 align-top"
              >
                <td className="py-2 pr-3 text-xs">
                  <div>{formatRelative(ev.occurred_at)}</div>
                  <div className="text-muted-foreground">
                    {formatTimestamp(ev.occurred_at)}
                  </div>
                </td>
                <td className="py-2 pr-3 text-xs font-mono">
                  {ev.from_epoch} → {ev.to_epoch}
                </td>
                <td className="py-2 pr-3 text-xs">
                  <div>
                    {ev.discarded_operation_ids} operation
                    {ev.discarded_operation_ids === 1 ? "" : "s"}
                  </div>
                  <div className="text-muted-foreground">
                    {ev.discarded_ledger_rows} ledger row
                    {ev.discarded_ledger_rows === 1 ? "" : "s"}
                  </div>
                </td>
                <td className="py-2 text-xs">
                  {ev.cleared_at && ev.cleared_by ? (
                    <>
                      <CheckCircle2 className="h-3.5 w-3.5 text-success inline mr-1" />
                      {ev.cleared_by}
                      <div className="text-muted-foreground">
                        {formatRelative(ev.cleared_at)}
                      </div>
                    </>
                  ) : (
                    <span className="text-warning">
                      <XCircle className="h-3.5 w-3.5 inline mr-1" />
                      not cleared
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function hasNonMonotonicJump(history: EpochHistoryEntry[]): boolean {
  // Sort by observed_at asc and check if any neighbour pair jumps by > 1
  // (DR-recovery or operator-bump can do that). Used to default-expand
  // the DR events section even when runbook_pending is False but the
  // history shows a prior DR event.
  const sorted = [...history].sort((a, b) =>
    (a.observed_at ?? "").localeCompare(b.observed_at ?? ""),
  );
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].epoch - sorted[i - 1].epoch > 1) return true;
  }
  return false;
}

export default function DRStatePage() {
  const [data, setData] = useState<DRStateResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [eventsExpanded, setEventsExpanded] = useState<boolean | null>(null);
  const { toast, showToast } = useToast();

  const loadDR = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getDRState()
        .then((resp) => {
          setData(resp);
          if (eventsExpanded === null) {
            setEventsExpanded(
              resp.runbook_pending || hasNonMonotonicJump(resp.epoch_history),
            );
          }
        })
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load DR state: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [eventsExpanded, showToast],
  );

  useEffect(() => {
    loadDR(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

      {/* ── DR alert banner (only when runbook_pending) ──────────── */}
      {data?.runbook_pending && (
        <Card className="border-destructive/60 bg-destructive/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <ShieldAlert className="h-5 w-5 mt-0.5 text-destructive shrink-0" />
            <div>
              <div className="font-semibold text-destructive">
                DR DETECTED — Kora is PAUSED&#123;substrate&#125;
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                Run the post-PITR runbook: bump <code>substrate_epoch</code>{" "}
                (operator action — Fly secret + <code>flyctl restart</code>),
                then issue a <code>kora_control</code> reset to clear
                PAUSED&#123;substrate&#125;.
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                See <code>kora_docs/15_status_and_roadmap/dr_runbook.md</code>{" "}
                (pending KR-P2-M).
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex items-start justify-between gap-4">
        <div>
          <H2>DR &amp; substrate epoch</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Disaster recovery &amp; substrate epoch — proof gate 3b is doing
            its job.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadDR(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load DR state</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <Database className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB DATA — DR/epoch runtime wire-in pending (KR-P2-M)
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Panel is a UI preview; values shown are sample data, not the
                real substrate epoch state.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <CurrentEpochCard current={data.current} />

          <section className="flex flex-col gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <History className="h-4 w-4" />
              Epoch history
              <span className="text-xs text-muted-foreground">
                ({data.epoch_history.length})
              </span>
            </div>
            <EpochHistoryTable history={data.epoch_history} />
          </section>

          <section className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setEventsExpanded((v) => !v)}
              className="flex items-center gap-2 text-sm font-medium w-fit"
              aria-expanded={eventsExpanded ?? false}
            >
              {eventsExpanded ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
              <ShieldAlert className="h-4 w-4" />
              Recent DR events
              <span className="text-xs text-muted-foreground">
                ({data.recent_dr_events.length})
              </span>
            </button>
            {eventsExpanded && (
              <DRObservedEventsTable events={data.recent_dr_events} />
            )}
          </section>
        </>
      )}
    </div>
  );
}
