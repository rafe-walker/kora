// KR-FE-TENANT-PICKER-COCKPIT-CHROME — cockpit-chrome dropdown for
// switching the active tenant view across cost/audit/promotion pages.
//
// Placement (per A.1): sidebar header section, directly below the
// "Hermes Agent" branding. Always visible when the sidebar is open
// (lg+: always; mobile: when the menu is toggled). Top-bar would
// also work but the sidebar slot is already present in App.tsx and
// avoids a layout-chrome refactor for the first cut.
//
// Single-tenant degradation (A.6): renders nothing when the cost-
// holder registry contains < 2 tenants. The picker re-appears
// automatically when a second tenant emerges (re-fetched on focus
// by useActiveTenant). Keeps single-tenant operators (Joshua today)
// from seeing UI clutter.
//
// Aggregate-view option ("All tenants"): present iff there are ≥ 2
// real tenants. Stored as ALL_TENANTS_SENTINEL — pages branch on
// useActiveTenant().isAllTenants to render aggregate vs single-
// tenant views.

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, Users } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  ALL_TENANTS_SENTINEL,
  DEFAULT_TENANT_ID,
  OPEN_TENANT_PICKER_EVENT,
  useActiveTenant,
} from "@/hooks/useActiveTenant";

export function TenantPicker() {
  const {
    activeTenant,
    setActiveTenant,
    availableTenants,
    isAllTenants,
    isMultiTenant,
    loadingTenants,
  } = useActiveTenant();
  const [open, setOpen] = useState(false);

  // KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — listen for
  // open-requests from page-header tenant badges. Lets the badge
  // open the picker without prop-drilling through Layout.
  useEffect(() => {
    const handler = () => setOpen(true);
    window.addEventListener(OPEN_TENANT_PICKER_EVENT, handler);
    return () => window.removeEventListener(OPEN_TENANT_PICKER_EVENT, handler);
  }, []);

  const onPick = useCallback(
    (next: string) => {
      setActiveTenant(next);
      setOpen(false);
    },
    [setActiveTenant],
  );

  // A.6: only render when we have observed ≥ 2 tenants. Hides
  // entirely for the single-tenant case (no chrome clutter).
  if (loadingTenants) return null;
  if (!isMultiTenant) return null;

  const label = isAllTenants ? "All tenants" : activeTenant;

  return (
    <div className="relative px-4 pb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Active tenant: ${label}`}
        className={cn(
          "flex w-full items-center justify-between gap-2",
          "rounded border border-current/20 bg-card/60 px-2 py-1.5",
          "text-xs text-midground hover:bg-card/80",
          "focus-visible:outline-none focus-visible:ring-1",
          "focus-visible:ring-midground/40",
        )}
      >
        <span className="flex items-center gap-1.5 min-w-0">
          <Users className="h-3 w-3 shrink-0 text-muted-foreground" />
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Tenant
          </span>
          <span className="font-mono truncate">{label}</span>
        </span>
        <ChevronDown
          className={cn(
            "h-3 w-3 text-muted-foreground transition-transform",
            open && "rotate-180",
          )}
        />
      </button>

      {open && (
        <div
          role="listbox"
          aria-label="Select active tenant"
          className={cn(
            "absolute left-4 right-4 z-50 mt-1",
            "rounded border border-current/20 bg-popover shadow-lg",
            "max-h-64 overflow-auto",
          )}
        >
          {availableTenants.map((t) => (
            <TenantOption
              key={t}
              tenantId={t}
              active={t === activeTenant}
              onPick={onPick}
            />
          ))}
          <TenantOption
            tenantId={ALL_TENANTS_SENTINEL}
            label="All tenants (aggregate)"
            active={isAllTenants}
            onPick={onPick}
          />
        </div>
      )}
    </div>
  );
}

interface TenantOptionProps {
  tenantId: string;
  label?: string;
  active: boolean;
  onPick: (id: string) => void;
}

function TenantOption({ tenantId, label, active, onPick }: TenantOptionProps) {
  const display =
    label ??
    (tenantId === DEFAULT_TENANT_ID ? `${tenantId} (canonical)` : tenantId);
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      onClick={() => onPick(tenantId)}
      className={cn(
        "block w-full px-3 py-1.5 text-left text-xs font-mono",
        "hover:bg-accent/40",
        active && "bg-accent/30 text-accent-foreground font-semibold",
      )}
    >
      {display}
    </button>
  );
}
