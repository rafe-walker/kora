// Shared FE helpers used across the 12 admin panels — KR-FE-PANEL-HELPERS-DRY.
// Eliminates ~300 LOC of duplication: formatRelative was copy-pasted across
// HeartbeatPanel / MCPClientsPanel / WebhookEventsPanel / AgentActivityPanel /
// SlackDMPanel / EmailPanel / ReasoningPanel / AlertsPanel (and the duration
// helper was duplicated in 2 of those).
//
// All helpers accept `null | undefined` so panels don't need wrapper logic
// for missing-data paths. Render semantics match the most-cautious panel's
// behavior (HeartbeatPanel post-KR-FRONTEND-CLEANUP).

// "2m ago" / "3h ago" / "4d ago" / "in 30s" — Δ from now in the operator's
// browser locale. Empty string for missing/invalid inputs so consumers can
// render their own placeholder (some panels want "" — no time = no event;
// others want "never checked" — render-time fallback at the call site).
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const deltaMs = d.getTime() - Date.now();
  const absSec = Math.abs(deltaMs) / 1000;
  if (absSec < 60) {
    const n = Math.round(absSec);
    return deltaMs < 0 ? `${n}s ago` : `in ${n}s`;
  }
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

// Absolute timestamp formatted in the operator's browser locale.
// "—" for missing inputs; raw value passes through for un-parseable
// strings so the operator at least sees the bad input.
//
// KR-FE-OPS-QUALITY-PASS: appends "(local)" hint so an operator
// switching between machines / timezones isn't momentarily confused
// about which TZ the rendered time is in. The hover tooltip (via
// timestampAbsoluteUtc) gives the unambiguous UTC ISO for forensic
// correlation against logs / substrate.
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toLocaleString()} (local)`;
}

// Companion to formatTimestamp — the unambiguous UTC ISO for the
// timestamp hover tooltip. Renders the original ISO when valid; falls
// back to the raw value or "—" so the title/aria-label always has a
// stringable value. Use on the SAME element as formatTimestamp via
// title= or aria-label= so operator hover surfaces the absolute form.
export function timestampAbsoluteUtc(
  iso: string | null | undefined,
): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // Normalize to Z-suffixed UTC ISO regardless of input form so the
  // hover always reads as "2026-05-23T17:48:42Z" — the canonical
  // shape operator workflows grep for.
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

// "142 ms" / "2.40 s" / "—" — formats a duration in milliseconds.
// Replaces the formatDuration helpers in AgentActivityPanel + ReasoningPanel
// (identical logic, identical thresholds). Null/undefined → "—" so panels
// don't need to wrap missing-duration paths.
export function formatLatency(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}
