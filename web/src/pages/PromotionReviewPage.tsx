// KR-FE-PROMOTION-REVIEW-PANEL — operator-approval UX for the
// Kora-generated phrasebook promotion proposals (PR #186).
//
// Reads: GET /api/promotions/phrasebook/pending
// Writes: POST /api/promotions/phrasebook/{id}/approve  (+ optional
//         pattern_override / reply_template_override /
//         category_override / review_notes)
//         POST /api/promotions/phrasebook/{id}/reject (review_notes)
//
// Layout (per CC#1's pre-spec'd shape in PR #186):
//
//   Filter: [Pending] [Approved] [Rejected] [All]
//   Summary band: pending count + last 14d daily-proposals sparkline
//   Per-row card (sorted by confidence desc):
//     confidence + cluster_size + created_at + haiku_synthesized badge
//     category / pattern / reply_template (read mode) OR editable inputs
//     sample_questions (up to 3)
//     [Edit before approving] [Approve] [Reject (with notes)]
//
// Drift-guard: PROMOTION_STATUS_VALUES (api.ts) mirrors BE
// _PROMOTION_STATUS_VALUES. Pinned by test_promotion_status_drift_guard.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Lightbulb,
  RefreshCw,
  Send,
  Sparkles,
  Wand2,
  X,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import {
  PROMOTION_STATUS_VALUES,
  type PromotionApproveOverrides,
  type PromotionProposal,
  type PromotionProposalsResponse,
  type PromotionStatus,
} from "@/lib/api";
import {
  EmptyFilteredMessage,
  FilterChips,
  formatRelative,
  formatTimestamp,
  type CategoryDef,
  type FilterValue,
} from "@/components/AuditPanelKit";

// Filter chips iterate the canonical PROMOTION_STATUS_VALUES (BE-
// echoed). Status icons mirror the lifecycle: pending=Sparkles
// (awaiting attention), approved=CheckCircle2, rejected=XCircle,
// expired=AlertTriangle.
const STATUS_CATEGORIES: readonly CategoryDef<PromotionStatus>[] = [
  { key: "pending", label: "Pending", tone: "warning", Icon: Sparkles },
  {
    key: "approved",
    label: "Approved",
    tone: "success",
    Icon: CheckCircle2,
  },
  {
    key: "rejected",
    label: "Rejected",
    tone: "destructive",
    Icon: XCircle,
  },
  {
    key: "expired",
    label: "Expired",
    tone: "outline",
    Icon: AlertTriangle,
  },
];

function formatConfidence(c: number): string {
  // BE writes confidence to 4 decimals; render to 2 — enough
  // signal for "much better than 0.85" without false precision.
  return c.toFixed(2);
}

// ---------------------------------------------------------------
// Per-row card (with inline edit-before-approve flow)
// ---------------------------------------------------------------

interface ProposalCardProps {
  proposal: PromotionProposal;
  onActionComplete: () => void;
  focused: boolean;
}

type CardMode = "view" | "edit" | "reject";

function ProposalCard({
  proposal,
  onActionComplete,
  focused,
}: ProposalCardProps) {
  const [mode, setMode] = useState<CardMode>("view");
  const [pattern, setPattern] = useState(proposal.proposed_pattern);
  const [category, setCategory] = useState(proposal.proposed_category);
  const [replyTemplate, setReplyTemplate] = useState(
    proposal.proposed_reply_template,
  );
  const [reviewNotes, setReviewNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const isPending = proposal.status === "pending";

  const approve = useCallback(
    async (overrides?: PromotionApproveOverrides) => {
      setBusy(true);
      setActionError(null);
      try {
        await api.approvePhrasebookPromotion(
          proposal.proposal_id,
          overrides,
        );
        onActionComplete();
      } catch (e) {
        setActionError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [proposal.proposal_id, onActionComplete],
  );

  const reject = useCallback(async () => {
    setBusy(true);
    setActionError(null);
    try {
      await api.rejectPhrasebookPromotion(
        proposal.proposal_id,
        reviewNotes.trim(),
      );
      onActionComplete();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [proposal.proposal_id, reviewNotes, onActionComplete]);

  const submitEdit = useCallback(() => {
    const overrides: PromotionApproveOverrides = {};
    if (pattern !== proposal.proposed_pattern) {
      overrides.pattern_override = pattern;
    }
    if (category !== proposal.proposed_category) {
      overrides.category_override = category;
    }
    if (replyTemplate !== proposal.proposed_reply_template) {
      overrides.reply_template_override = replyTemplate;
    }
    if (reviewNotes.trim()) {
      overrides.review_notes = reviewNotes.trim();
    }
    void approve(Object.keys(overrides).length ? overrides : undefined);
  }, [
    pattern,
    category,
    replyTemplate,
    reviewNotes,
    proposal.proposed_pattern,
    proposal.proposed_category,
    proposal.proposed_reply_template,
    approve,
  ]);

  return (
    <Card
      className={
        focused
          ? "border-primary/60 ring-1 ring-primary/30"
          : isPending
            ? "border-yellow-500/40"
            : ""
      }
      id={`proposal-${proposal.proposal_id}`}
    >
      <CardContent className="p-4 space-y-3">
        <div className="flex items-baseline gap-2 flex-wrap">
          <Badge tone="warning" className="font-mono">
            {formatConfidence(proposal.confidence)} confidence
          </Badge>
          <span className="text-xs text-muted-foreground">
            cluster of {proposal.cluster_size}
          </span>
          <span className="text-xs text-muted-foreground">·</span>
          <span
            className="text-xs text-muted-foreground"
            title={formatTimestamp(proposal.created_at)}
          >
            created {formatRelative(proposal.created_at)}
          </span>
          {proposal.haiku_synthesized && (
            <Badge tone="secondary" className="ml-1">
              <Wand2 className="h-3 w-3 mr-1 inline" />
              Kora wrote this
            </Badge>
          )}
          <span className="ml-auto" />
          <Badge tone={isPending ? "warning" : "outline"}>
            {proposal.status}
          </Badge>
        </div>

        {mode === "view" && (
          <div className="space-y-2 text-sm">
            <div>
              <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
                category
              </span>
              <div className="font-mono">{proposal.proposed_category}</div>
            </div>
            <div>
              <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
                pattern
              </span>
              <div className="font-mono text-xs whitespace-pre-wrap">
                {proposal.proposed_pattern}
              </div>
            </div>
            <div>
              <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
                reply template
              </span>
              <div className="font-mono text-xs whitespace-pre-wrap rounded bg-muted/30 border border-border p-2">
                {proposal.proposed_reply_template}
              </div>
            </div>
          </div>
        )}

        {mode === "edit" && (
          <div className="space-y-2 text-sm">
            <FieldEditor
              label="Category"
              value={category}
              onChange={setCategory}
            />
            <FieldEditor
              label="Pattern (Python regex, case-insensitive)"
              value={pattern}
              onChange={setPattern}
              mono
            />
            <FieldEditor
              label="Reply template (supports {snapshot.X.Y} placeholders)"
              value={replyTemplate}
              onChange={setReplyTemplate}
              mono
              textarea
            />
            <FieldEditor
              label="Review notes (optional — recorded on the audit row)"
              value={reviewNotes}
              onChange={setReviewNotes}
            />
          </div>
        )}

        {mode === "reject" && (
          <div className="space-y-2">
            <FieldEditor
              label="Why are you rejecting? (recorded verbatim on the audit row)"
              value={reviewNotes}
              onChange={setReviewNotes}
              textarea
            />
          </div>
        )}

        {proposal.sample_questions.length > 0 && (
          <div className="text-xs text-muted-foreground">
            <span className="text-[10px] uppercase tracking-wide">
              sample questions
            </span>
            <ul className="mt-1 ml-3 list-disc space-y-0.5">
              {proposal.sample_questions.map((q, i) => (
                <li key={i} className="italic">
                  &ldquo;{q}&rdquo;
                </li>
              ))}
            </ul>
          </div>
        )}

        {actionError && (
          <div className="text-xs text-destructive flex items-center gap-1.5">
            <AlertCircle className="h-3 w-3" />
            <span className="font-mono">{actionError}</span>
          </div>
        )}

        {isPending && (
          <div className="flex items-center gap-2 flex-wrap pt-1 border-t border-border/40">
            {mode === "view" && (
              <>
                <Button
                  size="sm"
                  onClick={() => void approve()}
                  disabled={busy}
                >
                  {busy ? (
                    <Spinner className="h-3 w-3" />
                  ) : (
                    <CheckCircle2 className="h-3 w-3 mr-1" />
                  )}
                  Approve
                </Button>
                <Button
                  size="sm"
                  outlined
                  onClick={() => setMode("edit")}
                  disabled={busy}
                >
                  <Wand2 className="h-3 w-3 mr-1" />
                  Edit before approving
                </Button>
                <Button
                  size="sm"
                  ghost
                  destructive
                  onClick={() => setMode("reject")}
                  disabled={busy}
                >
                  <XCircle className="h-3 w-3 mr-1" />
                  Reject (with notes)
                </Button>
              </>
            )}
            {mode === "edit" && (
              <>
                <Button
                  size="sm"
                  onClick={submitEdit}
                  disabled={busy}
                >
                  {busy ? (
                    <Spinner className="h-3 w-3" />
                  ) : (
                    <Send className="h-3 w-3 mr-1" />
                  )}
                  Submit + approve
                </Button>
                <Button
                  size="sm"
                  ghost
                  onClick={() => setMode("view")}
                  disabled={busy}
                >
                  <X className="h-3 w-3 mr-1" />
                  Cancel
                </Button>
              </>
            )}
            {mode === "reject" && (
              <>
                <Button
                  size="sm"
                  destructive
                  onClick={() => void reject()}
                  disabled={busy}
                >
                  {busy ? (
                    <Spinner className="h-3 w-3" />
                  ) : (
                    <XCircle className="h-3 w-3 mr-1" />
                  )}
                  Confirm reject
                </Button>
                <Button
                  size="sm"
                  ghost
                  onClick={() => setMode("view")}
                  disabled={busy}
                >
                  <X className="h-3 w-3 mr-1" />
                  Cancel
                </Button>
              </>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

interface FieldEditorProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  mono?: boolean;
  textarea?: boolean;
}

function FieldEditor({
  label,
  value,
  onChange,
  mono,
  textarea,
}: FieldEditorProps) {
  const baseInputClass = `w-full px-2 py-1 text-xs rounded border bg-card border-border focus:outline-primary ${
    mono ? "font-mono" : ""
  }`;
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      {textarea ? (
        <textarea
          className={`${baseInputClass} min-h-[64px] resize-y`}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          className={baseInputClass}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </label>
  );
}

// ---------------------------------------------------------------
// Page
// ---------------------------------------------------------------

export default function PromotionReviewPage() {
  usePanelView("PromotionReviewPage");

  const [data, setData] = useState<PromotionProposalsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterValue<PromotionStatus>>(
    "pending",
  );

  const location = useLocation();
  const focusedId = useMemo(() => {
    const qs = new URLSearchParams(location.search);
    return qs.get("focus");
  }, [location.search]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.getPhrasebookPromotionProposals();
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Scroll the deep-linked proposal into view once data loads.
  useEffect(() => {
    if (!focusedId || !data) return;
    const el = document.getElementById(`proposal-${focusedId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [focusedId, data]);

  const sorted = useMemo(() => {
    if (data === null) return [];
    return [...data.proposals].sort((a, b) => b.confidence - a.confidence);
  }, [data]);

  // BE returns all-pending today (the pending endpoint). Filter chips
  // are forward-compat — once /api/promotions/phrasebook?status=
  // lands we'll fan out across the lifecycle. In v1 the non-pending
  // chips show 0 + an explanatory empty state.
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const s of PROMOTION_STATUS_VALUES) c[s] = 0;
    for (const p of sorted) {
      c[p.status] = (c[p.status] ?? 0) + 1;
    }
    return c;
  }, [sorted]);

  const filteredProposals = useMemo(() => {
    if (filter === "all") return sorted;
    return sorted.filter((p) => p.status === filter);
  }, [sorted, filter]);

  // Drift-guard grep — pins the import so test_promotion_status_drift_guard
  // catches a rename on either side.
  void PROMOTION_STATUS_VALUES;

  return (
    <div className="space-y-4 p-4 max-w-5xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2 className="flex items-center gap-2">
          <Lightbulb className="h-5 w-5" />
          Promotion Review · Phrasebook
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

      <p className="text-sm text-muted-foreground">
        Kora&apos;s proposed phrasebook entries — clusters of operator
        questions that look answerable from the current snapshot. Approve
        moves the entry into the operator-override phrasebook (the
        $0-cost reply path); reject records the rationale for the audit
        trail. Edit-before-approve lets you tighten the regex or reply
        before committing.
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
              Failed to load promotion proposals:{" "}
              <span className="font-mono">{error}</span>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <Card>
            <CardContent className="p-4 flex flex-col gap-3">
              <div className="flex items-center gap-3 text-sm">
                <Sparkles className="h-4 w-4 text-yellow-500" />
                <span className="font-medium">
                  {counts.pending ?? 0} pending
                </span>
                <span className="text-muted-foreground">·</span>
                <span className="text-muted-foreground">
                  {counts.approved ?? 0} approved this cycle
                </span>
                <span className="text-muted-foreground">·</span>
                <span className="text-muted-foreground">
                  {counts.rejected ?? 0} rejected
                </span>
                {counts.expired ? (
                  <>
                    <span className="text-muted-foreground">·</span>
                    <span className="text-muted-foreground">
                      {counts.expired} expired
                    </span>
                  </>
                ) : null}
              </div>
              <FilterChips
                categories={STATUS_CATEGORIES}
                counts={counts}
                current={filter}
                onChange={setFilter}
                allLabel="All"
              />
            </CardContent>
          </Card>

          {filteredProposals.length === 0 ? (
            <EmptyFilteredMessage
              isAllFilter={filter === "all"}
              titleAll="No promotion proposals yet"
              titleFiltered={`No ${filter} proposals.`}
              bodyAll="Kora hasn't synthesized any clusters worth promoting in the current cycle. New proposals appear here as the promotion loop discovers patterns in the operator-DM history."
              onResetToAll={() => setFilter("all")}
            />
          ) : (
            <div className="space-y-3">
              {filteredProposals.map((p) => (
                <ProposalCard
                  key={p.proposal_id}
                  proposal={p}
                  onActionComplete={() => void load()}
                  focused={focusedId === p.proposal_id}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
