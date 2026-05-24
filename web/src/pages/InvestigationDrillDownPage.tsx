// KR-FE-INVESTIGATION-DRILL-DOWN — operator-comprehension apex view.
//
// Route: /investigations/:callerSessionId (URL-encoded path arg)
//
// Backend: GET /api/investigations/{caller_session_id:path}
//
// One investigation = one caller_session_id. The page joins EVERY
// audit row (across all supported seams) + slack_dm_log entry that
// shares the session id, displays them as a single oldest-first
// chronological timeline. Operator gets the full picture of "what
// happened" for one investigation in one scroll — useful for
// triage, postmortem, trust-building ("did Kora do what I think
// it did?").
//
// Deep-linked from:
//   * KoraActionsPage row → /investigations/{caller_session_id}
//   * ProbeInvestigationsPage card → /investigations/{caller_session_id}
//
// Empty-state framing: a session id with zero matching rows is
// indistinguishable from an invalid id (could be a typo, could be
// a future session that hasn't fired yet). The empty state says
// so explicitly rather than rendering a sad "no data" — operator
// can re-verify the session id without leaving the page.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  ChevronRight,
  Inbox,
  Lightbulb,
  Mail,
  MessageCircle,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  Wrench,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import type {
  InvestigationDrillDownResponse,
  InvestigationDrillKind,
  InvestigationDrillTimelineItem,
} from "@/lib/api";
import {
  formatRelative,
  formatTimestamp,
  type BadgeTone,
} from "@/components/AuditPanelKit";

// Per-seam visual map. Tone matches the same-seam panel pages so
// the operator's mental model is consistent across surfaces.
interface SeamVisual {
  icon: typeof Sparkles;
  tone: BadgeTone;
  label: string;
  /** Deep-link to the per-seam panel for the full audit row. */
  deepLinkPath: string | null;
}

const SEAM_VISUALS: Record<string, SeamVisual> = {
  "probe.wake_requested": {
    icon: Sparkles,
    tone: "warning",
    label: "Probe wake",
    deepLinkPath: "/probe-investigations",
  },
  "reasoning.tool_called": {
    icon: Wrench,
    tone: "outline",
    label: "Tool call",
    deepLinkPath: "/reasoning",
  },
  "tool.probe_autofix_attempted": {
    icon: Wrench,
    tone: "warning",
    label: "Autofix attempted",
    deepLinkPath: "/probe-autofix-log",
  },
  "probe.investigation_completed": {
    icon: Search,
    tone: "secondary",
    label: "Investigation completed",
    deepLinkPath: "/probe-investigations",
  },
  "intent.email_to_sea_ticket": {
    icon: Inbox,
    tone: "outline",
    label: "Email intent",
    deepLinkPath: "/email-intent-log",
  },
  "tool.email_to_operator_sent": {
    icon: Send,
    tone: "success",
    label: "Email sent",
    deepLinkPath: "/outbound-email-log",
  },
  "phrasebook.updated": {
    icon: BookOpen,
    tone: "outline",
    label: "Phrasebook updated",
    deepLinkPath: "/phrasebook",
  },
  "promotion.proposed": {
    icon: Lightbulb,
    tone: "warning",
    label: "Promotion proposed",
    deepLinkPath: "/promotions/phrasebook",
  },
  "promotion.approved": {
    icon: CheckCircle2,
    tone: "success",
    label: "Promotion approved",
    deepLinkPath: "/promotions/phrasebook",
  },
  "promotion.rejected": {
    icon: XCircle,
    tone: "destructive",
    label: "Promotion rejected",
    deepLinkPath: "/promotions/phrasebook",
  },
  "slack_dm_log.jsonl": {
    icon: MessageCircle,
    tone: "secondary",
    label: "Slack DM sent",
    deepLinkPath: "/slack-dm",
  },
};

const DEFAULT_VISUAL: SeamVisual = {
  icon: AlertTriangle,
  tone: "outline",
  label: "Unknown",
  deepLinkPath: null,
};

function seamVisual(seam: string): SeamVisual {
  return SEAM_VISUALS[seam] ?? DEFAULT_VISUAL;
}

function kindHeaderVisual(kind: InvestigationDrillKind): {
  Icon: typeof Sparkles;
  title: string;
} {
  if (kind === "probe") return { Icon: Sparkles, title: "Probe investigation" };
  if (kind === "email") return { Icon: Mail, title: "Email intent flow" };
  if (kind === "promotion")
    return { Icon: Lightbulb, title: "Promotion flow" };
  return { Icon: Search, title: "Investigation" };
}

// ----- Per-row card -----

interface TimelineCardProps {
  item: InvestigationDrillTimelineItem;
}

function TimelineCard({ item }: TimelineCardProps) {
  const v = seamVisual(item.seam);
  const Icon = v.icon;
  const [showRaw, setShowRaw] = useState(false);

  return (
    <Card>
      <CardContent className="p-3 space-y-2">
        <div className="flex items-start gap-3">
          <Icon className="h-4 w-4 mt-0.5 text-muted-foreground flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span
                className="text-xs text-muted-foreground font-mono"
                title={item.emitted_at}
              >
                {formatTimestamp(item.emitted_at)}
              </span>
              <span className="text-[10px] text-muted-foreground">
                ({formatRelative(item.emitted_at)})
              </span>
              <Badge tone={v.tone}>{v.label}</Badge>
              <Badge tone="outline" className="font-mono text-[10px]">
                {item.seam}
              </Badge>
              {item.source && (
                <Badge tone="outline" className="font-mono text-[10px]">
                  source: {item.source}
                </Badge>
              )}
              {v.deepLinkPath && (
                <Link
                  to={v.deepLinkPath}
                  className="ml-auto inline-flex items-center gap-1 text-primary hover:underline text-xs"
                  title={`Open the ${v.label} panel`}
                >
                  panel
                  <ChevronRight className="h-3 w-3" />
                </Link>
              )}
            </div>
            <DetailsSummary item={item} />
            <Button
              size="sm"
              ghost
              className="mt-1 text-[10px] text-muted-foreground hover:text-foreground"
              onClick={() => setShowRaw((v) => !v)}
            >
              {showRaw ? "Hide raw" : "View raw JSON"}
            </Button>
            {showRaw && (
              <pre className="mt-1 text-[10px] font-mono whitespace-pre-wrap break-all rounded bg-muted/30 border border-border p-2 max-h-80 overflow-auto">
                {JSON.stringify(item.details, null, 2)}
              </pre>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function DetailsSummary({
  item,
}: {
  item: InvestigationDrillTimelineItem;
}) {
  const d = item.details;
  // Per-seam summary line — opinionated subset of the projection
  // so the operator can scan the timeline without expanding every
  // card. Falls back to a key=value chip render for unknown seams.
  switch (item.seam) {
    case "probe.wake_requested":
      return (
        <div className="text-sm mt-0.5">
          <span className="font-mono">{String(d.probe ?? "?")}</span> ·
          severity{" "}
          <span className="font-mono">{String(d.severity ?? "?")}</span> ·
          category{" "}
          <span className="font-mono">{String(d.category ?? "?")}</span>
          {Boolean(d.envelope_enabled) && (
            <span className="text-muted-foreground italic">
              {" "}
              · envelope {String(d.envelope_fix_name)}
            </span>
          )}
          {d.title ? (
            <div className="text-xs text-muted-foreground mt-1">
              {String(d.title)}
            </div>
          ) : null}
        </div>
      );
    case "reasoning.tool_called":
      return (
        <div className="text-sm mt-0.5">
          <span className="font-mono">{String(d.tool_name ?? "?")}</span> ·
          status{" "}
          <span className="font-mono">{String(d.tool_status ?? "?")}</span>
          {typeof d.tool_duration_ms === "number" && (
            <span className="text-muted-foreground italic">
              {" "}
              · {d.tool_duration_ms}ms
            </span>
          )}
        </div>
      );
    case "tool.probe_autofix_attempted":
      return (
        <div className="text-sm mt-0.5">
          <span className="font-mono">
            {String(d.action_canonical ?? d.action_taken ?? d.action ?? "?")}
          </span>{" "}
          on{" "}
          <span className="font-mono">
            {String(d.probe ?? "?")}/{String(d.target_id ?? "?")}
          </span>
          {d.before_state_label && d.after_state_label ? (
            <span className="text-muted-foreground italic">
              {" "}
              · {String(d.before_state_label)} →{" "}
              {String(d.after_state_label)}
            </span>
          ) : null}
        </div>
      );
    case "probe.investigation_completed":
      return (
        <div className="text-sm mt-0.5 space-y-1">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge tone="outline" className="font-mono">
              dm: {String(d.dm_status ?? "?")}
            </Badge>
            {Boolean(d.autofix_attempted) && (
              <Badge tone="warning">🔧 fix attempted</Badge>
            )}
            {d.model_used ? (
              <Badge tone="outline" className="font-mono">
                {String(d.model_used)}
              </Badge>
            ) : null}
            {typeof d.total_cost_usd === "number" && (
              <span className="text-muted-foreground italic">
                ${(d.total_cost_usd as number).toFixed(4)}
              </span>
            )}
          </div>
          {d.summary_text ? (
            <div className="text-xs text-muted-foreground whitespace-pre-wrap">
              {String(d.summary_text)}
            </div>
          ) : null}
        </div>
      );
    case "intent.email_to_sea_ticket":
      return (
        <div className="text-sm mt-0.5">
          <Badge tone="outline" className="font-mono text-[10px]">
            {String(d.action ?? "?")}
          </Badge>{" "}
          <span className="text-muted-foreground">
            pattern: <span className="font-mono">{String(d.pattern_matched ?? "")}</span>
          </span>
          {d.subject ? (
            <div className="text-xs mt-1 font-medium">{String(d.subject)}</div>
          ) : null}
        </div>
      );
    case "tool.email_to_operator_sent":
      return (
        <div className="text-sm mt-0.5 text-muted-foreground text-xs">
          status <span className="font-mono">{String(d.status ?? "?")}</span>{" "}
          ·{" "}
          {typeof d.subject_chars === "number"
            ? `${d.subject_chars}-char subject`
            : ""}
          {typeof d.body_chars === "number"
            ? ` · ${d.body_chars}-char body`
            : ""}
        </div>
      );
    case "phrasebook.updated":
      return (
        <div className="text-sm mt-0.5 text-muted-foreground text-xs">
          actor <span className="font-mono">{String(d.actor ?? "?")}</span>
          {typeof d.entry_count_before === "number" &&
          typeof d.entry_count_after === "number"
            ? ` · ${d.entry_count_before}→${d.entry_count_after} entries`
            : ""}
        </div>
      );
    case "promotion.proposed":
    case "promotion.approved":
    case "promotion.rejected":
      return (
        <div className="text-sm mt-0.5">
          <span className="font-mono">{String(d.proposed_category ?? "?")}</span>
          {typeof d.cluster_size === "number" && (
            <span className="text-muted-foreground">
              {" "}
              · cluster {d.cluster_size}
            </span>
          )}
          {typeof d.confidence === "number" && (
            <span className="text-muted-foreground">
              {" "}
              · conf {(d.confidence as number).toFixed(2)}
            </span>
          )}
          {d.proposed_reply_template ? (
            <div className="text-xs text-muted-foreground font-mono mt-1 whitespace-pre-wrap">
              {String(d.proposed_reply_template)}
            </div>
          ) : null}
        </div>
      );
    case "slack_dm_log.jsonl":
      return (
        <div className="text-sm mt-0.5 text-muted-foreground text-xs">
          channel{" "}
          <span className="font-mono">{String(d.channel_id ?? "?")}</span>{" "}
          · send_status{" "}
          <span className="font-mono">{String(d.send_status ?? "?")}</span>
          {d.slack_message_ts ? (
            <span className="ml-1">
              · ts{" "}
              <span className="font-mono">{String(d.slack_message_ts)}</span>
            </span>
          ) : null}
          {d.failure_reason ? (
            <div className="text-destructive font-mono mt-1">
              failure_reason: {String(d.failure_reason)}
            </div>
          ) : null}
        </div>
      );
    default:
      return null;
  }
}

// ----- Page -----

export default function InvestigationDrillDownPage() {
  usePanelView("InvestigationDrillDownPage");

  // The router escapes the param, so React Router gives us the
  // decoded form back automatically. The session id may legitimately
  // contain ":" (probe:fly:service_unhealthy); useParams handles it.
  const params = useParams<{ callerSessionId: string }>();
  const sessionId = params.callerSessionId ?? "";

  const [data, setData] =
    useState<InvestigationDrillDownResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!sessionId) {
      setError("No caller_session_id in URL.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getInvestigationDrillDown(sessionId);
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const header = useMemo(() => {
    const kind = data?.session.kind ?? "other";
    return kindHeaderVisual(kind);
  }, [data?.session.kind]);

  return (
    <div className="space-y-4 p-4 max-w-5xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2 className="flex items-center gap-2">
          <header.Icon className="h-5 w-5" />
          {header.title}
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

      <Card>
        <CardContent className="p-3 text-xs">
          <div className="text-muted-foreground">
            <span className="uppercase tracking-wide text-[10px]">
              caller_session_id
            </span>
            <div className="font-mono text-sm text-foreground break-all">
              {sessionId}
            </div>
          </div>
          {data && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-muted-foreground">
              <Badge tone="outline" className="font-mono text-[10px]">
                {data.timeline.length} timeline events
              </Badge>
              {data.seams_seen.length > 0 && (
                <span className="text-[10px]">
                  joined from{" "}
                  <span className="font-mono">
                    {data.seams_seen.join(" · ")}
                  </span>
                </span>
              )}
            </div>
          )}
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
              Failed to load investigation:{" "}
              <span className="font-mono">{error}</span>
            </div>
          </CardContent>
        </Card>
      )}

      {data && data.timeline.length === 0 && (
        <Card className="border-yellow-500/30 bg-yellow-500/5">
          <CardContent className="p-6 text-sm space-y-2">
            <div className="flex items-center gap-2 font-medium">
              <AlertTriangle className="h-4 w-4 text-yellow-500" />
              No audit rows joined this caller_session_id
            </div>
            <div className="text-muted-foreground text-xs">
              Either the session id is invalid (typo / stale link), or no
              audit row has been emitted with this id yet. Verify the id
              against the row that linked here — the caller_session_id is
              shown on each row in the source panel.
            </div>
            <div className="text-muted-foreground text-xs">
              Supported seams for this drill-down:{" "}
              <span className="font-mono">
                {data.supported_seams.join(", ")}
              </span>{" "}
              · plus <span className="font-mono">slack_dm_log.jsonl</span>{" "}
              outbound entries.
            </div>
          </CardContent>
        </Card>
      )}

      {data && data.timeline.length > 0 && (
        <div className="space-y-2">
          {data.timeline.map((item) => (
            <TimelineCard key={item.id} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}
