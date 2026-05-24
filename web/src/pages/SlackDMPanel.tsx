import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Hash,
  MessageCircle,
  RefreshCw,
  ShieldOff,
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
import { formatRelative, formatTimestamp } from "@/lib/panelHelpers";
import { usePanelView } from "@/hooks/usePanelView";
import type {
  SlackDMDirection,
  SlackDMHandledStatus,
  SlackDMMessage,
  SlackDMResponse,
} from "@/lib/api";

type Filter =
  | "all"
  | "inbound"
  | "outbound"
  | "filtered"
  | "errors";

const STATUS_TONE: Record<
  SlackDMHandledStatus,
  "success" | "warning" | "destructive" | "outline"
> = {
  received: "success",
  sent_ok: "success",
  sent_failed: "destructive",
  filtered_non_joshua: "warning",
  filtered_bot: "outline",
  filtered_subtype: "outline",
  handler_error: "destructive",
  dropped_paused: "warning",
};

const STATUS_LABEL: Record<SlackDMHandledStatus, string> = {
  received: "received",
  sent_ok: "sent ok",
  sent_failed: "send failed",
  filtered_non_joshua: "filtered · non-Joshua",
  filtered_bot: "filtered · bot",
  filtered_subtype: "filtered · subtype",
  handler_error: "handler error",
  dropped_paused: "dropped · paused",
};

// Statuses that visually pop in the timeline. received / sent_ok are
// the happy path and the row's direction arrow already conveys them;
// we only render a status badge for non-default states.
const NON_DEFAULT_STATUSES: Set<SlackDMHandledStatus> = new Set([
  "sent_failed",
  "filtered_non_joshua",
  "filtered_bot",
  "filtered_subtype",
  "handler_error",
  "dropped_paused",
]);

const FILTERED_STATUSES: Set<SlackDMHandledStatus> = new Set([
  "filtered_non_joshua",
  "filtered_bot",
  "filtered_subtype",
]);

const ERROR_STATUSES: Set<SlackDMHandledStatus> = new Set([
  "sent_failed",
  "handler_error",
  "dropped_paused",
]);

const COLLAPSED_TEXT_MAX = 120;

function StatusIcon({ status }: { status: SlackDMHandledStatus }) {
  switch (status) {
    case "received":
      return <CheckCircle2 className="h-3.5 w-3.5 text-success" />;
    case "sent_ok":
      return <CheckCircle2 className="h-3.5 w-3.5 text-success" />;
    case "sent_failed":
      return <AlertOctagon className="h-3.5 w-3.5 text-destructive" />;
    case "filtered_non_joshua":
      return <ShieldOff className="h-3.5 w-3.5 text-warning" />;
    case "filtered_bot":
      return <Ban className="h-3.5 w-3.5 text-muted-foreground" />;
    case "filtered_subtype":
      return <Ban className="h-3.5 w-3.5 text-muted-foreground" />;
    case "handler_error":
      return <AlertOctagon className="h-3.5 w-3.5 text-destructive" />;
    case "dropped_paused":
      return <XCircle className="h-3.5 w-3.5 text-warning" />;
  }
}

function DirectionIcon({ direction }: { direction: SlackDMDirection }) {
  return direction === "inbound" ? (
    <ArrowDownLeft className="h-4 w-4 text-primary" />
  ) : (
    <ArrowUpRight className="h-4 w-4 text-muted-foreground" />
  );
}

function truncateText(s: string, max: number = COLLAPSED_TEXT_MAX): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + "…";
}

interface MessageRowProps {
  message: SlackDMMessage;
  expanded: boolean;
  onToggle: () => void;
}

function MessageRow({ message, expanded, onToggle }: MessageRowProps) {
  const needsExpand = message.text.length > COLLAPSED_TEXT_MAX;
  const showStatusBadge = NON_DEFAULT_STATUSES.has(message.handled_status);
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
          <DirectionIcon direction={message.direction} />
          <div className="flex flex-col gap-1 flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap text-xs">
              <Badge tone="outline">{message.user_id_label}</Badge>
              {showStatusBadge && (
                <Badge tone={STATUS_TONE[message.handled_status]}>
                  <StatusIcon status={message.handled_status} />
                  <span className="ml-1">
                    {STATUS_LABEL[message.handled_status]}
                  </span>
                </Badge>
              )}
              <span className="text-muted-foreground flex items-center gap-1 ml-auto">
                <Clock className="h-3 w-3" />
                <span title={formatTimestamp(message.timestamp)}>
                  {formatRelative(message.timestamp)}
                </span>
              </span>
            </div>
            {/* Message text. Rendered as a JSX child expression —
                React's default escaping defangs any HTML/markdown/
                script content. HARD CONSTRAINT: NEVER switch this
                to dangerouslySetInnerHTML — real DM text may contain
                arbitrary user-typed content. */}
            <div className="text-sm whitespace-pre-wrap break-words">
              {expanded ? message.text : truncateText(message.text)}
            </div>
          </div>
        </button>

        {expanded && (
          <div className="ml-10 flex flex-col gap-1.5 pt-2 border-t border-border text-xs">
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[110px]">
                timestamp
              </span>
              <span>{formatTimestamp(message.timestamp)}</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[110px]">
                channel_id
              </span>
              <code className="font-mono flex items-center gap-1">
                <Hash className="h-3 w-3" />
                {message.channel_id}
              </code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[110px]">
                thread_ts
              </span>
              {message.thread_ts ? (
                <code className="font-mono">{message.thread_ts}</code>
              ) : (
                <span className="text-muted-foreground italic">
                  (top-level message)
                </span>
              )}
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[110px]">
                handled_status
              </span>
              <span className="flex items-center gap-1.5">
                <StatusIcon status={message.handled_status} />
                {STATUS_LABEL[message.handled_status]}
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[110px]">id</span>
              <code className="font-mono">{message.id}</code>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function matchesFilter(message: SlackDMMessage, filter: Filter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "inbound":
      return message.direction === "inbound";
    case "outbound":
      return message.direction === "outbound";
    case "filtered":
      return FILTERED_STATUSES.has(message.handled_status);
    case "errors":
      return ERROR_STATUSES.has(message.handled_status);
  }
}

export default function SlackDMPanel() {
  usePanelView("SlackDMPanel");

  const [data, setData] = useState<SlackDMResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<Filter>("all");
  const { toast, showToast } = useToast();

  const loadDM = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getRecentSlackDM()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load Slack DMs: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadDM(false);
  }, [loadDM]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Newest-first ordering (matches other panels; reads like a recent-
  // activity log rather than a chat scroll). Stable sort via getTime
  // descending; falls back to id order when timestamps tie.
  const orderedMessages = useMemo(() => {
    if (!data) return [];
    return [...data.messages].sort((a, b) => {
      const ta = new Date(a.timestamp).getTime();
      const tb = new Date(b.timestamp).getTime();
      if (ta !== tb) return tb - ta;
      return a.id < b.id ? 1 : -1;
    });
  }, [data]);

  const filterCounts = useMemo(() => {
    if (!data) {
      return { all: 0, inbound: 0, outbound: 0, filtered: 0, errors: 0 };
    }
    return {
      all: data.messages.length,
      inbound: data.messages.filter((m) => m.direction === "inbound").length,
      outbound: data.messages.filter((m) => m.direction === "outbound").length,
      filtered: data.messages.filter((m) =>
        FILTERED_STATUSES.has(m.handled_status),
      ).length,
      errors: data.messages.filter((m) =>
        ERROR_STATUSES.has(m.handled_status),
      ).length,
    };
  }, [data]);

  const visibleMessages = useMemo(
    () => orderedMessages.filter((m) => matchesFilter(m, filter)),
    [orderedMessages, filter],
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
          <H2>Slack DM ↔ Joshua</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Recent direct-message exchanges between Joshua and the Kora bot.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadDM(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load Slack DMs</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <MessageCircle className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — real data wires in via CC#3's KR-FEAT-SLACK-DM ST2
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample messages (deliberately
                spanning inbound / outbound / filtered_non_joshua so the
                operator sees what the filtering posture looks like). ST2
                swaps the endpoint body to read from{" "}
                <code>${"{HERMES_HOME}"}/slack_dm_log.jsonl</code>.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Aggregate summary strip ─────────────────────────── */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
              <MessageCircle className="h-4 w-4 text-primary" />
              <span className="font-medium">
                {data.total_recent_24h} message
                {data.total_recent_24h === 1 ? "" : "s"} / 24h
              </span>
              <span className="flex items-center gap-1.5 text-xs">
                <ArrowDownLeft className="h-3.5 w-3.5 text-primary" />
                {data.by_direction_24h.inbound} inbound
              </span>
              <span className="flex items-center gap-1.5 text-xs">
                <ArrowUpRight className="h-3.5 w-3.5 text-muted-foreground" />
                {data.by_direction_24h.outbound} outbound
              </span>
              {Object.entries(data.by_status_24h).map(([status, count]) => {
                const isFiltered = FILTERED_STATUSES.has(
                  status as SlackDMHandledStatus,
                );
                const isError = ERROR_STATUSES.has(
                  status as SlackDMHandledStatus,
                );
                if (!isFiltered && !isError) return null;
                return (
                  <span
                    key={status}
                    className={`flex items-center gap-1.5 text-xs ${
                      isError ? "text-destructive" : "text-warning"
                    }`}
                  >
                    {isError ? (
                      <AlertOctagon className="h-3.5 w-3.5" />
                    ) : (
                      <ShieldOff className="h-3.5 w-3.5" />
                    )}
                    {count} {status.replaceAll("_", " ")}
                  </span>
                );
              })}
              <span className="text-xs text-muted-foreground ml-auto">
                generated {formatRelative(data.generated_at)} (
                {formatTimestamp(data.generated_at)})
              </span>
            </CardContent>
          </Card>

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
                    ["inbound", `inbound (${filterCounts.inbound})`],
                    ["outbound", `outbound (${filterCounts.outbound})`],
                    ["filtered", `filtered (${filterCounts.filtered})`],
                    ["errors", `errors (${filterCounts.errors})`],
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
          {data.messages.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                <MessageCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
                No DMs yet. Slack app config + setup at:{" "}
                <code className="font-mono">
                  kora_docs/15_status_and_roadmap/slack_app_setup_runbook.md
                </code>
                .
              </CardContent>
            </Card>
          ) : visibleMessages.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                <XCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
                No messages match the current filter.
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {visibleMessages.map((m) => (
                <MessageRow
                  key={m.id}
                  message={m}
                  expanded={expandedIds.has(m.id)}
                  onToggle={() => toggleExpand(m.id)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
