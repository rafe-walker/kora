import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  Ban,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Coins,
  PauseCircle,
  RefreshCw,
  Timer,
  XCircle,
  Zap,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import {
  formatLatency,
  formatRelative,
  formatTimestamp,
  timestampAbsoluteUtc,
} from "@/lib/panelHelpers";
import {
  SHOW_MORE_DEFAULT_LIMIT,
  ShowMoreFooter,
} from "@/components/ShowMoreFooter";
import type {
  ReasoningCall,
  ReasoningCostRung,
  ReasoningResponse,
  ReasoningStatus,
} from "@/lib/api";

type Filter = "all" | "ok" | "failed" | "halted";

// Cost-rung pill colours per bucket §3(b): gray/yellow/orange/red.
// Keys are the lowercase CostRung.value wire strings
// (agent/cost_state_holder.py:114-117).
const COST_RUNG_TONE: Record<
  ReasoningCostRung,
  "success" | "warning" | "destructive" | "outline"
> = {
  normal: "outline",
  warn_75: "warning",
  downshift_90: "warning",
  hard_stop_100: "destructive",
  unknown: "outline",
};

const COST_RUNG_LABEL: Record<ReasoningCostRung, string> = {
  normal: "normal",
  warn_75: "warn 75%",
  downshift_90: "downshift 90%",
  hard_stop_100: "hard-stop 100%",
  unknown: "unknown",
};

// Visual cue: cost-rung text color so the rung itself pops
// at-a-glance even without the badge tone.
const COST_RUNG_TEXT_COLOR: Record<ReasoningCostRung, string> = {
  normal: "text-muted-foreground",
  warn_75: "text-warning",
  downshift_90: "text-orange-500",
  hard_stop_100: "text-destructive",
  unknown: "text-muted-foreground",
};

const STATUS_TONE: Record<
  ReasoningStatus,
  "success" | "warning" | "destructive"
> = {
  ok: "success",
  failed: "destructive",
  halted: "destructive",
  paused: "warning",
};

const STATUS_LABEL: Record<ReasoningStatus, string> = {
  ok: "ok",
  failed: "failed",
  halted: "halted",
  paused: "paused",
};

// Model badges color-coded by tier (cost ladder rung mapping):
// opus = top tier (blue), sonnet = downshift mid (purple),
// haiku = downshift deep (gray), null = halted (red).
function ModelBadge({ model }: { model: ReasoningCall["model_used"] }) {
  if (model === null) {
    return (
      <Badge tone="destructive">
        <Ban className="h-3 w-3" />
        <span className="ml-1">no model · halted</span>
      </Badge>
    );
  }
  // Tier-tinted text + outline rather than full destructive on every
  // sonnet/haiku call — those are normal downshifts, not failures.
  const tierClass =
    model === "claude-opus-4-7"
      ? "text-blue-500"
      : model === "claude-sonnet-4-6"
        ? "text-purple-500"
        : "text-muted-foreground";
  return (
    <Badge tone="outline">
      <span className={`font-mono text-[10px] ${tierClass}`}>{model}</span>
    </Badge>
  );
}

function StatusIcon({ status }: { status: ReasoningStatus }) {
  switch (status) {
    case "ok":
      return <CheckCircle2 className="h-3.5 w-3.5 text-success" />;
    case "failed":
      return <AlertOctagon className="h-3.5 w-3.5 text-destructive" />;
    case "halted":
      return <Ban className="h-3.5 w-3.5 text-destructive" />;
    case "paused":
      return <PauseCircle className="h-3.5 w-3.5 text-warning" />;
  }
}


// Cap visual bar at 5s — anything beyond is "long" regardless of
// exact value; the operator just needs the "this took real time" cue.
function durationBarWidth(ms: number): string {
  const capped = Math.min(ms, 5000);
  return `${(capped / 5000) * 100}%`;
}

const COLLAPSED_TEXT_MAX = 80;

function truncateText(s: string, max: number = COLLAPSED_TEXT_MAX): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + "…";
}

interface CallRowProps {
  call: ReasoningCall;
  expanded: boolean;
  onToggle: () => void;
}

function CallRow({ call, expanded, onToggle }: CallRowProps) {
  const isSlow = call.duration_ms >= 2000;
  const hasText = call.response_text_truncated_200 !== null;
  const needsExpand =
    hasText && call.response_text_truncated_200!.length > COLLAPSED_TEXT_MAX;
  return (
    <Card>
      <CardContent className="flex flex-col gap-2 py-3">
        <button
          type="button"
          onClick={onToggle}
          className="flex items-start gap-3 text-left w-full"
          aria-expanded={expanded}
        >
          {needsExpand ? (
            expanded ? (
              <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0 mt-1" />
            ) : (
              <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0 mt-1" />
            )
          ) : (
            <span className="w-3 shrink-0" />
          )}
          <StatusIcon status={call.status} />
          <div className="flex flex-col gap-1 flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap text-xs">
              <ModelBadge model={call.model_used} />
              <Badge tone={COST_RUNG_TONE[call.cost_rung_at_call]}>
                <Coins className="h-3 w-3" />
                <span
                  className={`ml-1 ${COST_RUNG_TEXT_COLOR[call.cost_rung_at_call]}`}
                >
                  {COST_RUNG_LABEL[call.cost_rung_at_call]}
                </span>
              </Badge>
              {call.status !== "ok" && (
                <Badge tone={STATUS_TONE[call.status]}>
                  {STATUS_LABEL[call.status]}
                </Badge>
              )}
              {call.error_code && (
                <Badge tone="destructive">
                  <code className="font-mono text-[10px]">
                    {call.error_code}
                  </code>
                </Badge>
              )}
              <span className="text-muted-foreground flex items-center gap-1 ml-auto">
                <Clock className="h-3 w-3" />
                <span title={timestampAbsoluteUtc(call.started_at)}>
                  {formatRelative(call.started_at)}
                </span>
              </span>
            </div>
            <div className="flex items-center gap-3 text-xs">
              <span
                className={`flex items-center gap-1 font-mono ${
                  isSlow ? "text-warning" : "text-muted-foreground"
                }`}
                title={`${call.duration_ms} ms`}
              >
                <Timer className="h-3 w-3" />
                {formatLatency(call.duration_ms)}
              </span>
              <span className="flex-1 max-w-[160px] h-1 bg-muted rounded-full overflow-hidden">
                <span
                  className={`block h-full ${isSlow ? "bg-warning" : "bg-primary"}`}
                  style={{ width: durationBarWidth(call.duration_ms) }}
                />
              </span>
              <span className="font-mono text-muted-foreground">
                <Zap className="h-3 w-3 inline mr-0.5" />
                {call.input_tokens} → {call.output_tokens}
              </span>
            </div>
            {/* Response excerpt — plain text. JSX child expression
                so React's default escaping defangs any HTML / markdown
                / script in Kora's generated text. HARD CONSTRAINT
                (bucket §3(a) SECURITY layer 1): NEVER switch to
                dangerouslySetInnerHTML here. */}
            {hasText && (
              <div className="text-sm whitespace-pre-wrap break-words">
                {expanded
                  ? call.response_text_truncated_200
                  : truncateText(call.response_text_truncated_200!)}
              </div>
            )}
          </div>
        </button>

        {expanded && (
          <div className="ml-10 flex flex-col gap-1.5 pt-2 border-t border-border text-xs">
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                started_at
              </span>
              <span title={timestampAbsoluteUtc(call.started_at)}>
                {formatTimestamp(call.started_at)}
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                triggered_by
              </span>
              <code className="font-mono">{call.triggered_by}</code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                model_used
              </span>
              <code className="font-mono">
                {call.model_used ?? (
                  <span className="text-muted-foreground italic not-italic">
                    null (no SDK call)
                  </span>
                )}
              </code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                cost_rung_at_call
              </span>
              <code
                className={`font-mono ${COST_RUNG_TEXT_COLOR[call.cost_rung_at_call]}`}
              >
                {call.cost_rung_at_call}
              </code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                tokens
              </span>
              <span className="font-mono">
                {call.input_tokens} in → {call.output_tokens} out
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                duration
              </span>
              <span className="font-mono">{call.duration_ms} ms</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">
                status
              </span>
              <span className="flex items-center gap-1.5">
                <StatusIcon status={call.status} />
                {STATUS_LABEL[call.status]}
              </span>
            </div>
            {call.error_code && (
              <div className="flex gap-2">
                <span className="text-muted-foreground min-w-[130px]">
                  error_code
                </span>
                <code className="font-mono text-destructive">
                  {call.error_code}
                </code>
              </div>
            )}
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[130px]">id</span>
              <code className="font-mono">{call.id}</code>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function matchesFilter(call: ReasoningCall, filter: Filter): boolean {
  if (filter === "all") return true;
  return call.status === filter;
}

export default function ReasoningPanel() {
  const [data, setData] = useState<ReasoningResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<Filter>("all");
  const [limit, setLimit] = useState<number>(SHOW_MORE_DEFAULT_LIMIT);
  const { toast, showToast } = useToast();

  const loadReasoning = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getRecentReasoning(limit)
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load reasoning activity: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast, limit],
  );

  useEffect(() => {
    loadReasoning(false);
  }, [loadReasoning]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const orderedCalls = useMemo(() => {
    if (!data) return [];
    return [...data.calls].sort((a, b) => {
      const ta = new Date(a.started_at).getTime();
      const tb = new Date(b.started_at).getTime();
      if (ta !== tb) return tb - ta;
      return a.id < b.id ? 1 : -1;
    });
  }, [data]);

  const filterCounts = useMemo(() => {
    if (!data) return { all: 0, ok: 0, failed: 0, halted: 0 };
    return {
      all: data.calls.length,
      ok: data.calls.filter((c) => c.status === "ok").length,
      failed: data.calls.filter((c) => c.status === "failed").length,
      halted: data.calls.filter((c) => c.status === "halted").length,
    };
  }, [data]);

  const visibleCalls = useMemo(
    () => orderedCalls.filter((c) => matchesFilter(c, filter)),
    [orderedCalls, filter],
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
          <H2>Kora Reasoning Activity</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Recent ReasoningEngine calls — model, tokens, cost rung,
            errors.
          </p>
        </div>
        <Button
          size="sm"
          ghost
          disabled={refreshing}
          onClick={() => loadReasoning(true)}
        >
          <RefreshCw
            className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`}
          />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">
                Failed to load reasoning activity
              </div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <Brain className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — real data wires in via CC#3's
                KR-FEAT-AI-RESPONSE-LOOP ST2 follow-on
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample calls (deliberately
                spanning ok @ normal / ok @ warn_75 / halted at
                hard_stop_100 / sdk_timeout failure so the operator
                sees the cost-ladder behaviour + error taxonomy).
                Real data flips once ST2 extends{" "}
                <code>
                  ${"{HERMES_HOME}"}/slack_dm_log.jsonl
                </code>{" "}
                with the reasoning fields.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Stats strip (4 columns per spec §3(b)) ─────────── */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <Card>
              <CardContent className="py-3 flex flex-col gap-1">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">
                  Total calls / 24h
                </span>
                <span className="text-xl font-semibold">
                  {data.total_recent_24h}
                </span>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="py-3 flex flex-col gap-1">
                <span className="text-xs text-muted-foreground uppercase tracking-wide flex items-center gap-1">
                  <Zap className="h-3 w-3" />
                  Token spend / 24h
                </span>
                <span className="text-xl font-semibold font-mono">
                  {data.tokens_total_24h.input.toLocaleString()} →{" "}
                  {data.tokens_total_24h.output.toLocaleString()}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  input → output
                </span>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="py-3 flex flex-col gap-1">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">
                  Model distribution / 24h
                </span>
                <div className="flex flex-wrap gap-1.5 mt-0.5">
                  {Object.entries(data.by_model_24h)
                    .filter(([, count]) => count > 0)
                    .map(([model, count]) => (
                      <Badge key={model} tone="outline">
                        <span className="font-mono text-[10px]">
                          {model === "halted_no_model" ? "halted" : model}
                        </span>
                        <span className="ml-1 text-muted-foreground">
                          {count}
                        </span>
                      </Badge>
                    ))}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="py-3 flex flex-col gap-1">
                <span className="text-xs text-muted-foreground uppercase tracking-wide">
                  Status distribution / 24h
                </span>
                <div className="flex flex-wrap gap-1.5 mt-0.5">
                  {Object.entries(data.by_status_24h).map(([status, count]) => {
                    const s = status as ReasoningStatus;
                    return (
                      <Badge
                        key={status}
                        tone={
                          status === "ok"
                            ? "success"
                            : status === "halted" || status === "failed"
                              ? "destructive"
                              : "warning"
                        }
                      >
                        {STATUS_LABEL[s] ?? status} {count}
                      </Badge>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          </div>

          {/* ── Filter pills ─────────────────────────────────── */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-3 text-sm">
              <span className="text-xs text-muted-foreground uppercase tracking-wide">
                view
              </span>
              <div className="flex flex-wrap gap-1.5">
                {(
                  [
                    ["all", `all (${filterCounts.all})`],
                    ["ok", `ok (${filterCounts.ok})`],
                    ["failed", `failed (${filterCounts.failed})`],
                    ["halted", `halted (${filterCounts.halted})`],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setFilter(key)}
                    className={`px-2 py-0.5 rounded-full text-xs border ${
                      filter === key
                        ? "bg-primary text-primary-foreground border-primary"
                        : "border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </CardContent>
          </Card>

          {/* ── Timeline (newest first) ──────────────────────── */}
          {/* Empty-state convergence (KR-FE-OPS-QUALITY-PASS): an
              idle Kora (no recent reasoning calls) is a healthy
              steady-state — positive reinforcement instead of the
              data-absence neutral. */}
          {data.calls.length === 0 ? (
            <Card className="border-success/30 bg-success/5">
              <CardContent className="py-8 text-center text-sm">
                <CheckCircle2 className="h-6 w-6 mx-auto mb-2 text-success" />
                <div className="font-medium">No reasoning activity.</div>
                <div className="text-xs text-muted-foreground mt-0.5">
                  Kora is idle.
                </div>
              </CardContent>
            </Card>
          ) : visibleCalls.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                <XCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
                No calls match the current filter.
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {visibleCalls.map((c) => (
                <CallRow
                  key={c.id}
                  call={c}
                  expanded={expandedIds.has(c.id)}
                  onToggle={() => toggleExpand(c.id)}
                />
              ))}
            </div>
          )}
          <ShowMoreFooter
            currentLimit={limit}
            totalShown={data.calls.length}
            onShowMore={setLimit}
            unitLabel="calls"
          />
        </>
      )}
    </div>
  );
}
