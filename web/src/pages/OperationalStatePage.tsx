import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
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
import { usePanelView } from "@/hooks/usePanelView";
import type {
  ClaimPermission,
  DegradationReason,
  OperationalStateResponse,
  PrimaryState,
} from "@/lib/api";

const PRIMARY_STATE_TONE: Record<PrimaryState, "success" | "warning" | "destructive" | "outline"> = {
  booting: "warning",
  ready: "success",
  active: "success",
  paused: "warning",
  stopped: "destructive",
};

const CLAIM_PERMISSION_TONE: Record<ClaimPermission, "success" | "warning" | "destructive"> = {
  normal: "success",
  critical_only: "warning",
  none: "destructive",
};

const DEGRADATION_REASON_BLURB: Record<DegradationReason, string> = {
  cost: "Cost cap pressure",
  auth: "Auth/credential issue",
  dispatch: "Dispatch pipeline degraded",
  substrate: "Substrate connectivity issue",
  migration: "Schema/data migration in progress",
  operator: "Operator-initiated degradation",
  token_expiring: "Auth token approaching expiry",
  retry_ceiling: "Retry ceiling hit",
};

function formatTimestamp(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function uppercaseLabel(value: string): string {
  return value.toUpperCase().replace(/_/g, " ");
}

export default function OperationalStatePage() {
  usePanelView("OperationalStatePage");

  const [state, setState] = useState<OperationalStateResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const { toast, showToast } = useToast();

  const loadState = useCallback(
    (isManualReload: boolean) => {
      if (isManualReload) setRefreshing(true);
      setLoadError(null);
      api
        .getOperationalState()
        .then((resp) => setState(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load operational state: ${msg}`, "error");
        })
        .finally(() => {
          if (isManualReload) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadState(false);
  }, [loadState]);

  if (state === null && !loadError) {
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
          <H2>Operational State</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Kora's current runtime state — what she's doing and what she could
            transition to.
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
              <div className="font-medium">Failed to load operational state</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {state?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <ShieldAlert className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB DATA — operational state machine wire-in pending (KR-P2-I-integration)
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                This panel is a UI preview; values shown are not the real
                runtime state.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {state && (
        <>
          <Card>
            <CardContent className="flex flex-col gap-4 py-5">
              <div className="flex flex-wrap items-center gap-3">
                <Badge tone={PRIMARY_STATE_TONE[state.primary_state]}>
                  <span className="font-semibold tracking-wide">
                    {uppercaseLabel(state.primary_state)}
                  </span>
                </Badge>
                {state.is_degraded && (
                  <Badge tone="destructive">DEGRADED</Badge>
                )}
                <Badge tone={CLAIM_PERMISSION_TONE[state.claim_permission]}>
                  claim: {state.claim_permission}
                </Badge>
              </div>

              {state.degradation_reasons.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <div className="text-xs font-medium text-muted-foreground">
                    Degradation reasons
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {state.degradation_reasons.map((reason) => (
                      <span
                        key={reason}
                        title={DEGRADATION_REASON_BLURB[reason] ?? reason}
                        className="rounded border border-warning/40 bg-warning/10 px-2 py-0.5 text-xs"
                      >
                        {reason}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="flex flex-col gap-3 py-4">
              <div className="text-sm font-medium">Transition history</div>
              {state.transition_history.length === 0 ? (
                <div className="text-sm text-muted-foreground">
                  No transitions recorded.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-xs text-muted-foreground border-b">
                        <th className="py-2 pr-3 font-medium">Timestamp</th>
                        <th className="py-2 pr-3 font-medium">From</th>
                        <th className="py-2 pr-3 font-medium">To</th>
                        <th className="py-2 font-medium">Trigger</th>
                      </tr>
                    </thead>
                    <tbody>
                      {state.transition_history.map((t, i) => (
                        <tr
                          key={`${t.timestamp}-${i}`}
                          className="border-b last:border-0"
                        >
                          <td className="py-2 pr-3 font-mono text-xs">
                            {formatTimestamp(t.timestamp)}
                          </td>
                          <td className="py-2 pr-3">
                            <code className="text-xs">{t.from_state}</code>
                          </td>
                          <td className="py-2 pr-3">
                            <code className="text-xs">{t.to_state}</code>
                          </td>
                          <td className="py-2 text-xs text-muted-foreground">
                            {t.trigger}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="flex flex-col gap-3 py-4">
              <div className="text-sm font-medium">Valid next states</div>
              {state.valid_next_states.length === 0 ? (
                <div className="text-sm text-muted-foreground">
                  No transitions available from{" "}
                  <code className="text-xs">{state.primary_state}</code>.
                </div>
              ) : (
                <ul className="flex flex-col gap-2">
                  {state.valid_next_states.map((n) => (
                    <li
                      key={n.to_state}
                      className="flex items-start gap-2 text-sm"
                    >
                      <ArrowRight className="h-4 w-4 mt-0.5 text-muted-foreground shrink-0" />
                      <div>
                        <code className="text-xs font-semibold">
                          {n.to_state}
                        </code>
                        <span className="text-xs text-muted-foreground ml-2">
                          {n.trigger}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
