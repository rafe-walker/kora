// Panel-view instrumentation hook — KR-PANEL-USE-INSTRUMENTATION.
//
// Per Council R3 lock sub-cut (c): every top-level *Page.tsx /
// *Panel.tsx calls usePanelView at mount so the backend
// /api/panel_view sink accretes operator-UX telemetry that
// informs any future panel-design decisions.
//
// Fire-and-forget semantics:
//   * Failures are silently swallowed — instrumentation MUST
//     NEVER break operator UX (the panel still renders even if
//     the POST fails / the daemon is unreachable / sessionStorage
//     is denied by browser policy).
//   * Empty useEffect deps (only panelName, which is a
//     compile-time constant per call site) so re-renders don't
//     produce duplicate emits. NOTE: React 18+ strict-mode runs
//     effects twice in dev — that's a documented dev-only quirk
//     and irrelevant for prod telemetry analysis.
//
// Session-id discipline:
//   * sessionStorage key "kora_session_id" (per-tab scope; resets
//     on tab close).
//   * Auto-generated UUID on first access if missing, so analytics
//     can group views by tab session rather than every event being
//     "unknown".
//   * Falls back to "unknown" if sessionStorage throws (private
//     browsing / strict cookie modes) so the emit still goes
//     through.

import { useEffect } from "react";
import { fetchJSON } from "@/lib/api";

const SESSION_ID_KEY = "kora_session_id";

function getOrCreateSessionId(): string {
  try {
    const existing = window.sessionStorage.getItem(SESSION_ID_KEY);
    if (existing) return existing;
    // Per-tab uuid — crypto.randomUUID is available in modern browsers
    // (Chromium 92+ / Firefox 95+ / Safari 15.4+); both Kora's
    // supported targets. Fallback to Math.random base36 for the
    // unlikely older-browser case so we don't crash the hook.
    const next =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `tab-${Math.random().toString(36).slice(2, 10)}-${Date.now()}`;
    window.sessionStorage.setItem(SESSION_ID_KEY, next);
    return next;
  } catch {
    return "unknown";
  }
}

/**
 * Emits a panel_view event when the component mounts.
 *
 * Usage in any top-level Page/Panel (NOT internal components):
 *   export default function AlertsPanel() {
 *     usePanelView("AlertsPanel");
 *     // ... component body ...
 *   }
 *
 * The panel_name string should match the component name verbatim
 * so downstream queries against panel_views.jsonl can group by
 * file/component without extra mapping.
 */
export function usePanelView(panelName: string): void {
  useEffect(() => {
    const sessionId = getOrCreateSessionId();
    fetchJSON("/api/panel_view", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        panel_name: panelName,
        session_id: sessionId,
      }),
    }).catch(() => {
      // Silent failure — instrumentation must never break UX.
      // The daemon may be down, the browser may have denied the
      // request, the user may have content-blockers; none of
      // those should surface to the operator.
    });
  }, [panelName]);
}
