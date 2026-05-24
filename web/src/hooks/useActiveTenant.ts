// KR-FE-TENANT-PICKER-COCKPIT-CHROME — single source-of-truth for
// the operator's currently-selected tenant view across the cockpit.
//
// Resolution order:
//   1. URL ``?tenant=<id>`` query param (deep-link wins so a shared
//      link always renders the linked tenant regardless of operator
//      localStorage state).
//   2. localStorage ``kora_active_tenant`` (per-browser persistence).
//   3. Default → ``"default"`` (canonical DEFAULT_TENANT_ID — pre-#202
//      single-tenant behavior preserved exactly when the operator has
//      not picked).
//
// Setting:
//   * setActiveTenant writes localStorage AND emits a synthetic
//     ``storage`` event so other tabs / cockpit subtrees re-render.
//   * If a URL ?tenant= is set, manual picks update localStorage but
//     do NOT mutate the URL (the operator picked from the picker; the
//     deep-link continues to anchor the page).
//
// Available tenants are fetched from ``/api/tenants/list`` once on
// mount + on tab focus (the cost-holder registry mutates lazily; a
// new tenant emerging mid-session shows up on focus without forcing
// a full reload). Failure → ``["default"]`` so the picker still
// renders the single canonical option.
//
// Drift-guard pins:
//   * TENANT_PICKER_STORAGE_KEY — localStorage key, asserted by the
//     test_tenant_picker_drift_guards.ts unit test.
//   * TENANT_ID_QUERY_PARAM — URL query param, asserted to match the
//     BE-side TENANT_ID_QUERY_PARAM_NAME via the cross-stack
//     test_tenants_endpoint contract.
//   * ALL_TENANTS_SENTINEL — pseudo-id for the aggregate-view option.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";

export const TENANT_PICKER_STORAGE_KEY = "kora_active_tenant" as const;
export const TENANT_ID_QUERY_PARAM = "tenant" as const;
export const DEFAULT_TENANT_ID = "default" as const;
export const ALL_TENANTS_SENTINEL = "__all__" as const;

// KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — operator-friendly
// alias accepted in URL deep-links: ``?tenant=all`` resolves to the
// ALL_TENANTS_SENTINEL pseudo-id. Keeps shareable URLs readable
// while preserving a stable internal sentinel that won't collide
// with a real tenant_id named "all". The internal sentinel is also
// honored verbatim for completeness.
export const ALL_TENANTS_URL_ALIAS = "all" as const;

interface TenantsListResponse {
  tenants: string[];
}

function readStored(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(TENANT_PICKER_STORAGE_KEY);
    return raw && raw.trim() ? raw : null;
  } catch {
    return null;
  }
}

function writeStored(value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(TENANT_PICKER_STORAGE_KEY, value);
    // Cross-tab + cross-subtree notification. ``storage`` events
    // don't fire in the originating tab — emit a custom event the
    // hook listens to for in-tab updates.
    window.dispatchEvent(new Event("kora:active-tenant-changed"));
  } catch {
    // Quota exceeded / disabled storage — best-effort. The picker
    // still updates this tab via React state.
  }
}

export interface UseActiveTenantResult {
  /** "default", another tenant_id, or ALL_TENANTS_SENTINEL for aggregate view. */
  activeTenant: string;
  setActiveTenant: (next: string) => void;
  /** Sorted list from /api/tenants/list — default-first. */
  availableTenants: string[];
  /** True when the operator picked the All-tenants pseudo-option. */
  isAllTenants: boolean;
  /** True when at least 2 real tenants exist — picker visibility gate. */
  isMultiTenant: boolean;
  /** True until the initial /api/tenants/list resolves. */
  loadingTenants: boolean;
}

/**
 * Read the active tenant from URL → localStorage → default. Subscribes
 * to localStorage updates (cross-tab + in-tab).
 */
function useResolvedTenant(): [string, (next: string) => void] {
  const location = useLocation();
  const urlTenant = useMemo(() => {
    const params = new URLSearchParams(location.search);
    const v = params.get(TENANT_ID_QUERY_PARAM);
    if (!v || !v.trim()) return null;
    // KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — accept the
    // operator-friendly ``?tenant=all`` alias and resolve to the
    // canonical sentinel. The internal sentinel is also honored
    // verbatim (round-trip safe).
    if (v === ALL_TENANTS_URL_ALIAS) return ALL_TENANTS_SENTINEL;
    return v;
  }, [location.search]);

  const [stored, setStored] = useState<string | null>(() => readStored());

  useEffect(() => {
    const reread = () => setStored(readStored());
    window.addEventListener("storage", reread);
    window.addEventListener("kora:active-tenant-changed", reread);
    return () => {
      window.removeEventListener("storage", reread);
      window.removeEventListener("kora:active-tenant-changed", reread);
    };
  }, []);

  const resolved = urlTenant ?? stored ?? DEFAULT_TENANT_ID;

  const setActiveTenant = useCallback((next: string) => {
    writeStored(next);
    setStored(next);
  }, []);

  return [resolved, setActiveTenant];
}

/**
 * KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — build the
 * URL-form value for an active tenant. Operator-readable for the
 * aggregate sentinel (``all`` rather than ``__all__``); literal for
 * real tenant_ids. Used by the share-URL action so links round-trip
 * cleanly via the URL alias path in useResolvedTenant.
 */
export function tenantToUrlValue(tenant: string): string {
  return tenant === ALL_TENANTS_SENTINEL ? ALL_TENANTS_URL_ALIAS : tenant;
}

// KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — page-header
// badges fire this event to ask the sidebar's TenantPicker to open
// its dropdown. Decouples the badge (per-page) from the picker
// (sidebar) without prop-drilling through Layout.
export const OPEN_TENANT_PICKER_EVENT = "kora:open-tenant-picker" as const;

export function requestOpenTenantPicker(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(OPEN_TENANT_PICKER_EVENT));
}

export function useActiveTenant(): UseActiveTenantResult {
  const [activeTenant, setActiveTenant] = useResolvedTenant();
  const [availableTenants, setAvailableTenants] = useState<string[]>([
    DEFAULT_TENANT_ID,
  ]);
  const [loadingTenants, setLoadingTenants] = useState(true);

  const fetchTenants = useCallback(async () => {
    try {
      const resp = await fetch("/api/tenants/list", {
        credentials: "same-origin",
      });
      if (!resp.ok) {
        setAvailableTenants([DEFAULT_TENANT_ID]);
        return;
      }
      const data = (await resp.json()) as TenantsListResponse;
      if (Array.isArray(data.tenants) && data.tenants.length > 0) {
        setAvailableTenants(data.tenants);
      } else {
        setAvailableTenants([DEFAULT_TENANT_ID]);
      }
    } catch {
      setAvailableTenants([DEFAULT_TENANT_ID]);
    } finally {
      setLoadingTenants(false);
    }
  }, []);

  useEffect(() => {
    void fetchTenants();
    const onFocus = () => void fetchTenants();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [fetchTenants]);

  return {
    activeTenant,
    setActiveTenant,
    availableTenants,
    isAllTenants: activeTenant === ALL_TENANTS_SENTINEL,
    // A.6 — single-tenant degradation: hide picker when only the
    // default tenant has ever been observed.
    isMultiTenant: availableTenants.length >= 2,
    loadingTenants,
  };
}
