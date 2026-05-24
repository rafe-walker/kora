// KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — visually-hidden
// aria-live region that announces active-tenant changes to
// assistive tech (VoiceOver / NVDA / Narrator). Single instance,
// mounted in the App shell.
//
// Why polite (not assertive): tenant switching is operator-
// initiated UI navigation, not an emergency. Polite lets the
// screen reader finish what it's reading before announcing,
// which matches every other navigation event.
//
// First-mount suppression: announcing "Active tenant is default"
// on every page load is noise. Only fire when the tenant actually
// changes after first observation.
//
// Skips firing on single-tenant deployments — there's no value to
// announce since there's nothing to switch to.

import { useEffect, useRef, useState } from "react";
import {
  ALL_TENANTS_SENTINEL,
  useActiveTenant,
} from "@/hooks/useActiveTenant";

export const TENANT_ANNOUNCER_LIVE_REGION_ROLE = "status" as const;
export const TENANT_ANNOUNCER_ARIA_LIVE = "polite" as const;

function describeTenant(tenant: string): string {
  if (tenant === ALL_TENANTS_SENTINEL) return "all tenants";
  return tenant;
}

export function TenantChangeAnnouncer() {
  const { activeTenant, isMultiTenant } = useActiveTenant();
  const [message, setMessage] = useState("");
  // Track the last announced tenant. ``null`` ≡ first render;
  // updating only on actual change avoids the noisy "default" /
  // "default" / "default" repeats that happen as availableTenants
  // resolves.
  const lastRef = useRef<string | null>(null);

  useEffect(() => {
    // Hide announcements on single-tenant deployments (mirrors
    // the picker auto-hide behavior — no tenant to switch to
    // means no announcement worth making).
    if (!isMultiTenant) {
      lastRef.current = activeTenant;
      return;
    }
    // Suppress the first observation post-multi-tenant becoming
    // true; only announce on subsequent changes.
    if (lastRef.current === null) {
      lastRef.current = activeTenant;
      return;
    }
    if (lastRef.current === activeTenant) return;
    lastRef.current = activeTenant;
    setMessage(`Active tenant changed to ${describeTenant(activeTenant)}`);
  }, [activeTenant, isMultiTenant]);

  return (
    <div
      role={TENANT_ANNOUNCER_LIVE_REGION_ROLE}
      aria-live={TENANT_ANNOUNCER_ARIA_LIVE}
      aria-atomic="true"
      // Visually-hidden but reachable by assistive tech. Avoids
      // ``display: none`` which removes the region from the
      // accessibility tree entirely.
      className="sr-only"
    >
      {message}
    </div>
  );
}
