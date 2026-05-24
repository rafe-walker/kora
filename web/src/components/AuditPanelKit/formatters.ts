// Audit-panel timestamp + size formatters. Extracted from
// EmailIntentLogPage (PR #180) + OutboundEmailLogPage (PR #183)
// where these were verbatim copies. New audit-stream panels
// (KR-FE-AUTOFIX-LOG-PANEL, KR-FE-KORA-ACTIONS-AGGREGATED-PANEL,
// and future buckets) should import from here instead of
// re-implementing.
//
// Behavior contract:
//   - formatTimestamp returns Date.toLocaleString output;
//     callers use it for hover-tooltips / detail rows.
//   - formatRelative returns a coarse "Ns/m/h/d ago" string;
//     callers use it for the visible per-row timestamp.
//   - truncate appends "…" past the limit (n-1 visible chars).
//   - formatBytes uses binary scale (1024) since audit sizes
//     are real-world payload bytes.
//   - formatChars uses decimal scale (1000) since "chars" reads
//     more naturally to operators at K-scale.

export function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function formatRelative(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    return `${Math.floor(hr / 24)}d ago`;
  } catch {
    return iso;
  }
}

export function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

// Binary scale for real-world bytes (KB / MB).
export function formatBytes(n: number): string {
  if (n === 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Decimal scale for character counts ("1.8k chars" reads more
// naturally than "1.7K chars" at audit-message size).
export function formatChars(n: number): string {
  if (n < 1000) return `${n} chars`;
  return `${(n / 1000).toFixed(1)}k chars`;
}

// Duration: ms → "Nms" / "N.Ns" / "N.Nmin" — used by autofix +
// kora-actions panels for executor duration rendering.
export function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}min`;
}
