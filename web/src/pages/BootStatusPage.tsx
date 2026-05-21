import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Loader2,
  RefreshCw,
  ShieldAlert,
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
  BootHistoryEntry,
  BootOutcome,
  BootStatusResponse,
  CurrentBoot,
  GateResult,
} from "@/lib/api";

const OUTCOME_TONE: Record<BootOutcome, "success" | "warning" | "destructive"> = {
  ready: "success",
  booting: "warning",
  failed: "destructive",
};

const HISTORY_DEFAULT_LIMIT = 10;
const HISTORY_MAX_LIMIT = 20;

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

interface GateSequenceStripProps {
  gates: GateResult[];
  outcome: BootOutcome;
}

function GateSequenceStrip({ gates, outcome }: GateSequenceStripProps) {
  // Find the failed gate index (if any); steps after it stay gray.
  const failedIndex = gates.findIndex((g) => g.outcome === "fail");

  return (
    <div className="overflow-x-auto">
      <ol className="flex items-stretch gap-0 text-xs min-w-fit">
        {gates.map((gate, idx) => {
          const isFailed = gate.outcome === "fail";
          const isPending =
            outcome === "booting" && !isFailed && failedIndex === -1 && idx === gates.length - 1;
          // Steps after the failure render greyed-out (not run).
          const isAfterFailure = failedIndex !== -1 && idx > failedIndex;
          const reachedAndPassed = !isFailed && !isAfterFailure && !isPending;

          const connectorTone =
            idx < gates.length - 1
              ? failedIndex !== -1 && idx >= failedIndex
                ? "bg-border"
                : "bg-success/60"
              : "";

          return (
            <li
              key={`${gate.gate_id}-${idx}`}
              className="flex-1 flex flex-col items-center text-center min-w-[80px] relative px-1"
              title={`Gate ${gate.gate_id} (${gate.gate_class}) — ${gate.detail}`}
            >
              {idx < gates.length - 1 && (
                <div
                  className={`absolute top-2 left-1/2 w-full h-px ${connectorTone}`}
                  aria-hidden
                />
              )}
              <div className="relative z-10 mb-1">
                {isFailed ? (
                  <XCircle className="h-4 w-4 text-destructive" />
                ) : isPending ? (
                  <Loader2 className="h-4 w-4 text-warning animate-spin" />
                ) : isAfterFailure ? (
                  <Circle className="h-4 w-4 text-muted-foreground/40" />
                ) : reachedAndPassed ? (
                  <CheckCircle2 className="h-4 w-4 text-success" />
                ) : (
                  <Circle className="h-4 w-4 text-muted-foreground/40" />
                )}
              </div>
              <div
                className={`font-medium truncate w-full ${
                  isFailed
                    ? "text-destructive"
                    : isAfterFailure
                      ? "text-muted-foreground/60"
                      : "text-foreground"
                }`}
              >
                gate {gate.gate_id}
              </div>
              <div className="text-muted-foreground text-[10px] truncate w-full">
                {gate.title}
              </div>
              {reachedAndPassed && (
                <div className="text-muted-foreground text-[10px]">
                  {formatElapsed(gate.elapsed_ms)}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

interface CurrentBootCardProps {
  boot: CurrentBoot;
}

function CurrentBootCard({ boot }: CurrentBootCardProps) {
  const failedGate = boot.gates.find((g) => g.outcome === "fail");
  const isBooting = boot.outcome === "booting";

  return (
    <Card
      className={
        boot.outcome === "failed"
          ? "border-destructive/40"
          : isBooting
            ? "border-warning/40"
            : "border-success/30"
      }
    >
      <CardContent className="flex flex-col gap-4 py-5">
        <div className="flex flex-wrap items-center gap-3">
          {isBooting && (
            <span
              className="h-2 w-2 rounded-full bg-warning animate-pulse"
              aria-label="booting"
            />
          )}
          <Badge tone={OUTCOME_TONE[boot.outcome]}>
            <span className="font-semibold tracking-wide">
              {boot.outcome.toUpperCase()}
            </span>
          </Badge>
          <code className="text-xs text-muted-foreground">{boot.boot_id}</code>
          <span className="text-xs text-muted-foreground">
            started {formatTimestamp(boot.started_at)}
          </span>
          {boot.completed_at && (
            <span className="text-xs text-muted-foreground">
              completed {formatTimestamp(boot.completed_at)} ({formatElapsed(boot.elapsed_ms)})
            </span>
          )}
        </div>

        <GateSequenceStrip gates={boot.gates} outcome={boot.outcome} />

        {failedGate && (
          <div className="border-destructive/40 bg-destructive/10 rounded p-3 flex items-start gap-2 text-sm">
            <AlertTriangle className="h-4 w-4 mt-0.5 text-destructive shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="font-medium text-destructive">
                Gate {failedGate.gate_id} failed: {failedGate.title}
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                {failedGate.detail}
              </div>
              <div className="text-xs text-muted-foreground mt-2 italic">
                Review the <code>kora.boot.failed</code> chain event payload for
                the full GateResult list.
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

interface HistoryTableProps {
  history: BootHistoryEntry[];
  limit: number;
}

function HistoryTable({ history, limit }: HistoryTableProps) {
  const sorted = useMemo(
    () =>
      [...history]
        .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""))
        .slice(0, limit),
    [history, limit],
  );

  if (sorted.length === 0) {
    return (
      <Card>
        <CardContent className="py-4 text-sm text-muted-foreground">
          No prior boots recorded.
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
              <th className="py-2 pr-3 font-medium">Started</th>
              <th className="py-2 pr-3 font-medium">Outcome</th>
              <th className="py-2 pr-3 font-medium">Elapsed</th>
              <th className="py-2 font-medium">Failed gate</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((boot) => (
              <tr key={boot.boot_id} className="border-b last:border-0 align-top">
                <td className="py-2 pr-3 text-xs">
                  <div>{formatTimestamp(boot.started_at)}</div>
                  <code className="text-muted-foreground">{boot.boot_id}</code>
                </td>
                <td className="py-2 pr-3">
                  <Badge tone={OUTCOME_TONE[boot.outcome]}>
                    {boot.outcome}
                  </Badge>
                </td>
                <td className="py-2 pr-3 text-xs">
                  {formatElapsed(boot.elapsed_ms)}
                </td>
                <td className="py-2 text-xs">
                  {boot.failed_gate_id ? (
                    <span
                      className="text-destructive"
                      title={boot.detail ?? ""}
                    >
                      gate {boot.failed_gate_id}: {boot.failed_gate_title}
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
  );
}

export default function BootStatusPage() {
  const [data, setData] = useState<BootStatusResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  // Default-expanded if the most-recent boot failed (operator should see
  // the history immediately for context); collapsed otherwise.
  const [historyExpanded, setHistoryExpanded] = useState<boolean | null>(null);
  const [historyLimit, setHistoryLimit] = useState(HISTORY_DEFAULT_LIMIT);
  const { toast, showToast } = useToast();

  const loadStatus = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getBootStatus()
        .then((resp) => {
          setData(resp);
          // First-load: open history if current boot failed.
          if (historyExpanded === null) {
            setHistoryExpanded(resp.current.outcome === "failed");
          }
        })
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load boot status: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [historyExpanded, showToast],
  );

  useEffect(() => {
    loadStatus(false);
    // We intentionally only want this on mount; loadStatus depends on
    // historyExpanded which would re-trigger if not stripped.
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

      <div className="flex items-start justify-between gap-4">
        <div>
          <H2>Boot Status</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Kora's last boot sequence — gate outcomes + recent boot history.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadStatus(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load boot status</div>
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
                STUB DATA — BootGateRunner wire-in pending (KR-P2-H)
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Panel is a UI preview; values shown are sample data, not the
                real boot result.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <section className="flex flex-col gap-2">
            <div className="text-sm font-medium">Current boot</div>
            <CurrentBootCard boot={data.current} />
          </section>

          <section className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setHistoryExpanded((v) => !v)}
              className="flex items-center gap-2 text-sm font-medium w-fit"
              aria-expanded={historyExpanded ?? false}
            >
              {historyExpanded ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
              Recent boot history
              <span className="text-xs text-muted-foreground">
                ({data.history.length})
              </span>
            </button>
            {historyExpanded && (
              <>
                <HistoryTable history={data.history} limit={historyLimit} />
                {data.history.length > HISTORY_DEFAULT_LIMIT &&
                  historyLimit < HISTORY_MAX_LIMIT && (
                    <Button
                      size="sm"
                      ghost
                      onClick={() => setHistoryLimit(HISTORY_MAX_LIMIT)}
                    >
                      Show more (up to {HISTORY_MAX_LIMIT})
                    </Button>
                  )}
              </>
            )}
          </section>
        </>
      )}
    </div>
  );
}
