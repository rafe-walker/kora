// KR-FE-OPERATOR-FIRST-RUN-WIZARD — first-run detection.
//
// Fires ONCE at app boot to ask the BE whether the wizard should
// be shown at "/". The combined signal (per the spec):
//
//   showWizard = !marker_present && audit_log_empty
//
// ``marker_present + completed`` means operator finished the wizard
// already; ``marker_present + skipped`` means operator dismissed
// with "I'll configure manually" — both flip showWizard false so
// the cockpit goes back to Dashboard. If the BE call fails (e.g.
// daemon not reachable), we default showWizard=false rather than
// trap the operator in an unrecoverable wizard.
//
// Returns ``null`` while loading so the App can render a Spinner
// rather than flicker Dashboard then immediately re-render Wizard.

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

export interface FirstRunDetectionResult {
  showWizard: boolean;
  /** Whether the BE has answered yet — null = still loading. */
  loaded: boolean;
}

export function useWizardFirstRunDetection(): FirstRunDetectionResult {
  const [state, setState] = useState<FirstRunDetectionResult>({
    showWizard: false,
    loaded: false,
  });

  useEffect(() => {
    let cancelled = false;
    void api
      .getWizardState()
      .then((resp) => {
        if (cancelled) return;
        const showWizard =
          !resp.marker_present && resp.audit_log_empty;
        setState({ showWizard, loaded: true });
      })
      .catch(() => {
        // Best-effort: failed fetch means "no idea — fall back to
        // Dashboard". Operator can still navigate to /wizard via
        // a URL if they want to re-run the flow manually.
        if (cancelled) return;
        setState({ showWizard: false, loaded: true });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}
