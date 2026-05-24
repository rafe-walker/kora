// KR-FE-PROMOTION-REVIEW-PANEL — sidebar PendingBadge data source.
//
// Polls /api/promotions/counts every 60s and returns the total
// pending count across all actionable loops (excludes
// snapshot_expand per the BE's aggregate definition — that loop
// is informational only). Returns null while loading or on
// persistent failure — the SidebarNavLink skips the chip rather
// than rendering "?".
//
// KR-FE-PROMOTION-REVIEW-MULTI-LOOP-EXTEND update: the badge now
// reflects all actionable loops at once (phrasebook + router-
// tuning + tool-trimming + probe-envelopes — and email-intent
// when CC#1's #420 lands). Pre-extension it polled the phrasebook
// /pending endpoint directly; the counts endpoint added in this
// bucket aggregates server-side so we still do one round-trip.

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

const POLL_INTERVAL_MS = 60_000;

export function usePromotionPendingCount(): number | null {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll(): Promise<void> {
      try {
        const resp = await api.getPromotionCounts();
        if (cancelled) return;
        setCount(resp.total_pending);
      } catch {
        // Best-effort: keep the existing count on transient failure
        // (typically a one-off restart) rather than flicker null.
      }
    }

    void poll();
    const id = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return count;
}
