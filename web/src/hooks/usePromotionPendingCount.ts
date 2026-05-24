// KR-FE-PROMOTION-REVIEW-PANEL — sidebar PendingBadge data source.
//
// Polls /api/promotions/phrasebook/pending every 60s and returns the
// count of proposals (BE returns pending-only today). Returns null
// while loading or on persistent failure — the SidebarNavLink skips
// the chip rather than rendering "?".
//
// 60s cadence is the same pattern the rest of the cockpit uses for
// no-WebSocket polling (cost-state, kora-actions). The endpoint is
// fast (file-backed JSON read of pending/ directory).

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

const POLL_INTERVAL_MS = 60_000;

export function usePromotionPendingCount(): number | null {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll(): Promise<void> {
      try {
        const resp = await api.getPhrasebookPromotionProposals();
        if (cancelled) return;
        setCount(resp.proposals.length);
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
