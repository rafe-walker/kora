import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  RefreshCw,
  ShieldCheck,
  ShieldX,
  TriangleAlert,
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
import { usePanelView } from "@/hooks/usePanelView";
import type {
  CapabilitiesResponse,
  CapabilityGroup,
  CapVerdict,
} from "@/lib/api";

const VERDICT_TONE: Record<CapVerdict, "success" | "destructive" | "warning"> = {
  granted: "success",
  denied: "destructive",
  unmapped_in_c2_mirror: "warning",
  error: "destructive",
};

const VERDICT_LABEL: Record<CapVerdict, string> = {
  granted: "granted",
  denied: "denied",
  unmapped_in_c2_mirror: "unmapped — INCONCLUSIVE",
  error: "error",
};

function VerdictIcon({ verdict }: { verdict: CapVerdict }) {
  switch (verdict) {
    case "granted":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "denied":
      return <XCircle className="h-4 w-4 text-destructive" />;
    case "unmapped_in_c2_mirror":
      return <TriangleAlert className="h-4 w-4 text-warning" />;
    case "error":
      return <AlertTriangle className="h-4 w-4 text-destructive" />;
  }
}

interface CapGroupCardProps {
  group: CapabilityGroup;
}

function CapGroupCard({ group }: CapGroupCardProps) {
  const cardBorder =
    group.verdict === "granted"
      ? "border-success/30"
      : group.verdict === "denied" || group.verdict === "error"
        ? "border-destructive/40"
        : "border-warning/40";

  return (
    <Card className={cardBorder}>
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex items-center gap-2 flex-wrap">
          <VerdictIcon verdict={group.verdict} />
          <code className="text-sm font-semibold">{group.cap_name}</code>
          <Badge tone={VERDICT_TONE[group.verdict]}>
            {VERDICT_LABEL[group.verdict]}
          </Badge>
          <span className="text-xs text-muted-foreground ml-auto">
            {group.tools.length} tool{group.tools.length === 1 ? "" : "s"}
          </span>
        </div>

        {group.verdict === "unmapped_in_c2_mirror" && (
          <p className="text-xs text-muted-foreground">
            Cap not in C2 mirror yet. Tools in this group escalate INCONCLUSIVE
            per fail-CLOSED policy. Closes when substrate-team KR-P2-N ships.
          </p>
        )}

        {group.verdict === "error" && (
          <p className="text-xs text-destructive">
            Unexpected error resolving this cap. Check server logs.
          </p>
        )}

        <div className="flex flex-wrap gap-1.5">
          {group.tools.map((tool) => (
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
  );
}

export default function CapabilitiesPage() {
  usePanelView("CapabilitiesPage");

  const [data, setData] = useState<CapabilitiesResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const { toast, showToast } = useToast();

  const loadCapabilities = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getCapabilities()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load capabilities: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadCapabilities(false);
  }, [loadCapabilities]);

  const verdictCounts = useMemo(() => {
    if (!data) return null;
    const counts: Record<CapVerdict, number> = {
      granted: 0,
      denied: 0,
      unmapped_in_c2_mirror: 0,
      error: 0,
    };
    for (const g of data.groups) counts[g.verdict]++;
    return counts;
  }, [data]);

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
          <H2>Capabilities</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Tools Kora is permitted to invoke + their cap_* policy verdict.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadCapabilities(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load capabilities</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Summary banner ─────────────────────────────────────── */}
          <Card>
            <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 py-3 text-sm">
              <span className="font-medium">
                {data.total_tools} tools across {data.total_caps} cap_* groups
              </span>
              {verdictCounts && (
                <>
                  <span className="flex items-center gap-1.5 text-xs">
                    <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                    {verdictCounts.granted} granted
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <XCircle className="h-3.5 w-3.5 text-destructive" />
                    {verdictCounts.denied} denied
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <TriangleAlert className="h-3.5 w-3.5 text-warning" />
                    {data.unmapped_count} unmapped (INCONCLUSIVE)
                  </span>
                  {verdictCounts.error > 0 && (
                    <span className="flex items-center gap-1.5 text-xs text-destructive">
                      <AlertTriangle className="h-3.5 w-3.5" />
                      {verdictCounts.error} error
                    </span>
                  )}
                </>
              )}
              {data.unmapped_count > 0 && (
                <span className="text-xs text-muted-foreground ml-auto">
                  Unmapped groups documented: D-krp2a-st1
                </span>
              )}
            </CardContent>
          </Card>

          {/* ── Substrate-enforced section ────────────────────────── */}
          <Card className="border-success/30">
            <CardContent className="flex flex-col gap-3 py-4">
              <div className="flex items-center gap-2 flex-wrap">
                <ShieldCheck className="h-4 w-4 text-success" />
                <span className="font-medium">Substrate-enforced</span>
                <Badge tone="success">always PASS</Badge>
                <span className="text-xs text-muted-foreground ml-auto">
                  {data.substrate_tier.length} tool
                  {data.substrate_tier.length === 1 ? "" : "s"}
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                The pre-screen short-circuits any name starting with{" "}
                <code>kora__</code> with PASS — substrate-side dispatch is the
                authoritative capability gate; re-checking here would duplicate
                the substrate-side check and risk drift.
              </p>
              <div className="flex flex-wrap gap-1.5">
                {data.substrate_tier.map((tool) => (
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

          {/* ── Capability groups (sorted alphabetical by cap_name) ─ */}
          <section className="flex flex-col gap-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <ShieldX className="h-4 w-4" />
              Capability groups
              <span className="text-xs text-muted-foreground">
                ({data.groups.length} groups)
              </span>
            </div>
            {data.groups.length === 0 ? (
              <Card>
                <CardContent className="py-4 text-sm text-muted-foreground">
                  No capability groups discovered. This is unexpected — check
                  that <code>TOOL_CAPABILITY_MAP</code> is populated.
                </CardContent>
              </Card>
            ) : (
              data.groups.map((g) => <CapGroupCard key={g.cap_name} group={g} />)
            )}
          </section>
        </>
      )}
    </div>
  );
}
