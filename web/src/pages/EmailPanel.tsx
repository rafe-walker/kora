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
  Code2,
  Mail,
  Paperclip,
  RefreshCw,
  ShieldAlert,
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
  EmailDirection,
  EmailHandledStatus,
  EmailMessage,
  EmailResponse,
} from "@/lib/api";

type Filter = "all" | "inbound" | "outbound" | "filtered" | "errors";

const STATUS_TONE: Record<
  EmailHandledStatus,
  "success" | "warning" | "destructive" | "outline"
> = {
  received: "success",
  sent_ok: "success",
  sent_failed: "destructive",
  filtered_non_allowlist: "warning",
  filtered_wrong_recipient: "warning",
  dropped_paused: "warning",
  handler_error: "destructive",
};

const STATUS_LABEL: Record<EmailHandledStatus, string> = {
  received: "received",
  sent_ok: "sent ok",
  sent_failed: "send failed",
  filtered_non_allowlist: "filtered · non-allowlist",
  filtered_wrong_recipient: "filtered · wrong recipient",
  dropped_paused: "dropped · paused",
  handler_error: "handler error",
};

// Statuses that visually pop. received / sent_ok are the happy path
// and the direction arrow already conveys them; only show a badge
// for non-default states.
const NON_DEFAULT_STATUSES: Set<EmailHandledStatus> = new Set([
  "sent_failed",
  "filtered_non_allowlist",
  "filtered_wrong_recipient",
  "dropped_paused",
  "handler_error",
]);

const FILTERED_STATUSES: Set<EmailHandledStatus> = new Set([
  "filtered_non_allowlist",
  "filtered_wrong_recipient",
]);

const ERROR_STATUSES: Set<EmailHandledStatus> = new Set([
  "sent_failed",
  "handler_error",
  "dropped_paused",
]);

// Spec §2(b): collapsed body excerpt ~80 chars, expandable to full
// 400-char truncated body. Backend already capped at 400; we never
// fetch more than that — even the "expanded" view tops out at 400
// (operator pulls full body from Purelymail web client if needed).
const COLLAPSED_BODY_MAX = 80;

function StatusIcon({ status }: { status: EmailHandledStatus }) {
  switch (status) {
    case "received":
    case "sent_ok":
      return <CheckCircle2 className="h-3.5 w-3.5 text-success" />;
    case "sent_failed":
    case "handler_error":
      return <AlertOctagon className="h-3.5 w-3.5 text-destructive" />;
    case "filtered_non_allowlist":
      return <ShieldOff className="h-3.5 w-3.5 text-warning" />;
    case "filtered_wrong_recipient":
      return <Ban className="h-3.5 w-3.5 text-warning" />;
    case "dropped_paused":
      return <XCircle className="h-3.5 w-3.5 text-warning" />;
  }
}

function DirectionIcon({ direction }: { direction: EmailDirection }) {
  return direction === "inbound" ? (
    <ArrowDownLeft className="h-4 w-4 text-primary" />
  ) : (
    <ArrowUpRight className="h-4 w-4 text-muted-foreground" />
  );
}

function truncateBody(s: string, max: number = COLLAPSED_BODY_MAX): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + "…";
}

interface MessageRowProps {
  message: EmailMessage;
  expanded: boolean;
  onToggle: () => void;
}

function MessageRow({ message, expanded, onToggle }: MessageRowProps) {
  const needsExpand = message.body_text_truncated_400.length > COLLAPSED_BODY_MAX;
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
              <Badge tone="outline">
                {message.from_label} → {message.to_label}
              </Badge>
              {showStatusBadge && (
                <Badge tone={STATUS_TONE[message.handled_status]}>
                  <StatusIcon status={message.handled_status} />
                  <span className="ml-1">
                    {STATUS_LABEL[message.handled_status]}
                  </span>
                </Badge>
              )}
              {/* Spoofing chip: same destructive tone as agent-activity
                  denied — surfaces DMARC/SPF red flags that warrant
                  operator attention before opening the message. */}
              {message.spoofing_warning && (
                <Badge tone="destructive">
                  <ShieldAlert className="h-3 w-3" />
                  <span className="ml-1">spoofing risk</span>
                </Badge>
              )}
              {message.attachments_count > 0 && (
                <Badge tone="outline">
                  <Paperclip className="h-3 w-3" />
                  <span className="ml-1">
                    {message.attachments_count}
                  </span>
                </Badge>
              )}
              {message.has_html && (
                <Badge
                  tone="outline"
                  title="Original body contained HTML — rendered as plain text only"
                >
                  <Code2 className="h-3 w-3" />
                  <span className="ml-1">HTML</span>
                </Badge>
              )}
              <span className="text-muted-foreground flex items-center gap-1 ml-auto">
                <Clock className="h-3 w-3" />
                <span title={formatTimestamp(message.timestamp)}>
                  {formatRelative(message.timestamp)}
                </span>
              </span>
            </div>
            <div className="text-sm font-mono break-words">
              {message.subject}
            </div>
            {/* Body excerpt. Rendered as a JSX child expression —
                React's default escaping defangs any HTML/markdown/
                script content. HARD CONSTRAINT: NEVER switch this
                to dangerouslySetInnerHTML — real email bodies may
                contain arbitrary HTML / phishing payloads. Full
                HTML rendering happens in the Purelymail web client,
                NOT here. */}
            <div className="text-xs text-muted-foreground whitespace-pre-wrap break-words">
              {expanded
                ? message.body_text_truncated_400
                : truncateBody(message.body_text_truncated_400)}
            </div>
          </div>
        </button>

        {expanded && (
          <div className="ml-10 flex flex-col gap-1.5 pt-2 border-t border-border text-xs">
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                timestamp
              </span>
              <span>{formatTimestamp(message.timestamp)}</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                message_id
              </span>
              <code className="font-mono break-all">{message.message_id}</code>
            </div>
            {message.in_reply_to && (
              <div className="flex gap-2">
                <span className="text-muted-foreground min-w-[120px]">
                  in_reply_to
                </span>
                <code className="font-mono break-all">
                  {message.in_reply_to}
                </code>
              </div>
            )}
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                handled_status
              </span>
              <span className="flex items-center gap-1.5">
                <StatusIcon status={message.handled_status} />
                {STATUS_LABEL[message.handled_status]}
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                has_html
              </span>
              <span>
                {message.has_html ? "yes" : "no"}
                {message.has_html && (
                  <span className="text-muted-foreground italic ml-2">
                    (rendered as plain text here; pull full HTML from
                    Purelymail web client)
                  </span>
                )}
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                attachments
              </span>
              <span>
                {message.attachments_count === 0
                  ? "none"
                  : `${message.attachments_count} (download via Purelymail)`}
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">id</span>
              <code className="font-mono">{message.id}</code>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function matchesFilter(message: EmailMessage, filter: Filter): boolean {
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

export default function EmailPanel() {
  usePanelView("EmailPanel");

  const [data, setData] = useState<EmailResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<Filter>("all");
  const { toast, showToast } = useToast();

  const loadEmail = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getRecentEmail()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load email: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadEmail(false);
  }, [loadEmail]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

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
          <H2>Email ↔ Joshua</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Recent inbound + outbound email via Purelymail.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadEmail(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load email</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <Mail className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — real data wires in via CC#1's KR-FEAT-EMAIL ST2
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample messages (spanning
                inbound / outbound / filtered_non_allowlist / inbound
                with attachment). ST2 swaps the endpoint body to read
                from{" "}
                <code>${"{HERMES_HOME}"}/email_inbound_log.jsonl</code>
                {" + "}
                <code>${"{HERMES_HOME}"}/email_outbound_log.jsonl</code>.
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
              <Mail className="h-4 w-4 text-primary" />
              <span className="font-medium">
                {data.total_recent_24h} email
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
                  status as EmailHandledStatus,
                );
                const isError = ERROR_STATUSES.has(
                  status as EmailHandledStatus,
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
                <Mail className="h-6 w-6 mx-auto mb-2 opacity-50" />
                No emails yet. Purelymail config + setup at:{" "}
                <code className="font-mono">
                  kora_docs/15_status_and_roadmap/purelymail_setup_runbook.md
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
