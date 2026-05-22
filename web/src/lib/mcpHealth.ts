// Helpers for the MCP-clients panel's health rendering
// (KR-MCP-CLIENTS-HEALTH-DISPLAY). Kept as pure functions in a
// separate module so the thresholds + label rules can be reviewed
// and source-pinned independently of the React render tree.

// 2x the default KORA_MCP_HEALTH_CHECK_INTERVAL_SEC (300s) cadence.
// Custom cadences via env var are not surfaced client-side per the
// bucket spec — operator inspects via the dashboard health rollup
// if their cadence is non-default.
export const STALE_CHECK_THRESHOLD_MS = 10 * 60 * 1000;

// Spec §2(a): collapsed-view error message truncated to ~80 chars.
export const ERROR_TRUNCATE_LEN = 80;

// "checked 2m ago" / "checked 14h ago" / "never checked".
// Returns the bare relative phrase; the caller prefixes "checked ".
export function formatRelativeCheck(iso: string | null): string {
  if (!iso) return "never checked";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "never checked";
  const deltaMs = d.getTime() - Date.now();
  const absSec = Math.abs(deltaMs) / 1000;
  if (absSec < 60) {
    const n = Math.round(absSec);
    return deltaMs < 0 ? `checked ${n}s ago` : `checked in ${n}s`;
  }
  const absMin = absSec / 60;
  if (absMin < 60) {
    const n = Math.round(absMin);
    return deltaMs < 0 ? `checked ${n}m ago` : `checked in ${n}m`;
  }
  const absHr = absMin / 60;
  if (absHr < 24) {
    const n = Math.round(absHr);
    return deltaMs < 0 ? `checked ${n}h ago` : `checked in ${n}h`;
  }
  const absDay = absHr / 24;
  const n = Math.round(absDay);
  return deltaMs < 0 ? `checked ${n}d ago` : `checked in ${n}d`;
}

// True when last_check_at exists AND is older than 10 min — signals
// the heartbeat scheduler has stopped firing for this endpoint.
// nowMs is injectable so the test-pin can exercise the boundary
// without freezing real time.
export function isStaleCheck(
  iso: string | null,
  nowMs: number = Date.now(),
): boolean {
  if (!iso) return false;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return false;
  return nowMs - d.getTime() > STALE_CHECK_THRESHOLD_MS;
}

// Truncate to `max` chars with a unicode ellipsis. Short strings
// pass through unchanged.
export function truncateError(s: string, max: number = ERROR_TRUNCATE_LEN): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + "…";
}
