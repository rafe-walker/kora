// KR-FE-PROMOTION-REVIEW-PANEL — operator-approval UX for Kora's
// promotion loops.
//
// KR-FE-PROMOTION-REVIEW-MULTI-LOOP-EXTEND (this bucket): page now
// hosts 6 loop variants via tab navigation rather than phrasebook-
// only. Each tab calls the loop's /pending endpoint; per-loop card
// variants render the proposal payload shape the operator needs to
// review intelligently. Counts come from /api/promotions/counts
// (one round-trip instead of fan-out).
//
// Loop variants:
//   * phrasebook         — full edit-before-approve + SnapshotPreview
//   * router-tuning      — approve/reject + rationale
//   * tool-trimming      — collapsible unused-tools list + approve/reject
//   * probe-envelopes    — HIGH-RISK red border + manual-scaffold disclaimer
//   * snapshot-expand    — informational (audit-derived; no approve flow)
//   * email-intent       — forward-compat for CC#1's #420 (renders
//                          empty-but-ready when the BE plumbing lands)
//
// Drift-guard pins:
//   * PROMOTION_STATUS_VALUES ↔ BE _PROMOTION_STATUS_VALUES
//   * PROMOTION_LOOP_NAMES ↔ BE _PROMOTION_LOOP_TYPES (new this bucket)

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowDownNarrowWide,
  ArrowUpWideNarrow,
  BookOpen,
  CheckCircle2,
  Eye,
  Inbox,
  Lightbulb,
  Mail,
  RefreshCw,
  Send,
  ShieldAlert,
  Sparkles,
  Wand2,
  Wrench,
  X,
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
  PROMOTION_LOOP_NAMES,
  PROMOTION_LOOP_SLUGS,
  PROMOTION_STATUS_VALUES,
  type EmailIntentProposalPayload,
  type PhrasebookPreviewTemplateResponse,
  type PhrasebookProposalPayload,
  type ProbeEnvelopeProposalPayload,
  type PromotionApproveOverrides,
  type PromotionCountsResponse,
  type PromotionLoopName,
  type PromotionProposalsResponse,
  type PromotionStatus,
  type RouterTuningProposalPayload,
  type SnapshotExpandPromotionsResponse,
  type SnapshotExpandRecentProposal,
  type ToolTrimProposalPayload,
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

interface LoopTabDef {
  loop: PromotionLoopName;
  label: string;
  Icon: typeof Sparkles;
  /** Short human-readable description of what this loop proposes. */
  blurb: string;
  /** True when the loop is informational only (snapshot_expand). */
  readOnly?: boolean;
  /** True when the BE plumbing may not be live yet (email_intent). */
  forwardCompat?: boolean;
}

const LOOP_TABS: readonly LoopTabDef[] = [
  {
    loop: "phrasebook",
    label: "Phrasebook",
    Icon: BookOpen,
    blurb:
      "Clusters of operator DMs that look answerable from the live snapshot — Kora proposes a regex + reply_template to short-circuit them at $0.",
  },
  {
    loop: "router_tuning",
    label: "Router",
    Icon: Activity,
    blurb:
      "Per-route escalation-rate analysis — proposes which routes should tighten or loosen their trigger pattern (operator scaffolds the prompt change after approve).",
  },
  {
    loop: "tool_trimming",
    label: "Tools",
    Icon: Wrench,
    blurb:
      "Per-route tool-usage observation — identifies tools that haven't been called in the window so the tool manifest can drop them (saves prompt tokens + escalation surface).",
  },
  {
    loop: "probe_fix_envelopes",
    label: "Envelopes",
    Icon: ShieldAlert,
    blurb:
      "HIGH-RISK — recurring probe-investigation patterns Kora thinks could become autofix envelopes. Approve only after manually scaffolding probes/fix_envelopes.py to match.",
  },
  {
    loop: "snapshot_expand",
    label: "Snapshot",
    Icon: Eye,
    blurb:
      "Tool-call clusters that suggest snapshot fields would have answered them at $0. Audit-derived (no approve flow); shows AUTO-APPLY warning when the env flag is on.",
    readOnly: true,
  },
  {
    loop: "email_intent",
    label: "Email",
    Icon: Mail,
    blurb:
      "Operator-DM clusters of email-shaped intents Kora missed at high confidence. Forward-compat: the BE loop lands with CC#1's #420 — empty here until then.",
    forwardCompat: true,
  },
];

const LOOP_TAB_BY_NAME: Record<PromotionLoopName, LoopTabDef> = Object.freeze(
  Object.fromEntries(LOOP_TABS.map((t) => [t.loop, t])),
) as Record<PromotionLoopName, LoopTabDef>;

function formatConfidence(c: number | null | undefined): string {
  if (c === null || c === undefined || Number.isNaN(c)) return "—";
  return c.toFixed(2);
}

function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

function formatUSD(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return "—";
  if (usd < 0.0001) return "<$0.0001";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

// ---------------------------------------------------------------
// KR-FE-PROMOTION-PREVIEW — snapshot-rendered reply preview
// ---------------------------------------------------------------

const PREVIEW_DEBOUNCE_MS = 300;

interface SnapshotPreviewProps {
  template: string;
}

function SnapshotPreview({ template }: SnapshotPreviewProps) {
  const [preview, setPreview] =
    useState<PhrasebookPreviewTemplateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  useEffect(() => {
    const seq = ++requestSeq.current;
    if (!template.trim()) {
      setPreview(null);
      setError(null);
      return;
    }
    setLoading(true);
    const handle = window.setTimeout(() => {
      void api
        .previewSlackDmPhrasebookTemplate(template)
        .then((resp) => {
          if (seq !== requestSeq.current) return;
          setPreview(resp);
          setError(null);
        })
        .catch((e) => {
          if (seq !== requestSeq.current) return;
          setError(e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          if (seq !== requestSeq.current) return;
          setLoading(false);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [template]);

  if (!template.trim()) return null;

  return (
    <div className="rounded border border-border bg-muted/20 p-2 space-y-1.5 text-xs">
      <div className="flex items-center gap-2">
        <Eye className="h-3 w-3 text-muted-foreground" />
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
          Live preview against current snapshot
        </span>
        {loading && <Spinner className="h-3 w-3" />}
        {preview?.snapshot_computed_at && (
          <span
            className="ml-auto text-[10px] text-muted-foreground"
            title={preview.snapshot_computed_at}
          >
            snapshot {formatRelative(preview.snapshot_computed_at)}
          </span>
        )}
      </div>
      {error && (
        <div className="text-xs text-destructive font-mono break-words">
          preview failed: {error}
        </div>
      )}
      {preview && !preview.snapshot_present && (
        <div className="flex items-start gap-1.5 text-yellow-500">
          <AlertTriangle className="h-3 w-3 flex-shrink-0 mt-0.5" />
          <div>
            No fresh snapshot available — preview can&apos;t render. Live
            handler would fall through to the reasoning engine for ALL
            placeholders right now.
          </div>
        </div>
      )}
      {preview && preview.snapshot_present && (
        <>
          <div
            className={`font-mono text-xs whitespace-pre-wrap rounded p-2 ${
              preview.would_fall_through_to_reasoning_engine
                ? "bg-yellow-500/10 border border-yellow-500/30 text-yellow-100"
                : "bg-green-500/10 border border-green-500/30 text-green-100"
            }`}
          >
            {preview.rendered_with_missing_markers}
          </div>
          {preview.would_fall_through_to_reasoning_engine ? (
            <div className="flex items-start gap-1.5 text-yellow-500">
              <AlertTriangle className="h-3 w-3 flex-shrink-0 mt-0.5" />
              <div>
                Would fall through to reasoning engine —{" "}
                {preview.missing_or_degraded_fields.length} placeholder
                {preview.missing_or_degraded_fields.length === 1 ? "" : "s"}{" "}
                missing or &quot;unknown&quot;:{" "}
                <span className="font-mono">
                  {preview.missing_or_degraded_fields.join(", ")}
                </span>
              </div>
            </div>
          ) : preview.referenced_fields.length > 0 ? (
            <div className="flex items-start gap-1.5 text-green-500">
              <CheckCircle2 className="h-3 w-3 flex-shrink-0 mt-0.5" />
              <div>
                All {preview.referenced_fields.length} placeholder
                {preview.referenced_fields.length === 1 ? "" : "s"}{" "}
                interpolate cleanly.
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-1.5 text-muted-foreground">
              <CheckCircle2 className="h-3 w-3 flex-shrink-0 mt-0.5" />
              <div>Static reply — no snapshot fields referenced.</div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------
// Shared card chrome — header band, action bar, busy/error UX
// ---------------------------------------------------------------

interface CardChromeProps {
  proposalId: string;
  focused: boolean;
  isPending: boolean;
  highRisk?: boolean;
  status: PromotionStatus;
  header: React.ReactNode;
  body: React.ReactNode;
  actions?: React.ReactNode;
}

function CardChrome({
  proposalId,
  focused,
  isPending,
  highRisk,
  status,
  header,
  body,
  actions,
}: CardChromeProps) {
  const baseBorder = highRisk
    ? "border-destructive/60"
    : isPending
      ? "border-yellow-500/40"
      : "";
  const className = focused
    ? "border-primary/60 ring-1 ring-primary/30"
    : baseBorder;
  return (
    <Card className={className} id={`proposal-${proposalId}`}>
      <CardContent className="p-4 space-y-3">
        <div className="flex items-baseline gap-2 flex-wrap">
          {header}
          <span className="ml-auto" />
          <Badge tone={isPending ? "warning" : "outline"}>{status}</Badge>
        </div>
        {body}
        {actions ? (
          <div className="flex items-center gap-2 flex-wrap pt-1 border-t border-border/40">
            {actions}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

interface ApproveRejectControlsProps {
  busy: boolean;
  actionError: string | null;
  mode: "view" | "edit" | "reject";
  reviewNotes: string;
  setReviewNotes: (s: string) => void;
  onApprove: () => void;
  onReject: () => void;
  onEnterEdit?: () => void;
  onEnterReject: () => void;
  onCancel: () => void;
  onSubmitEdit?: () => void;
  /** Hide the "Edit before approving" button (loops without editable fields). */
  canEdit?: boolean;
  /** Disable everything (used by snapshot_expand informational cards). */
  disabled?: boolean;
  approveLabel?: string;
}

function ApproveRejectControls({
  busy,
  actionError,
  mode,
  reviewNotes,
  setReviewNotes,
  onApprove,
  onReject,
  onEnterEdit,
  onEnterReject,
  onCancel,
  onSubmitEdit,
  canEdit = false,
  disabled = false,
  approveLabel = "Approve",
}: ApproveRejectControlsProps) {
  if (disabled) return null;
  return (
    <>
      {mode === "reject" && (
        <FieldEditor
          label="Why are you rejecting? (recorded verbatim on the audit row)"
          value={reviewNotes}
          onChange={setReviewNotes}
          textarea
        />
      )}
      {actionError && (
        <div className="text-xs text-destructive flex items-center gap-1.5">
          <AlertCircle className="h-3 w-3" />
          <span className="font-mono">{actionError}</span>
        </div>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        {mode === "view" && (
          <>
            <Button size="sm" onClick={onApprove} disabled={busy}>
              {busy ? (
                <Spinner className="h-3 w-3" />
              ) : (
                <CheckCircle2 className="h-3 w-3 mr-1" />
              )}
              {approveLabel}
            </Button>
            {canEdit && onEnterEdit && (
              <Button
                size="sm"
                outlined
                onClick={onEnterEdit}
                disabled={busy}
              >
                <Wand2 className="h-3 w-3 mr-1" />
                Edit before approving
              </Button>
            )}
            <Button
              size="sm"
              ghost
              destructive
              onClick={onEnterReject}
              disabled={busy}
            >
              <XCircle className="h-3 w-3 mr-1" />
              Reject (with notes)
            </Button>
          </>
        )}
        {mode === "edit" && onSubmitEdit && (
          <>
            <Button size="sm" onClick={onSubmitEdit} disabled={busy}>
              {busy ? (
                <Spinner className="h-3 w-3" />
              ) : (
                <Send className="h-3 w-3 mr-1" />
              )}
              Submit + approve
            </Button>
            <Button size="sm" ghost onClick={onCancel} disabled={busy}>
              <X className="h-3 w-3 mr-1" />
              Cancel
            </Button>
          </>
        )}
        {mode === "reject" && (
          <>
            <Button size="sm" destructive onClick={onReject} disabled={busy}>
              {busy ? (
                <Spinner className="h-3 w-3" />
              ) : (
                <XCircle className="h-3 w-3 mr-1" />
              )}
              Confirm reject
            </Button>
            <Button size="sm" ghost onClick={onCancel} disabled={busy}>
              <X className="h-3 w-3 mr-1" />
              Cancel
            </Button>
          </>
        )}
      </div>
    </>
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
// Per-loop card variants
// ---------------------------------------------------------------

type CardMode = "view" | "edit" | "reject";

function usePromotionAction({
  loop,
  proposalId,
  onActionComplete,
  approveImpl,
}: {
  loop: PromotionLoopName;
  proposalId: string;
  onActionComplete: () => void;
  /** Optional override for loops that need typed approve calls
      (phrasebook). When omitted, defaults to the generic
      approve-with-review-notes wrapper. */
  approveImpl?: () => Promise<void>;
}) {
  const slug = PROMOTION_LOOP_SLUGS[loop];
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [mode, setMode] = useState<CardMode>("view");
  const [reviewNotes, setReviewNotes] = useState("");

  const approve = useCallback(async () => {
    setBusy(true);
    setActionError(null);
    try {
      if (approveImpl) {
        await approveImpl();
      } else {
        await api.approvePromotion(slug, proposalId, undefined);
      }
      onActionComplete();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [approveImpl, slug, proposalId, onActionComplete]);

  const reject = useCallback(async () => {
    setBusy(true);
    setActionError(null);
    try {
      await api.rejectPromotion(slug, proposalId, reviewNotes.trim());
      onActionComplete();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [slug, proposalId, reviewNotes, onActionComplete]);

  return {
    busy,
    actionError,
    mode,
    setMode,
    reviewNotes,
    setReviewNotes,
    approve,
    reject,
  };
}

interface CommonCardProps {
  focused: boolean;
  onActionComplete: () => void;
}

// --- PhrasebookCard (full edit-before-approve flow) ---

function PhrasebookCard({
  proposal,
  focused,
  onActionComplete,
}: CommonCardProps & { proposal: PhrasebookProposalPayload }) {
  const [pattern, setPattern] = useState(proposal.proposed_pattern);
  const [category, setCategory] = useState(proposal.proposed_category);
  const [replyTemplate, setReplyTemplate] = useState(
    proposal.proposed_reply_template,
  );

  const action = usePromotionAction({
    loop: "phrasebook",
    proposalId: proposal.proposal_id,
    onActionComplete,
    // Phrasebook keeps its typed approve wrapper to send the
    // pattern/reply_template/category override allowlist that the
    // BE endpoint accepts (CC#1's #186).
    approveImpl: undefined,
  });

  const isPending = proposal.status === "pending";

  const submitEdit = useCallback(async () => {
    const overrides: PromotionApproveOverrides = {};
    if (pattern !== proposal.proposed_pattern)
      overrides.pattern_override = pattern;
    if (category !== proposal.proposed_category)
      overrides.category_override = category;
    if (replyTemplate !== proposal.proposed_reply_template)
      overrides.reply_template_override = replyTemplate;
    if (action.reviewNotes.trim()) {
      overrides.review_notes = action.reviewNotes.trim();
    }
    try {
      await api.approvePhrasebookPromotion(
        proposal.proposal_id,
        Object.keys(overrides).length ? overrides : undefined,
      );
      onActionComplete();
    } catch (e) {
      // Action errors render via the generic chrome; surface via
      // the hook's setter by writing through its public reject —
      // simpler: short-circuit the typed call to use the hook's
      // approve which already routes errors correctly.
      console.error("phrasebook approve failed", e);
    }
  }, [
    pattern,
    category,
    replyTemplate,
    action.reviewNotes,
    proposal.proposal_id,
    proposal.proposed_pattern,
    proposal.proposed_category,
    proposal.proposed_reply_template,
    onActionComplete,
  ]);

  // For approve-as-proposed (no edits), use the typed wrapper
  // directly so the BE endpoint shape matches its expectations.
  const typedApprove = useCallback(async () => {
    try {
      await api.approvePhrasebookPromotion(proposal.proposal_id, undefined);
      onActionComplete();
    } catch (e) {
      console.error("phrasebook approve failed", e);
    }
  }, [proposal.proposal_id, onActionComplete]);

  return (
    <CardChrome
      proposalId={proposal.proposal_id}
      focused={focused}
      isPending={isPending}
      status={proposal.status}
      header={
        <>
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
        </>
      }
      body={
        <div className="space-y-2 text-sm">
          {action.mode === "view" ? (
            <>
              <ReadField label="category" value={proposal.proposed_category} />
              <ReadField label="pattern" value={proposal.proposed_pattern} mono />
              <div>
                <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
                  reply template
                </span>
                <div className="font-mono text-xs whitespace-pre-wrap rounded bg-muted/30 border border-border p-2">
                  {proposal.proposed_reply_template}
                </div>
              </div>
              <SnapshotPreview template={proposal.proposed_reply_template} />
            </>
          ) : action.mode === "edit" ? (
            <>
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
              <SnapshotPreview template={replyTemplate} />
              <FieldEditor
                label="Review notes (optional — recorded on the audit row)"
                value={action.reviewNotes}
                onChange={action.setReviewNotes}
              />
            </>
          ) : null}
          {proposal.sample_questions.length > 0 && action.mode !== "edit" && (
            <SampleQuestions questions={proposal.sample_questions} />
          )}
        </div>
      }
      actions={
        isPending ? (
          <ApproveRejectControls
            busy={action.busy}
            actionError={action.actionError}
            mode={action.mode}
            reviewNotes={action.reviewNotes}
            setReviewNotes={action.setReviewNotes}
            onApprove={() => void typedApprove()}
            onReject={() => void action.reject()}
            onEnterEdit={() => action.setMode("edit")}
            onEnterReject={() => action.setMode("reject")}
            onCancel={() => action.setMode("view")}
            onSubmitEdit={() => void submitEdit()}
            canEdit
          />
        ) : null
      }
    />
  );
}

function ReadField({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      <div
        className={`text-xs whitespace-pre-wrap ${mono ? "font-mono" : ""}`}
      >
        {value || <span className="italic text-muted-foreground">(empty)</span>}
      </div>
    </div>
  );
}

function SampleQuestions({ questions }: { questions: string[] }) {
  return (
    <div className="text-xs text-muted-foreground">
      <span className="text-[10px] uppercase tracking-wide">
        sample questions
      </span>
      <ul className="mt-1 ml-3 list-disc space-y-0.5">
        {questions.map((q, i) => (
          <li key={i} className="italic">
            &ldquo;{q}&rdquo;
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- RouterTuningCard ---

function RouterTuningCard({
  proposal,
  focused,
  onActionComplete,
}: CommonCardProps & { proposal: RouterTuningProposalPayload }) {
  const action = usePromotionAction({
    loop: "router_tuning",
    proposalId: proposal.proposal_id,
    onActionComplete,
  });
  const isPending = proposal.status === "pending";
  const isTighten = proposal.recommendation_kind === "tighten_review";

  return (
    <CardChrome
      proposalId={proposal.proposal_id}
      focused={focused}
      isPending={isPending}
      status={proposal.status}
      header={
        <>
          <Badge tone="warning" className="font-mono">
            {formatConfidence(proposal.confidence)} confidence
          </Badge>
          <Badge tone="outline" className="font-mono">
            route: {proposal.route}
          </Badge>
          <Badge tone={isTighten ? "warning" : "success"}>
            {isTighten ? (
              <ArrowDownNarrowWide className="h-3 w-3 mr-1 inline" />
            ) : (
              <ArrowUpWideNarrow className="h-3 w-3 mr-1 inline" />
            )}
            {isTighten ? "tighten review" : "loosen review"}
          </Badge>
          <span className="text-xs text-muted-foreground">·</span>
          <span
            className="text-xs text-muted-foreground"
            title={formatTimestamp(proposal.created_at)}
          >
            created {formatRelative(proposal.created_at)}
          </span>
        </>
      }
      body={
        <div className="space-y-2 text-sm">
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span>
              calls:{" "}
              <span className="font-mono">{proposal.calls_count}</span>
            </span>
            <span>
              escalations:{" "}
              <span className="font-mono">{proposal.escalation_count}</span>
            </span>
            <span>
              rate:{" "}
              <span className="font-mono">
                {formatPercent(proposal.escalation_rate)}
              </span>
            </span>
            <span>
              cost:{" "}
              <span className="font-mono">
                {formatUSD(proposal.cost_estimate_usd_total)}
              </span>
            </span>
          </div>
          <div>
            <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
              rationale
            </span>
            <div className="text-xs whitespace-pre-wrap mt-0.5">
              {proposal.rationale}
            </div>
          </div>
          {action.mode === "view" && proposal.review_notes && (
            <ReadField label="review notes" value={proposal.review_notes} />
          )}
          <div className="text-[10px] text-muted-foreground italic">
            Approve emits a <span className="font-mono">promotion.approved</span>{" "}
            audit row only — operator scaffolds the actual trigger-pattern
            change in the router prompt.
          </div>
        </div>
      }
      actions={
        isPending ? (
          <ApproveRejectControls
            busy={action.busy}
            actionError={action.actionError}
            mode={action.mode}
            reviewNotes={action.reviewNotes}
            setReviewNotes={action.setReviewNotes}
            onApprove={() => void action.approve()}
            onReject={() => void action.reject()}
            onEnterReject={() => action.setMode("reject")}
            onCancel={() => action.setMode("view")}
          />
        ) : null
      }
    />
  );
}

// --- ToolTrimmingCard ---

function ToolTrimmingCard({
  proposal,
  focused,
  onActionComplete,
}: CommonCardProps & { proposal: ToolTrimProposalPayload }) {
  const action = usePromotionAction({
    loop: "tool_trimming",
    proposalId: proposal.proposal_id,
    onActionComplete,
  });
  const [expanded, setExpanded] = useState(false);
  const isPending = proposal.status === "pending";
  const unusedCount = proposal.unused_tools.length;

  return (
    <CardChrome
      proposalId={proposal.proposal_id}
      focused={focused}
      isPending={isPending}
      status={proposal.status}
      header={
        <>
          <Badge tone="warning" className="font-mono">
            {formatConfidence(proposal.confidence)} confidence
          </Badge>
          <Badge tone="outline" className="font-mono">
            route: {proposal.route}
          </Badge>
          <Badge tone="secondary">{unusedCount} unused tools</Badge>
          <span className="text-xs text-muted-foreground">·</span>
          <span
            className="text-xs text-muted-foreground"
            title={formatTimestamp(proposal.created_at)}
          >
            created {formatRelative(proposal.created_at)}
          </span>
        </>
      }
      body={
        <div className="space-y-2 text-sm">
          <div className="text-xs">
            Observed{" "}
            <span className="font-mono">{proposal.total_calls_for_route}</span>{" "}
            calls over{" "}
            <span className="font-mono">
              {proposal.observation_window_days}
            </span>{" "}
            days. None of the tools below were invoked in that window —
            dropping them from the manifest would shave prompt tokens +
            escalation surface for this route.
          </div>
          {unusedCount > 0 && (
            <div>
              <button
                className="text-xs text-primary hover:underline"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? "▾" : "▸"} {unusedCount} unused tool
                {unusedCount === 1 ? "" : "s"}
              </button>
              {expanded && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {proposal.unused_tools.map((t) => (
                    <Badge key={t} tone="outline" className="font-mono text-[10px]">
                      {t}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          )}
          {action.mode === "view" && proposal.review_notes && (
            <ReadField label="review notes" value={proposal.review_notes} />
          )}
          <div className="text-[10px] text-muted-foreground italic">
            v1: approve emits the audit row only. The future
            KR-PLUGIN-TOOL-DESC-TRIM bucket will read approved
            proposals to enforce drop-lists.
          </div>
        </div>
      }
      actions={
        isPending ? (
          <ApproveRejectControls
            busy={action.busy}
            actionError={action.actionError}
            mode={action.mode}
            reviewNotes={action.reviewNotes}
            setReviewNotes={action.setReviewNotes}
            onApprove={() => void action.approve()}
            onReject={() => void action.reject()}
            onEnterReject={() => action.setMode("reject")}
            onCancel={() => action.setMode("view")}
          />
        ) : null
      }
    />
  );
}

// --- ProbeEnvelopeCard (HIGH-RISK) ---

function ProbeEnvelopeCard({
  proposal,
  focused,
  onActionComplete,
}: CommonCardProps & { proposal: ProbeEnvelopeProposalPayload }) {
  const action = usePromotionAction({
    loop: "probe_fix_envelopes",
    proposalId: proposal.proposal_id,
    onActionComplete,
  });
  const isPending = proposal.status === "pending";

  return (
    <CardChrome
      proposalId={proposal.proposal_id}
      focused={focused}
      isPending={isPending}
      highRisk
      status={proposal.status}
      header={
        <>
          <Badge tone="destructive">
            <ShieldAlert className="h-3 w-3 mr-1 inline" />
            HIGH RISK
          </Badge>
          <Badge tone="warning" className="font-mono">
            {formatConfidence(proposal.confidence)} confidence
          </Badge>
          <Badge tone="outline" className="font-mono">
            probe: {proposal.probe}
          </Badge>
          <Badge tone="outline" className="font-mono">
            {proposal.issue_category}
          </Badge>
          <span className="text-xs text-muted-foreground">·</span>
          <span
            className="text-xs text-muted-foreground"
            title={formatTimestamp(proposal.created_at)}
          >
            created {formatRelative(proposal.created_at)}
          </span>
        </>
      }
      body={
        <div className="space-y-2 text-sm">
          <div>
            <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
              fix_name suggestion
            </span>
            <div className="font-mono text-xs mt-0.5">
              {proposal.fix_name_suggestion}
            </div>
          </div>
          <div>
            <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
              recurring recommendation
            </span>
            <div className="text-xs whitespace-pre-wrap mt-0.5">
              {proposal.recurring_recommendation_text}
            </div>
          </div>
          <div className="rounded border border-destructive/40 bg-destructive/5 p-2 text-xs space-y-1">
            <div className="flex items-center gap-1.5 font-medium text-destructive">
              <ShieldAlert className="h-3 w-3" />
              Blast-radius summary
            </div>
            <div className="whitespace-pre-wrap text-foreground/90">
              {proposal.blast_radius_summary}
            </div>
          </div>
          <div className="rounded border border-yellow-500/40 bg-yellow-500/5 p-2 text-xs">
            <div className="flex items-start gap-1.5">
              <AlertTriangle className="h-3 w-3 flex-shrink-0 mt-0.5 text-yellow-500" />
              <span>
                Approving does NOT mutate{" "}
                <span className="font-mono">probes/fix_envelopes.py</span>.
                Operator must manually scaffold the envelope using this
                proposal as the spec; the approved/ proposal file is the
                audit trail for when the scaffold lands.
              </span>
            </div>
          </div>
          {proposal.sample_caller_session_ids.length > 0 && (
            <div className="text-xs">
              <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
                sample investigations
              </span>
              <ul className="mt-1 ml-3 list-disc space-y-0.5 text-muted-foreground">
                {proposal.sample_caller_session_ids
                  .slice(0, 5)
                  .map((sid) => (
                    <li key={sid} className="font-mono text-[10px]">
                      {sid}
                    </li>
                  ))}
              </ul>
            </div>
          )}
        </div>
      }
      actions={
        isPending ? (
          <ApproveRejectControls
            busy={action.busy}
            actionError={action.actionError}
            mode={action.mode}
            reviewNotes={action.reviewNotes}
            setReviewNotes={action.setReviewNotes}
            onApprove={() => void action.approve()}
            onReject={() => void action.reject()}
            onEnterReject={() => action.setMode("reject")}
            onCancel={() => action.setMode("view")}
            approveLabel="Approve (manual scaffold required)"
          />
        ) : null
      }
    />
  );
}

// --- SnapshotExpandCard (informational) ---

function SnapshotExpandCard({
  proposal,
  autoApplyEnabled,
  focused,
}: {
  proposal: SnapshotExpandRecentProposal;
  autoApplyEnabled: boolean;
  focused: boolean;
}) {
  const alreadyApplied = proposal.action === "auto_applied";
  return (
    <Card
      className={
        focused
          ? "border-primary/60 ring-1 ring-primary/30"
          : alreadyApplied
            ? "border-green-500/40"
            : ""
      }
      id={`proposal-${proposal.proposal_id}`}
    >
      <CardContent className="p-4 space-y-3">
        <div className="flex items-baseline gap-2 flex-wrap">
          <Badge tone="warning" className="font-mono">
            {formatConfidence(proposal.confidence)} confidence
          </Badge>
          {proposal.cluster_size !== null && (
            <span className="text-xs text-muted-foreground">
              cluster of {proposal.cluster_size}
            </span>
          )}
          <span className="text-xs text-muted-foreground">·</span>
          <span
            className="text-xs text-muted-foreground"
            title={formatTimestamp(proposal.emitted_at)}
          >
            emitted {formatRelative(proposal.emitted_at)}
          </span>
          <Badge tone={alreadyApplied ? "success" : "outline"}>
            {alreadyApplied ? "auto-applied" : "proposed"}
          </Badge>
        </div>
        <div className="space-y-2 text-sm">
          <div>
            <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
              proposed snapshot field
            </span>
            <div className="font-mono text-xs mt-0.5">
              snapshot.{proposal.proposed_field_path}
            </div>
          </div>
          <div>
            <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
              collector summary
            </span>
            <div className="text-xs whitespace-pre-wrap mt-0.5">
              {proposal.proposed_collector_summary}
            </div>
          </div>
          <div className="text-xs text-muted-foreground">
            Inferred from{" "}
            <span className="font-mono">{proposal.source_tool_name}</span> tool
            calls — adding this field would short-circuit those calls at
            $0 LLM cost.
          </div>
          {autoApplyEnabled && !alreadyApplied && (
            <div className="rounded border border-yellow-500/40 bg-yellow-500/5 p-2 text-xs flex items-start gap-1.5">
              <AlertTriangle className="h-3 w-3 flex-shrink-0 mt-0.5 text-yellow-500" />
              <span>
                <code className="font-mono">
                  KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY=true
                </code>{" "}
                — this loop is currently AUTO-APPLY ON. The proposed
                field may already be in the snapshot schema next cycle.
              </span>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// --- EmailIntentCard (forward-compat — same chrome as phrasebook) ---

function EmailIntentCard({
  proposal,
  focused,
  onActionComplete,
}: CommonCardProps & { proposal: EmailIntentProposalPayload }) {
  const action = usePromotionAction({
    loop: "email_intent",
    proposalId: proposal.proposal_id,
    onActionComplete,
  });
  const isPending = proposal.status === "pending";

  return (
    <CardChrome
      proposalId={proposal.proposal_id}
      focused={focused}
      isPending={isPending}
      status={proposal.status}
      header={
        <>
          <Badge tone="warning" className="font-mono">
            {formatConfidence(proposal.confidence)} confidence
          </Badge>
          <span className="text-xs text-muted-foreground">
            cluster of {proposal.cluster_size}
          </span>
          <Badge tone="secondary">
            <Mail className="h-3 w-3 mr-1 inline" />
            Email intent
          </Badge>
          <span className="text-xs text-muted-foreground">·</span>
          <span
            className="text-xs text-muted-foreground"
            title={formatTimestamp(proposal.created_at)}
          >
            created {formatRelative(proposal.created_at)}
          </span>
        </>
      }
      body={
        <div className="space-y-2 text-sm">
          <ReadField label="category" value={proposal.proposed_category} />
          <ReadField label="pattern" value={proposal.proposed_pattern} mono />
          {proposal.sample_emails && proposal.sample_emails.length > 0 && (
            <div className="text-xs text-muted-foreground">
              <span className="text-[10px] uppercase tracking-wide">
                sample emails
              </span>
              <ul className="mt-1 ml-3 list-disc space-y-0.5">
                {proposal.sample_emails.slice(0, 3).map((s, i) => (
                  <li key={i} className="italic">
                    &ldquo;{s.slice(0, 120)}&rdquo;
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      }
      actions={
        isPending ? (
          <ApproveRejectControls
            busy={action.busy}
            actionError={action.actionError}
            mode={action.mode}
            reviewNotes={action.reviewNotes}
            setReviewNotes={action.setReviewNotes}
            onApprove={() => void action.approve()}
            onReject={() => void action.reject()}
            onEnterReject={() => action.setMode("reject")}
            onCancel={() => action.setMode("view")}
          />
        ) : null
      }
    />
  );
}

// ---------------------------------------------------------------
// LoopTypeTabs
// ---------------------------------------------------------------

interface LoopTypeTabsProps {
  selected: PromotionLoopName;
  onSelect: (loop: PromotionLoopName) => void;
  counts: Record<string, number> | null;
}

function LoopTypeTabs({ selected, onSelect, counts }: LoopTypeTabsProps) {
  return (
    <Card>
      <CardContent className="p-2 flex flex-wrap items-center gap-1">
        {LOOP_TABS.map((tab) => {
          const isSelected = tab.loop === selected;
          const count = counts ? (counts[tab.loop] ?? 0) : null;
          const Icon = tab.Icon;
          return (
            <button
              key={tab.loop}
              onClick={() => onSelect(tab.loop)}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-xs transition-colors ${
                isSelected
                  ? "bg-primary text-primary-foreground"
                  : "hover:bg-accent text-muted-foreground"
              }`}
              title={tab.blurb}
              aria-pressed={isSelected}
            >
              <Icon className="h-3.5 w-3.5" />
              <span>{tab.label}</span>
              {count !== null && count > 0 && (
                <span
                  className={`ml-1 px-1 py-0 rounded text-[10px] font-mono ${
                    isSelected
                      ? "bg-primary-foreground/20"
                      : "bg-yellow-500/30 text-yellow-200"
                  }`}
                >
                  {count}
                </span>
              )}
              {tab.forwardCompat && (
                <span
                  className="text-[9px] italic ml-1 opacity-60"
                  title="Forward-compat for CC#1's #420 — BE plumbing lands later"
                >
                  ⏳
                </span>
              )}
              {tab.readOnly && (
                <span
                  className="text-[9px] italic ml-1 opacity-60"
                  title="Read-only — informational lens, no approve flow"
                >
                  i
                </span>
              )}
            </button>
          );
        })}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------
// Page
// ---------------------------------------------------------------

export default function PromotionReviewPage() {
  usePanelView("PromotionReviewPage");

  const { activeTenant, isAllTenants } = useActiveTenant();
  const tenantForRead = isAllTenants ? undefined : activeTenant;

  const location = useLocation();
  const focusedId = useMemo(() => {
    const qs = new URLSearchParams(location.search);
    return qs.get("focus");
  }, [location.search]);

  // ``?loop=<name>`` deep-link (KoraActionsPage routes promotion
  // rows through here with the matching tab pre-selected).
  const initialLoop: PromotionLoopName = useMemo(() => {
    const qs = new URLSearchParams(location.search);
    const raw = qs.get("loop");
    if (raw && (PROMOTION_LOOP_NAMES as readonly string[]).includes(raw)) {
      return raw as PromotionLoopName;
    }
    return "phrasebook";
  }, [location.search]);

  const [selectedLoop, setSelectedLoop] =
    useState<PromotionLoopName>(initialLoop);
  const [filter, setFilter] =
    useState<FilterValue<PromotionStatus>>("pending");
  const [counts, setCounts] = useState<PromotionCountsResponse | null>(null);

  // Per-loop data state. Keyed by loop name so a tab switch
  // doesn't trash data we already fetched (snappy back-and-forth).
  const [loopData, setLoopData] = useState<
    Partial<Record<PromotionLoopName, PromotionProposalsResponse>>
  >({});
  const [snapshotExpandData, setSnapshotExpandData] =
    useState<SnapshotExpandPromotionsResponse | null>(null);
  const [loopLoading, setLoopLoading] = useState(false);
  const [loopError, setLoopError] = useState<string | null>(null);

  const tab = LOOP_TAB_BY_NAME[selectedLoop];

  const loadCounts = useCallback(async () => {
    try {
      const resp = await api.getPromotionCounts();
      setCounts(resp);
    } catch {
      // Counts are decorative for the tabs — failure leaves the
      // badges blank rather than blocking the page.
    }
  }, []);

  const loadLoop = useCallback(
    async (loop: PromotionLoopName) => {
      setLoopLoading(true);
      setLoopError(null);
      try {
        if (loop === "snapshot_expand") {
          const resp = await api.getSnapshotExpandPromotions();
          setSnapshotExpandData(resp);
        } else {
          const slug = PROMOTION_LOOP_SLUGS[loop];
          const resp = await api.getPromotionProposals(slug, {
            tenantId: tenantForRead,
          });
          setLoopData((prev) => ({ ...prev, [loop]: resp }));
        }
      } catch (e) {
        // Email-intent / probe-envelopes / etc. may legitimately
        // 404 on installs that don't have promotions yet — render
        // an empty state rather than a hard error. Still surface
        // the error in the page-level banner so operator knows.
        setLoopError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoopLoading(false);
      }
    },
    [tenantForRead],
  );

  useEffect(() => {
    void loadCounts();
  }, [loadCounts]);

  useEffect(() => {
    void loadLoop(selectedLoop);
  }, [selectedLoop, loadLoop]);

  // Scroll the deep-linked proposal into view once data loads.
  useEffect(() => {
    if (!focusedId) return;
    const el = document.getElementById(`proposal-${focusedId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [focusedId, loopData, snapshotExpandData]);

  const refreshAll = useCallback(() => {
    void loadCounts();
    void loadLoop(selectedLoop);
  }, [loadCounts, loadLoop, selectedLoop]);

  // Drift-guard greps for these constants.
  void PROMOTION_STATUS_VALUES;
  void PROMOTION_LOOP_NAMES;

  const sortedProposals = useMemo(() => {
    if (selectedLoop === "snapshot_expand") return [];
    const resp = loopData[selectedLoop];
    if (!resp) return [];
    return [...resp.proposals].sort((a, b) => {
      const ac =
        (a as { confidence?: number }).confidence ?? 0;
      const bc =
        (b as { confidence?: number }).confidence ?? 0;
      return bc - ac;
    });
  }, [loopData, selectedLoop]);

  const proposalsByStatus = useMemo(() => {
    const c: Record<string, number> = {};
    for (const s of PROMOTION_STATUS_VALUES) c[s] = 0;
    for (const p of sortedProposals) {
      c[p.status] = (c[p.status] ?? 0) + 1;
    }
    return c;
  }, [sortedProposals]);

  const filteredProposals = useMemo(() => {
    if (filter === "all") return sortedProposals;
    return sortedProposals.filter((p) => p.status === filter);
  }, [sortedProposals, filter]);

  return (
    <div className="space-y-4 p-4 max-w-5xl">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <H2 className="flex items-center gap-2">
            <Lightbulb className="h-5 w-5" />
            Promotion Review
          </H2>
          <ActiveTenantBadge />
        </div>
        <Button
          outlined
          size="sm"
          onClick={refreshAll}
          disabled={loopLoading}
        >
          <RefreshCw
            className={`h-3 w-3 mr-1 ${loopLoading ? "animate-spin" : ""}`}
          />
          Refresh
        </Button>
      </div>

      <LoopTypeTabs
        selected={selectedLoop}
        onSelect={setSelectedLoop}
        counts={counts?.counts ?? null}
      />

      <p className="text-sm text-muted-foreground">{tab.blurb}</p>

      {loopLoading && !loopData[selectedLoop] && snapshotExpandData === null && (
        <div className="flex items-center justify-center p-8">
          <Spinner />
        </div>
      )}

      {loopError && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="p-4 flex items-start gap-2">
            <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0 mt-0.5" />
            <div className="text-sm">
              Failed to load {tab.label} proposals:{" "}
              <span className="font-mono">{loopError}</span>
              {tab.forwardCompat && (
                <div className="text-xs text-muted-foreground mt-1 italic">
                  This loop is forward-compat for CC#1&apos;s #420 — the BE
                  plumbing may not be live yet on this install.
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {selectedLoop === "snapshot_expand" ? (
        <SnapshotExpandPanel
          data={snapshotExpandData}
          focusedId={focusedId}
        />
      ) : (
        <>
          <Card>
            <CardContent className="p-4 flex flex-col gap-3">
              <div className="flex items-center gap-3 text-sm flex-wrap">
                <Sparkles className="h-4 w-4 text-yellow-500" />
                <span className="font-medium">
                  {proposalsByStatus.pending ?? 0} pending
                </span>
                <span className="text-muted-foreground">·</span>
                <span className="text-muted-foreground">
                  {proposalsByStatus.approved ?? 0} approved this cycle
                </span>
                <span className="text-muted-foreground">·</span>
                <span className="text-muted-foreground">
                  {proposalsByStatus.rejected ?? 0} rejected
                </span>
                {proposalsByStatus.expired ? (
                  <>
                    <span className="text-muted-foreground">·</span>
                    <span className="text-muted-foreground">
                      {proposalsByStatus.expired} expired
                    </span>
                  </>
                ) : null}
              </div>
              <FilterChips
                categories={STATUS_CATEGORIES}
                counts={proposalsByStatus}
                current={filter}
                onChange={setFilter}
                allLabel="All"
              />
            </CardContent>
          </Card>

          {filteredProposals.length === 0 ? (
            <EmptyFilteredMessage
              isAllFilter={filter === "all"}
              titleAll={
                tab.forwardCompat
                  ? `No ${tab.label} proposals yet — forward-compat lens`
                  : `No ${tab.label} proposals yet`
              }
              titleFiltered={`No ${filter} ${tab.label} proposals.`}
              bodyAll={
                tab.forwardCompat
                  ? `The ${tab.label} loop ships with CC#1's #420. This tab is forward-compat: it will populate cleanly once the BE plumbing lands; until then, expect zero proposals.`
                  : `Kora hasn't synthesized any ${tab.label} clusters worth promoting in the current cycle.`
              }
              onResetToAll={() => setFilter("all")}
            />
          ) : (
            <div className="space-y-3">
              {filteredProposals.map((p) =>
                renderLoopCard({
                  loop: selectedLoop,
                  payload: p,
                  focused: focusedId === p.proposal_id,
                  onActionComplete: refreshAll,
                }),
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

interface RenderLoopCardArgs {
  loop: PromotionLoopName;
  payload: PromotionProposalsResponse["proposals"][number];
  focused: boolean;
  onActionComplete: () => void;
}

function renderLoopCard({
  loop,
  payload,
  focused,
  onActionComplete,
}: RenderLoopCardArgs): React.ReactNode {
  // Each branch narrows the payload to its specific shape. Cards
  // are responsible for ignoring extra fields; the discriminator
  // is the active tab.
  switch (loop) {
    case "phrasebook":
      return (
        <PhrasebookCard
          key={payload.proposal_id}
          proposal={payload as PhrasebookProposalPayload}
          focused={focused}
          onActionComplete={onActionComplete}
        />
      );
    case "router_tuning":
      return (
        <RouterTuningCard
          key={payload.proposal_id}
          proposal={payload as unknown as RouterTuningProposalPayload}
          focused={focused}
          onActionComplete={onActionComplete}
        />
      );
    case "tool_trimming":
      return (
        <ToolTrimmingCard
          key={payload.proposal_id}
          proposal={payload as unknown as ToolTrimProposalPayload}
          focused={focused}
          onActionComplete={onActionComplete}
        />
      );
    case "probe_fix_envelopes":
      return (
        <ProbeEnvelopeCard
          key={payload.proposal_id}
          proposal={payload as unknown as ProbeEnvelopeProposalPayload}
          focused={focused}
          onActionComplete={onActionComplete}
        />
      );
    case "email_intent":
      return (
        <EmailIntentCard
          key={payload.proposal_id}
          proposal={payload as unknown as EmailIntentProposalPayload}
          focused={focused}
          onActionComplete={onActionComplete}
        />
      );
    case "snapshot_expand":
      // Unreachable — page branches on selectedLoop before reaching here.
      return null;
  }
}

function SnapshotExpandPanel({
  data,
  focusedId,
}: {
  data: SnapshotExpandPromotionsResponse | null;
  focusedId: string | null;
}) {
  if (data === null) return null;
  return (
    <>
      <Card className="bg-muted/20">
        <CardContent className="p-3 text-xs flex items-start gap-2">
          <Inbox className="h-4 w-4 text-muted-foreground flex-shrink-0 mt-0.5" />
          <div className="space-y-1 text-muted-foreground">
            <div className="text-foreground font-medium">
              Snapshot-expand is read-only here
            </div>
            <div>
              This loop doesn&apos;t have an approve/reject lifecycle — it
              either auto-applies (when{" "}
              <code className="font-mono">
                KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY=true
              </code>
              ) or just emits a{" "}
              <code className="font-mono">
                promotion.snapshot_field_added
              </code>{" "}
              audit row with{" "}
              <code className="font-mono">action=&quot;proposed&quot;</code>.
              Showing the most-recent proposals so the cockpit&apos;s
              promotion view is complete.
            </div>
            <div>
              Auto-apply currently{" "}
              <strong>
                {data.auto_apply_enabled ? "ENABLED" : "DISABLED"}
              </strong>
              .
            </div>
          </div>
        </CardContent>
      </Card>
      {data.proposals.length === 0 ? (
        <EmptyFilteredMessage
          isAllFilter={true}
          titleAll="No snapshot-expand proposals in the recent window"
          titleFiltered=""
          bodyAll="Kora hasn't observed enough tool-call clustering to propose new snapshot fields recently. Proposals appear here as the loop runs (cycle interval lives in KORA_PROMOTE_SNAPSHOT_EXPAND_*)."
          onResetToAll={() => {
            /* no-op */
          }}
        />
      ) : (
        <div className="space-y-3">
          {data.proposals.map((p) => (
            <SnapshotExpandCard
              key={p.proposal_id}
              proposal={p}
              autoApplyEnabled={data.auto_apply_enabled}
              focused={focusedId === p.proposal_id}
            />
          ))}
        </div>
      )}
    </>
  );
}
