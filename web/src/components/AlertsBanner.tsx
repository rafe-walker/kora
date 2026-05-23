// Compact alerts banner for the top of DashboardPage. Hidden when
// there are no active alerts (no false-alarm trigger from absent
// data). Dismissible per-tab via sessionStorage — alerts come BACK
// next session because they're derived from source-panel state, not
// acknowledged-and-cleared.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertOctagon,
  AlertTriangle,
  ArrowRight,
  Info,
  X,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import type { AlertsResponse } from "@/lib/api";

// sessionStorage key — per-tab semantics: dismissal lives for the
// tab's lifetime and resets on close. The spec explicitly says NOT
// acknowledged-state; just a visual collapse. localStorage would be
// per-browser and persist across tab close (wrong); sessionStorage
// is the right scope.
const DISMISS_KEY = "kora.alerts.banner.dismissed";

export function AlertsBanner({ data }: { data: AlertsResponse | null }) {
  const [dismissed, setDismissed] = useState(false);

  // Read dismissal once on mount. The key carries a hash of the
  // current alert id-set so re-dismissal is required when new
  // alerts arrive — otherwise dismissing once would suppress all
  // future alerts until tab close.
  const alertIdsKey = data
    ? data.alerts.map((a) => a.id).sort().join(",")
    : "";

  useEffect(() => {
    try {
      const stored = sessionStorage.getItem(DISMISS_KEY);
      setDismissed(stored !== null && stored === alertIdsKey);
    } catch {
      // sessionStorage can throw in private-browsing on some
      // browsers; treat as "not dismissed" rather than crashing.
      setDismissed(false);
    }
  }, [alertIdsKey]);

  function handleDismiss() {
    try {
      sessionStorage.setItem(DISMISS_KEY, alertIdsKey);
    } catch {
      // ignore — best-effort persistence
    }
    setDismissed(true);
  }

  // Hidden when:
  //   - data not loaded yet (no false-alarm flash before fetch)
  //   - no active alerts (empty state shown in the full panel, not
  //     as a banner — dashboard stays clean)
  //   - operator dismissed for this tab
  if (data === null) return null;
  if (data.alerts.length === 0) return null;
  if (dismissed) return null;

  const critical = data.by_severity.critical ?? 0;
  const warning = data.by_severity.warning ?? 0;
  const info = data.by_severity.info ?? 0;

  // Banner border tone tracks the worst-severity in the active set
  // so the operator's peripheral vision catches it before reading.
  const borderClass =
    critical > 0
      ? "border-destructive/50 bg-destructive/5"
      : warning > 0
        ? "border-warning/50 bg-warning/5"
        : "border-primary/40 bg-primary/5";

  return (
    <Card className={borderClass}>
      <CardContent className="py-3 flex items-center gap-3 text-sm">
        {critical > 0 ? (
          <AlertOctagon className="h-4 w-4 text-destructive shrink-0" />
        ) : warning > 0 ? (
          <AlertTriangle className="h-4 w-4 text-warning shrink-0" />
        ) : (
          <Info className="h-4 w-4 text-primary shrink-0" />
        )}
        <span className="font-medium">
          {data.total_active} active alert
          {data.total_active === 1 ? "" : "s"}
        </span>
        {critical > 0 && (
          <span className="flex items-center gap-1 text-xs text-destructive">
            <AlertOctagon className="h-3 w-3" />
            {critical} critical
          </span>
        )}
        {warning > 0 && (
          <span className="flex items-center gap-1 text-xs text-warning">
            <AlertTriangle className="h-3 w-3" />
            {warning} warning
          </span>
        )}
        {info > 0 && (
          <span className="flex items-center gap-1 text-xs text-primary">
            <Info className="h-3 w-3" />
            {info} info
          </span>
        )}
        <Link
          to="/alerts"
          className="ml-auto inline-flex items-center gap-1 text-xs hover:text-foreground underline-offset-2 hover:underline"
        >
          Open alerts
          <ArrowRight className="h-3 w-3" />
        </Link>
        <button
          type="button"
          onClick={handleDismiss}
          className="text-muted-foreground hover:text-foreground"
          title="Dismiss for this tab (returns next session)"
          aria-label="Dismiss alerts banner"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </CardContent>
    </Card>
  );
}
