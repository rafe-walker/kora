// Apex "what did Kora do today" timeline —
// KR-FE-KORA-ACTIONS-AGGREGATED-PANEL (+ KR-FE-KORA-ACTIONS-EXTENDED-SEAMS).
//
// 4th consumer of AuditPanelKit. Single-page operator-trust view
// that joins ALL mutating-action audit seams into one
// chronological timeline. Categories cover the mutating actions
// Kora can take + an "other" forward-compat catch-all:
//
//   * email_sent              ← tool.email_to_operator_sent (#179)
//   * sea_ticket_created      ← intent.email_to_sea_ticket (#176, action=created only)
//   * autofix_attempted       ← tool.probe_autofix_attempted (#182, status=attempted only)
//   * investigation_completed ← probe.investigation_completed (#184 — productive now)
//   * phrasebook_proposal_approved ← phrasebook.updated (#177, actor != operator)
//   * promotion_proposed      ← promotion.proposed (#186 — Kora-generated phrasebook proposals)
//   * promotion_approved      ← promotion.approved (#186 — operator approval flow)
//   * promotion_rejected      ← promotion.rejected (#186 — operator rejection flow)
//   * other (catch-all)
//
// Per-row: chronological card with action_category color-coded
// chip + composed one-line summary + deep-link to the per-seam
// panel for the full row detail. Empty state: "Kora has been
// quiet today." Promotion-loop rows deep-link to PromotionReviewPage
// via /promotions/phrasebook?focus=<proposal_id>.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  BellRing,
  BookOpen,
  CheckCircle2,
  ExternalLink,
  Inbox,
  Lightbulb,
  RefreshCw,
  Search,
  Send,
  Wrench,
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
  KORA_ACTION_CATEGORIES,
  type KoraActionCategory,
  type KoraActionItem,
  type KoraActionsResponse,
} from "@/lib/api";
import {
  EmptyFilteredMessage,
  FilterChips,
  Sparkline,
  SummaryChips,
  formatRelative,
  formatTimestamp,
  type CategoryDef,
  type FilterValue,
} from "@/components/AuditPanelKit";

// Visual definition per action category. Icon choice mirrors the
// per-seam panel's icon (Send for email, Inbox for intent,
// Wrench for autofix, Search for investigation, BookOpen for
// phrasebook, Lightbulb for promotion-proposed —matches the
// PromotionReviewPage page title icon) so operator's mental model
// is consistent across panels.
const KORA_ACTION_CATEGORIES_DEFS: readonly CategoryDef<KoraActionCategory>[] = [
  {
    key: "email_sent",
    label: "Email sent",
    tone: "success",
    Icon: Send,
  },
  {
    key: "sea_ticket_created",
    label: "Sea_Ticket created",
    tone: "secondary",
    Icon: Inbox,
  },
  {
    key: "autofix_attempted",
    label: "Autofix attempted",
    tone: "warning",
    Icon: Wrench,
  },
  {
    key: "investigation_completed",
    label: "Investigation completed",
    tone: "outline",
    Icon: Search,
  },
  {
    // KR-FE-ALERT-INVESTIGATIONS-VIEWER (forward-compat #420)
    key: "alert_investigation_completed",
    label: "Alert investigation completed",
    tone: "warning",
    Icon: BellRing,
  },
  {
    key: "phrasebook_proposal_approved",
    label: "Phrasebook proposal",
    tone: "outline",
    Icon: BookOpen,
  },
  {
    key: "promotion_proposed",
    label: "Promotion proposed",
    tone: "warning",
    Icon: Lightbulb,
  },
  {
    key: "promotion_approved",
    label: "Promotion approved",
    tone: "success",
    Icon: CheckCircle2,
  },
  {
    key: "promotion_rejected",
    label: "Promotion rejected",
    tone: "destructive",
    Icon: XCircle,
  },
  {
    key: "other",
    label: "Other",
    tone: "outline",
    Icon: AlertTriangle,
  },
];

const KORA_ACTION_CATEGORIES_MAP: Record<
  KoraActionCategory,
  CategoryDef<KoraActionCategory>
> = {
  email_sent: KORA_ACTION_CATEGORIES_DEFS[0],
  sea_ticket_created: KORA_ACTION_CATEGORIES_DEFS[1],
  autofix_attempted: KORA_ACTION_CATEGORIES_DEFS[2],
  investigation_completed: KORA_ACTION_CATEGORIES_DEFS[3],
  alert_investigation_completed: KORA_ACTION_CATEGORIES_DEFS[4],
  phrasebook_proposal_approved: KORA_ACTION_CATEGORIES_DEFS[5],
  promotion_proposed: KORA_ACTION_CATEGORIES_DEFS[6],
  promotion_approved: KORA_ACTION_CATEGORIES_DEFS[7],
  promotion_rejected: KORA_ACTION_CATEGORIES_DEFS[8],
  other: KORA_ACTION_CATEGORIES_DEFS[9],
};

// ----- Per-row card -----

function ActionCard({ item }: { item: KoraActionItem }) {
  const v =
    KORA_ACTION_CATEGORIES_MAP[item.action_category] ??
    KORA_ACTION_CATEGORIES_MAP.other;
  const Icon = v.Icon;
  return (
    <Card>
      <CardContent className="p-3 flex items-start gap-3">
        <Icon className="h-4 w-4 mt-0.5 text-muted-foreground flex-shrink-0" />
        <div className="flex-1 min-w-0 flex flex-col gap-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-xs text-muted-foreground"
              title={formatTimestamp(item.emitted_at)}
            >
              {formatRelative(item.emitted_at)}
            </span>
            <Badge tone={v.tone}>{v.label}</Badge>
            {item.status && item.status !== "unknown" && (
              <Badge tone="outline" className="font-mono text-[10px]">
                {item.status}
              </Badge>
            )}
            <span className="text-sm flex-1 min-w-0 truncate" title={item.summary}>
              {item.summary}
            </span>
            {item.deep_link && (
              <Link
                to={item.deep_link}
                className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                title={`Open ${v.label} detail panel`}
              >
                <ExternalLink className="h-3 w-3" />
                detail
              </Link>
            )}
            {/* KR-FE-INVESTIGATION-DRILL-DOWN — drill into the unified
                per-caller_session_id timeline. Only renders when the
                row carries a non-empty caller_session_id (some legacy
                seams don't). The page renders the FULL audit trace
                for this one investigation. */}
            {item.caller_session_id && (
              <Link
                to={`/investigations/${encodeURIComponent(item.caller_session_id)}`}
                className="inline-flex items-center gap-1 text-primary hover:underline text-xs"
                title={`Drill into the full audit timeline for ${item.caller_session_id}`}
              >
                <Search className="h-3 w-3" />
                drill
              </Link>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ----- Page -----

export default function KoraActionsPage() {
  usePanelView("KoraActionsPage");

  const { activeTenant, isAllTenants } = useActiveTenant();
  const tenantForRead = isAllTenants ? undefined : activeTenant;

  const [data, setData] = useState<KoraActionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterValue<KoraActionCategory>>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getKoraActionsRecent({ tenantId: tenantForRead });
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

  const filteredItems = useMemo(() => {
    if (data === null) return [];
    if (filter === "all") return data.items;
    return data.items.filter((it) => it.action_category === filter);
  }, [data, filter]);

  // Drift-guard greps for this constant import.
  void KORA_ACTION_CATEGORIES;

  return (
    <div className="space-y-4 p-4 max-w-6xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <H2 className="flex items-center gap-2">
            <Activity className="h-5 w-5" />
            What Kora Did
          </H2>
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
        Chronological timeline of every mutating action Kora has taken —
        joined across all the per-seam audit streams (
        <span className="font-mono">tool.email_to_operator_sent</span>,{" "}
        <span className="font-mono">intent.email_to_sea_ticket</span>,{" "}
        <span className="font-mono">tool.probe_autofix_attempted</span>,{" "}
        <span className="font-mono">probe.investigation_completed</span>,{" "}
        kora-driven{" "}
        <span className="font-mono">phrasebook.updated</span>, and the
        promotion-loop seams{" "}
        <span className="font-mono">promotion.proposed</span> /{" "}
        <span className="font-mono">promotion.approved</span> /{" "}
        <span className="font-mono">promotion.rejected</span>). Each row
        links to its per-seam detail panel; promotion rows deep-link to
        the Promotion Review surface with the focused proposal.
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
              Failed to load Kora actions timeline:{" "}
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
                categories={KORA_ACTION_CATEGORIES_DEFS}
                counts={data.by_category_24h}
                total={data.total_recent_24h}
                totalNoun="actions"
              />
              <Sparkline
                points={data.daily_actions_14d}
                totalSuffix="actions · 14d"
                ariaLabel={`Daily action counts over the last ${data.daily_actions_14d.length} days`}
              />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-3">
              <FilterChips
                categories={KORA_ACTION_CATEGORIES_DEFS}
                counts={data.by_category_24h}
                current={filter}
                onChange={setFilter}
                allLabel="All actions"
              />
            </CardContent>
          </Card>

          {filteredItems.length === 0 ? (
            <EmptyFilteredMessage
              isAllFilter={filter === "all"}
              titleAll="Kora has been quiet today"
              titleFiltered={`No "${KORA_ACTION_CATEGORIES_MAP[filter as KoraActionCategory]?.label}" actions match this filter in the current window`}
              bodyAll="No mutating actions across any tracked seam. This page populates whenever Kora sends an email, creates a Sea_Ticket from email, attempts a probe autofix, or completes an investigation."
              onResetToAll={() => setFilter("all")}
            />
          ) : (
            <div className="space-y-2">
              {filteredItems.map((item) => (
                <ActionCard key={item.id} item={item} />
              ))}
              {filteredItems.length < data.items.length && (
                <div className="text-xs text-muted-foreground italic text-center py-2">
                  Showing {filteredItems.length} of {data.items.length}{" "}
                  items; clear filter to see all categories.
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
