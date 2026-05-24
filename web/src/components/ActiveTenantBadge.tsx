// KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — page-header
// chip that surfaces the active tenant on tenant-scoped panels
// (audit pages, promotion review, cost views). Click opens the
// sidebar TenantPicker; share button copies a deep-link URL with
// the current ?tenant= populated.
//
// Visibility rule (B.2): hidden on single-tenant deployments. The
// canonical "default" tenant + no other observed tenants means
// there's nothing to switch to + nothing worth labeling.
//
// Why a page-header badge rather than relying on the sidebar
// chrome alone: audit pages get deep-linked + shared. Operators
// arriving at /probe-investigations?tenant=marvin need an
// at-a-glance confirmation in the panel header that they're
// viewing marvin's data, not their own. Sidebar chrome may be
// hidden on mobile when the menu is collapsed.

import { useCallback, useState } from "react";
import { Check, Copy, Users } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  ALL_TENANTS_SENTINEL,
  DEFAULT_TENANT_ID,
  TENANT_ID_QUERY_PARAM,
  requestOpenTenantPicker,
  tenantToUrlValue,
  useActiveTenant,
} from "@/hooks/useActiveTenant";

export function ActiveTenantBadge() {
  const { activeTenant, isAllTenants, isMultiTenant, loadingTenants } =
    useActiveTenant();
  const [copied, setCopied] = useState(false);

  // B.2: hide on single-tenant + while the tenant list is loading
  // (avoid a flash of nothing then a flash of badge).
  if (loadingTenants) return null;
  if (!isMultiTenant) return null;

  const label = isAllTenants ? "All tenants" : activeTenant;

  const onShare = useCallback(async () => {
    // Build a URL with the canonical ?tenant= populated so the
    // recipient lands on the same tenant view regardless of their
    // own localStorage state. Anything else on the current URL
    // (filters / focus / window) is preserved verbatim.
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.set(
      TENANT_ID_QUERY_PARAM,
      tenantToUrlValue(activeTenant),
    );
    const shareUrl = url.toString();

    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(shareUrl);
        ok = true;
      }
    } catch {
      // Permissions blocked / non-secure context — fall through to
      // the prompt() fallback so the operator can still copy.
    }
    if (!ok) {
      // Non-blocking fallback that works in every browser regardless
      // of clipboard permissions / secure-context state.
      window.prompt("Copy this URL:", shareUrl);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }, [activeTenant]);

  return (
    <div className="inline-flex items-center gap-1">
      <button
        type="button"
        onClick={requestOpenTenantPicker}
        aria-label={`Active tenant: ${label}. Click to change.`}
        title="Click to open the tenant picker"
        // KR-FE-A11Y-COMPLETION-FORCED-COLORS-AND-AXE-CORE-CI —
        // data attribute hook for forced-colors. The border-
        // current/20 + bg-card/60 chip styling vanishes under
        // high-contrast; the CSS rule promotes data-tenant-chip
        // to a CanvasText border so the affordance survives.
        data-tenant-chip
        className={cn(
          "inline-flex items-center gap-1.5",
          "rounded border border-current/20 bg-card/60 px-2 py-0.5",
          "text-[10px] uppercase tracking-wide text-muted-foreground",
          "hover:bg-card/80 focus-visible:outline-none",
          "focus-visible:ring-1 focus-visible:ring-midground/40",
        )}
      >
        <Users className="h-3 w-3 shrink-0" />
        <span>Viewing</span>
        <span
          className={cn(
            "font-mono text-foreground normal-case tracking-normal",
            activeTenant === DEFAULT_TENANT_ID && "text-muted-foreground",
          )}
        >
          {label}
        </span>
      </button>
      <button
        type="button"
        onClick={() => void onShare()}
        aria-label="Copy a shareable URL with the current tenant filter"
        title="Copy share URL"
        data-tenant-chip
        className={cn(
          "inline-flex items-center justify-center",
          "rounded border border-current/20 bg-card/60 p-1",
          "text-muted-foreground hover:bg-card/80 hover:text-foreground",
          "focus-visible:outline-none focus-visible:ring-1",
          "focus-visible:ring-midground/40",
        )}
      >
        {copied ? (
          <Check className="h-3 w-3 text-success" />
        ) : (
          <Copy className="h-3 w-3" />
        )}
      </button>
    </div>
  );
}

// Drift-guard reference — keep this exported sentinel grepable so
// the cross-stack test in tests/test_tenants_endpoint.py can pin
// that the badge component participates in the same constant set.
export const ACTIVE_TENANT_BADGE_USES_SENTINEL = ALL_TENANTS_SENTINEL;
