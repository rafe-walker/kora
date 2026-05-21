import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Clock,
  History as HistoryIcon,
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
import type {
  KoraControlCommand,
  KoraControlLifecycleState,
  KoraControlObservedStateResponse,
} from "@/lib/api";

const LEVEL_TONE = (level: number): "outline" | "warning" | "destructive" => {
  if (level <= 0) return "outline";
  if (level <= 1) return "warning";
  return "destructive";
};

const LEVEL_LABEL = (level: number, kind: string): string => {
  if (kind === "reset") return `L${level} RESET`;
  return `L${level} STOP`;
};

const LIFECYCLE_TONE: Record<KoraControlLifecycleState, "success" | "warning" | "destructive" | "outline"> = {
  created: "outline",
  visible_to_runtime: "outline",
  acknowledged: "warning",
  enforcing: "warning",
  enforced: "success",
  superseded: "outline",
  expired: "destructive",
  failed: "destructive",
  escalated: "destructive",
};

// Lifecycle progression for the timeline strip. Note: the spec's lifecycle
// state union includes terminal-but-non-success states (superseded, expired,
// failed, escalated) — those are NOT timeline steps; they replace the
// progression altogether and are handled separately below.
const TIMELINE_STEPS: {
  key: keyof Pick<
    KoraControlCommand,
    | "created_at"
    | "visible_to_runtime_at"
    | "observed_at"
    | "acknowledged_at"
    | "enforced_at"
  >;
  label: string;
  // Which lifecycle states mean "we've reached this step at minimum"
  reachedAtState: KoraControlLifecycleState[];
}[] = [
  { key: "created_at", label: "created", reachedAtState: [] },
  {
    key: "visible_to_runtime_at",
    label: "visible",
    reachedAtState: ["visible_to_runtime", "acknowledged", "enforcing", "enforced"],
  },
  {
    key: "observed_at",
    label: "observed",
    reachedAtState: ["acknowledged", "enforcing", "enforced"],
  },
  {
    key: "acknowledged_at",
    label: "acked",
    reachedAtState: ["acknowledged", "enforcing", "enforced"],
  },
  {
    key: "enforced_at",
    label: "enforced",
    reachedAtState: ["enforced"],
  },
];

const TERMINAL_NON_SUCCESS: KoraControlLifecycleState[] = [
  "superseded",
  "expired",
  "failed",
  "escalated",
];

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatRelative(iso: string | null): string {
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

interface TimelineStripProps {
  command: KoraControlCommand;
  isActive: boolean;
}

function TimelineStrip({ command, isActive }: TimelineStripProps) {
  // Find the highest reached step (max index whose timestamp is present
  // OR whose lifecycle_state guarantees we've reached it).
  const reachedIndex = TIMELINE_STEPS.reduce((highest, step, idx) => {
    const hasTimestamp = command[step.key] !== null;
    const stateGuarantees = step.reachedAtState.includes(command.lifecycle_state);
    return hasTimestamp || stateGuarantees ? Math.max(highest, idx) : highest;
  }, 0);

  const isTerminalNonSuccess = TERMINAL_NON_SUCCESS.includes(command.lifecycle_state);

  return (
    <div className="flex flex-col gap-1.5">
      <ol className="flex items-stretch gap-0 text-xs">
        {TIMELINE_STEPS.map((step, idx) => {
          const ts = command[step.key];
          const reached = idx <= reachedIndex && !isTerminalNonSuccess;
          const isCurrentActive =
            isActive &&
            !isTerminalNonSuccess &&
            idx === reachedIndex &&
            reachedIndex < TIMELINE_STEPS.length - 1;

          return (
            <li
              key={step.key}
              className="flex-1 flex flex-col items-center text-center min-w-0 relative"
            >
              {/* Connector line to next step */}
              {idx < TIMELINE_STEPS.length - 1 && (
                <div
                  className={`absolute top-2 left-1/2 w-full h-px ${
                    idx < reachedIndex && !isTerminalNonSuccess
                      ? "bg-success/60"
                      : "bg-border"
                  }`}
                  aria-hidden
                />
              )}
              <div className="relative z-10 mb-1">
                {reached ? (
                  <CheckCircle2
                    className={`h-4 w-4 ${
                      isCurrentActive
                        ? "text-warning animate-pulse"
                        : "text-success"
                    }`}
                  />
                ) : (
                  <Circle className="h-4 w-4 text-muted-foreground/40" />
                )}
              </div>
              <div
                className={`font-medium truncate ${
                  reached ? "text-foreground" : "text-muted-foreground/60"
                }`}
              >
                {step.label}
              </div>
              <div className="text-muted-foreground text-[10px] truncate">
                {ts ? formatRelative(ts) : "—"}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

interface CommandCardProps {
  command: KoraControlCommand;
  isActive: boolean;
  expandable?: boolean;
}

function CommandCard({ command, isActive, expandable = false }: CommandCardProps) {
  const [expanded, setExpanded] = useState(false);
  const isTerminalNonSuccess = TERMINAL_NON_SUCCESS.includes(
    command.lifecycle_state,
  );
  const tone = LEVEL_TONE(command.level);

  return (
    <Card
      className={
        isActive
          ? "border-warning/40"
          : isTerminalNonSuccess
            ? "border-destructive/30"
            : undefined
      }
    >
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex items-start gap-3 flex-wrap">
          {isActive && (
            <span
              className="h-2 w-2 rounded-full bg-warning animate-pulse mt-2"
              aria-label="active"
            />
          )}
          <div className="flex flex-col gap-1 flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <Badge tone={tone}>
                {LEVEL_LABEL(command.level, command.kind)}
              </Badge>
              <Badge tone={LIFECYCLE_TONE[command.lifecycle_state]}>
                {command.lifecycle_state}
              </Badge>
              <span className="text-xs text-muted-foreground">
                seq #{command.sequence}
              </span>
            </div>
            <div className="text-sm">{command.reason}</div>
            <div className="text-xs text-muted-foreground">
              issued by {command.issuer}
            </div>
          </div>
          {expandable && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="text-muted-foreground hover:text-foreground"
              aria-expanded={expanded}
              aria-label={expanded ? "Collapse details" : "Expand details"}
            >
              {expanded ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
            </button>
          )}
        </div>

        {(!expandable || expanded || isActive) && (
          <TimelineStrip command={command} isActive={isActive} />
        )}

        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <code>{command.command_id}</code>
          {command.target_session && (
            <span>target session: {command.target_session}</span>
          )}
          {command.expires_at && (
            <span>
              expires {formatRelative(command.expires_at)} (
              {formatTimestamp(command.expires_at)})
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function activeSorted(commands: KoraControlCommand[]): KoraControlCommand[] {
  // R4.1 "highest open level wins": level desc, then sequence asc.
  return [...commands].sort((a, b) => {
    if (a.level !== b.level) return b.level - a.level;
    return a.sequence - b.sequence;
  });
}

export default function KoraControlPage() {
  const [data, setData] = useState<KoraControlObservedStateResponse | null>(
    null,
  );
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const { toast, showToast } = useToast();

  const loadState = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getKoraControlObservedState()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load kora_control state: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadState(false);
  }, [loadState]);

  const sortedActive = useMemo(
    () => (data ? activeSorted(data.active) : []),
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
          <H2>Kora Control — observed</H2>
          <p className="text-sm text-muted-foreground mt-1">
            STOP-KORA commands as the runtime has observed them. Issuance
            happens in the cockpit.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadState(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load kora_control state</div>
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
                STUB DATA — kora_control reader wire-in pending (KR-P2-J)
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Panel is a UI preview; values shown are sample data, not real
                commands.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Active ────────────────────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <ShieldAlert className="h-4 w-4" />
              Active
              <span className="text-xs text-muted-foreground">
                ({sortedActive.length})
              </span>
            </div>
            {sortedActive.length === 0 ? (
              <Card>
                <CardContent className="py-4 text-sm text-muted-foreground">
                  No active commands. Highest open level wins; if this is empty
                  the runtime is unconstrained.
                </CardContent>
              </Card>
            ) : (
              <>
                {sortedActive.length > 1 && (
                  <p className="text-xs text-muted-foreground -mt-1">
                    Multiple active commands shown sorted by level desc, then
                    sequence asc. Per R4.1 "highest open level wins" the topmost
                    card is the effective constraint.
                  </p>
                )}
                {sortedActive.map((c) => (
                  <CommandCard key={c.command_id} command={c} isActive />
                ))}
              </>
            )}
          </section>

          {/* ── Recently enforced ─────────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Clock className="h-4 w-4" />
              Recently enforced
              <span className="text-xs text-muted-foreground">
                ({data.recently_enforced.length})
              </span>
            </div>
            {data.recently_enforced.length === 0 ? (
              <Card>
                <CardContent className="py-4 text-sm text-muted-foreground">
                  No recently enforced commands.
                </CardContent>
              </Card>
            ) : (
              data.recently_enforced.map((c) => (
                <CommandCard
                  key={c.command_id}
                  command={c}
                  isActive={false}
                  expandable
                />
              ))
            )}
          </section>

          {/* ── History (collapsible) ─────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <button
              type="button"
              onClick={() => setHistoryExpanded((v) => !v)}
              className="flex items-center gap-2 text-sm font-medium w-fit"
              aria-expanded={historyExpanded}
            >
              {historyExpanded ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
              <HistoryIcon className="h-4 w-4" />
              History
              <span className="text-xs text-muted-foreground">
                ({data.history.length})
              </span>
            </button>
            {historyExpanded &&
              (data.history.length === 0 ? (
                <Card>
                  <CardContent className="py-4 text-sm text-muted-foreground">
                    No historical commands.
                  </CardContent>
                </Card>
              ) : (
                <Card>
                  <CardContent className="py-3 overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-left text-xs text-muted-foreground border-b">
                          <th className="py-2 pr-3 font-medium">Created</th>
                          <th className="py-2 pr-3 font-medium">Level</th>
                          <th className="py-2 pr-3 font-medium">Lifecycle</th>
                          <th className="py-2 font-medium">Reason</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...data.history]
                          .sort((a, b) =>
                            (b.created_at ?? "").localeCompare(a.created_at ?? ""),
                          )
                          .map((c) => (
                            <tr
                              key={c.command_id}
                              className="border-b last:border-0 align-top"
                            >
                              <td className="py-2 pr-3 text-xs">
                                <div>{formatRelative(c.created_at)}</div>
                                <div className="text-muted-foreground">
                                  {formatTimestamp(c.created_at)}
                                </div>
                              </td>
                              <td className="py-2 pr-3">
                                <Badge tone={LEVEL_TONE(c.level)}>
                                  {LEVEL_LABEL(c.level, c.kind)}
                                </Badge>
                              </td>
                              <td className="py-2 pr-3">
                                <Badge tone={LIFECYCLE_TONE[c.lifecycle_state]}>
                                  {c.lifecycle_state}
                                </Badge>
                              </td>
                              <td
                                className="py-2 text-xs max-w-md truncate"
                                title={c.reason}
                              >
                                {c.reason}
                              </td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </CardContent>
                </Card>
              ))}
          </section>
        </>
      )}
    </div>
  );
}
