import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Copy,
  ExternalLink,
  Hash,
  Info,
  RefreshCw,
  Scroll,
  ShieldCheck,
  ShieldX,
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
  ActiveConstitution,
  CharterCapabilityGroup,
  CharterResponse,
  ConstitutionRule,
} from "@/lib/api";

const RULES_HASH_DISPLAY_LENGTH = 16;

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
  if (absSec < 60) return deltaMs < 0 ? "just now" : "in <1m";
  const absMin = absSec / 60;
  if (absMin < 60) {
    const n = Math.round(absMin);
    return deltaMs < 0 ? `${n}m ago` : `in ${n}m`;
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

function truncateMiddle(value: string, head: number): string {
  if (value.length <= head + 4) return value;
  return `${value.slice(0, head)}…${value.slice(-4)}`;
}

interface CopyableCodeProps {
  value: string;
  truncate?: number;
  label?: string;
  onCopy: (label: string) => void;
}

function CopyableCode({ value, truncate, label = "value", onCopy }: CopyableCodeProps) {
  const display = truncate ? truncateMiddle(value, truncate) : value;
  return (
    <button
      type="button"
      onClick={(e) => {
        e.preventDefault();
        navigator.clipboard
          .writeText(value)
          .then(() => onCopy(label))
          .catch(() => onCopy(label));
      }}
      title={`Click to copy ${label}: ${value}`}
      className="group inline-flex items-center gap-1.5 rounded border border-border bg-muted/40 px-2 py-0.5 text-xs font-mono hover:border-primary/50 transition-colors"
    >
      <span>{display}</span>
      <Copy className="h-3 w-3 opacity-0 group-hover:opacity-60" />
    </button>
  );
}

interface ActiveRevisionCardProps {
  active: ActiveConstitution | null;
  onCopy: (label: string) => void;
}

function ActiveRevisionCard({ active, onCopy }: ActiveRevisionCardProps) {
  if (active === null) {
    return (
      <Card className="border-warning/40">
        <CardContent className="flex items-start gap-3 py-4 text-sm">
          <AlertTriangle className="h-4 w-4 mt-0.5 text-warning shrink-0" />
          <div>
            <div className="font-medium">No active Constitution loaded</div>
            <div className="text-xs text-muted-foreground mt-1">
              Either the IsoKron memory provider isn't registered in this
              environment, or no agent turn has primed the constitution
              cache yet. Run a chat turn against a configured workspace,
              then Reload.
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-primary/20">
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex items-center gap-2">
          <Scroll className="h-4 w-4 text-primary" />
          <span className="text-sm font-medium">Active revision</span>
        </div>

        <dl className="flex flex-col gap-2 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <dt className="text-muted-foreground min-w-[110px]">revision_id</dt>
            <dd>
              {active.revision_id ? (
                <CopyableCode
                  value={active.revision_id}
                  label="revision_id"
                  onCopy={onCopy}
                />
              ) : (
                <span className="text-muted-foreground">
                  — (fresh workspace, no revisions)
                </span>
              )}
            </dd>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <dt className="text-muted-foreground min-w-[110px]">rules_hash</dt>
            <dd className="flex items-center gap-2">
              <Hash className="h-3 w-3 text-muted-foreground" />
              {active.rules_hash ? (
                <CopyableCode
                  value={active.rules_hash}
                  truncate={RULES_HASH_DISPLAY_LENGTH}
                  label="rules_hash"
                  onCopy={onCopy}
                />
              ) : (
                <span className="text-muted-foreground">—</span>
              )}
            </dd>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <dt className="text-muted-foreground min-w-[110px]">loaded_at</dt>
            <dd>
              {formatRelative(active.loaded_at)} (
              {formatTimestamp(active.loaded_at)})
            </dd>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <dt className="text-muted-foreground min-w-[110px]">workspace_id</dt>
            <dd>
              <CopyableCode
                value={active.workspace_id}
                truncate={RULES_HASH_DISPLAY_LENGTH}
                label="workspace_id"
                onCopy={onCopy}
              />
            </dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  );
}

interface RulesNotAvailableBannerProps {
  active: ActiveConstitution | null;
}

function RulesNotAvailableBanner({ active }: RulesNotAvailableBannerProps) {
  // Only render when we have an active revision but rules content is
  // not exposed. Hide entirely when active is null (the "no active
  // revision" card already explains the empty state).
  if (active === null || active.rules_available) return null;
  return (
    <Card className="border-warning/40 bg-warning/10">
      <CardContent className="py-3 flex items-start gap-3 text-sm">
        <Info className="h-4 w-4 mt-0.5 text-warning shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="font-medium">
            Constitution rules not exposed via Kora-tier runtime read
          </div>
          <div className="text-xs text-muted-foreground mt-1">
            See the substrate-side cockpit for full rule text. The Kora
            runtime knows the active <code>revision_id</code> and{" "}
            <code>rules_hash</code> for audit integrity, but the rule
            bodies live behind a substrate read SECDEF that isn't
            exposed to Kora-tier callers yet.
          </div>
          <div className="text-xs text-muted-foreground mt-2 flex items-center gap-1">
            <ExternalLink className="h-3 w-3" />
            Pending: substrate-team rule-content read SECDEF
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

interface RulesSectionProps {
  active: ActiveConstitution | null;
}

function RulesSection({ active }: RulesSectionProps) {
  // Only render when content is actually available. Group by scope.
  if (active === null || !active.rules_available || active.rules.length === 0) {
    return null;
  }
  const byScope: Record<string, ConstitutionRule[]> = {};
  for (const r of active.rules) {
    (byScope[r.scope] ??= []).push(r);
  }
  const scopes = Object.keys(byScope).sort();
  return (
    <section className="flex flex-col gap-3">
      <div className="text-sm font-medium">
        Rules ({active.rules.length})
      </div>
      {scopes.map((scope) => (
        <Card key={scope}>
          <CardContent className="flex flex-col gap-2 py-3">
            <div className="flex items-center gap-2">
              <Badge tone="outline">{scope}</Badge>
              <span className="text-xs text-muted-foreground">
                {byScope[scope].length} rule
                {byScope[scope].length === 1 ? "" : "s"}
              </span>
            </div>
            <ul className="flex flex-col gap-1.5">
              {byScope[scope].map((r) => (
                <li key={r.rule_id} className="flex items-start gap-2 text-sm">
                  <code className="text-xs text-muted-foreground shrink-0 w-12">
                    {r.rule_id}
                  </code>
                  <Badge tone="outline">{r.severity}</Badge>
                  <span className="text-xs">{r.description}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ))}
    </section>
  );
}

interface CapMatrixSectionProps {
  groups: CharterCapabilityGroup[];
  substrateTier: string[];
}

function CapMatrixSection({ groups, substrateTier }: CapMatrixSectionProps) {
  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <ShieldX className="h-4 w-4" />
        Capability matrix
        <span className="text-xs text-muted-foreground">
          ({groups.length} cap_* groups · {substrateTier.length} substrate-tier
          tools)
        </span>
      </div>
      <p className="text-xs text-muted-foreground -mt-2">
        Policy map drives per-cap allow/deny in the Capabilities panel.
      </p>

      <Card className="border-success/30">
        <CardContent className="flex flex-col gap-3 py-4">
          <div className="flex items-center gap-2 flex-wrap">
            <ShieldCheck className="h-4 w-4 text-success" />
            <span className="font-medium">Substrate-enforced</span>
            <Badge tone="success">always PASS</Badge>
            <span className="text-xs text-muted-foreground ml-auto">
              {substrateTier.length} tool
              {substrateTier.length === 1 ? "" : "s"}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            The pre-screen short-circuits any name starting with{" "}
            <code>kora__</code> — substrate-side dispatch is the
            authoritative capability gate.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {substrateTier.map((tool) => (
              <code
                key={tool}
                className="rounded border border-success/30 bg-success/10 px-2 py-0.5 text-xs"
              >
                {tool}
              </code>
            ))}
          </div>
        </CardContent>
      </Card>

      {groups.length === 0 ? (
        <Card>
          <CardContent className="py-4 text-sm text-muted-foreground">
            No capability groups discovered. This is unexpected — check
            that <code>TOOL_CAPABILITY_MAP</code> is populated.
          </CardContent>
        </Card>
      ) : (
        groups.map((g) => (
          <Card key={g.cap_name}>
            <CardContent className="flex flex-col gap-2 py-3">
              <div className="flex items-center gap-2 flex-wrap">
                <code className="text-sm font-semibold">{g.cap_name}</code>
                <span className="text-xs text-muted-foreground ml-auto">
                  {g.tools.length} tool{g.tools.length === 1 ? "" : "s"}
                </span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {g.tools.map((tool) => (
                  <code
                    key={tool}
                    className="rounded border border-border bg-muted/40 px-2 py-0.5 text-xs"
                  >
                    {tool}
                  </code>
                ))}
              </div>
            </CardContent>
          </Card>
        ))
      )}
    </section>
  );
}

export default function CharterPage() {
  const [data, setData] = useState<CharterResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const { toast, showToast } = useToast();

  const loadCharter = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getCharter()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load charter: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadCharter(false);
  }, [loadCharter]);

  const handleCopy = useCallback(
    (label: string) => {
      showToast(`${label} copied`, "success");
    },
    [showToast],
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
          <H2>Charter</H2>
          <p className="text-sm text-muted-foreground mt-1">
            The Constitution Kora is operating under.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadCharter(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load charter</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <ActiveRevisionCard active={data.active} onCopy={handleCopy} />
          <RulesNotAvailableBanner active={data.active} />
          <RulesSection active={data.active} />
          <CapMatrixSection
            groups={data.capability_groups}
            substrateTier={data.substrate_tier_tools}
          />
        </>
      )}
    </div>
  );
}
