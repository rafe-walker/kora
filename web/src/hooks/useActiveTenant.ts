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

// KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — operator's most-
// recent picks (most-recent-first), capped at 5 entries. Drives
// the TenantPicker's "Recent" section that surfaces fast-access
// switching for operators who toggle between 2-3 tenants. Empty
// on single-tenant deployments. Updated on every setActiveTenant
// call (including aggregate sentinel and "default").
export const RECENT_TENANTS_STORAGE_KEY = "kora_recent_tenants" as const;
export const RECENT_TENANTS_CAP = 5 as const;

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

// KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — recent-tenants
// localStorage read. Returns a sanitized list: only string
// entries, deduped, capped. Corrupt JSON / wrong shape silently
// resets to []; the picker then renders without the Recent
// section. A subsequent setActiveTenant call rebuilds the list
// cleanly.
function readRecent(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_TENANTS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const out: string[] = [];
    const seen = new Set<string>();
    for (const entry of parsed) {
      if (typeof entry !== "string") continue;
      if (!entry.trim()) continue;
      if (seen.has(entry)) continue;
      seen.add(entry);
      out.push(entry);
      if (out.length >= RECENT_TENANTS_CAP) break;
    }
    return out;
  } catch {
    return [];
  }
}

function writeRecent(next: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      RECENT_TENANTS_STORAGE_KEY,
      JSON.stringify(next),
    );
    window.dispatchEvent(new Event("kora:recent-tenants-changed"));
  } catch {
    // Best-effort.
  }
}

/**
 * Push ``picked`` onto the head of the recent list; dedupe; cap.
 * Pure helper so the test in the picker test-doubles can exercise
 * the order semantics without round-tripping through localStorage.
 */
export function pushRecentTenant(
  current: readonly string[],
  picked: string,
): string[] {
  const next = [picked, ...current.filter((t) => t !== picked)];
  return next.slice(0, RECENT_TENANTS_CAP);
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
  /**
   * Operator's most-recent picks (most-recent-first, deduped, capped
   * at RECENT_TENANTS_CAP). Filtered to entries still present in
   * availableTenants — a corrupt localStorage entry or a
   * since-deleted tenant won't leak into the picker. Includes the
   * ALL_TENANTS_SENTINEL when the operator has picked aggregate.
   */
  recentTenants: string[];
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
    // KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
    // when the operator opted in, also mirror the pick into the URL.
    // Read the toggle live (not via the hook) so this stays usable
    // from callers outside React (e.g., the badge's share-URL flow
    // could trigger this in principle).
    if (readUrlToggle()) {
      updateUrlTenantParam(next);
    }
    // KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — track the recent
    // list. Read fresh from localStorage to avoid stale closure
    // capture (cross-tab activity may have updated it). pushRecentTenant
    // handles dedupe + cap.
    writeRecent(pushRecentTenant(readRecent(), next));
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

// KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
// opt-in "also update URL when picking a tenant" preference. When
// enabled, picker selections write the ``?tenant=`` query param to
// the current URL (preserving every other param) in addition to
// localStorage. Useful for power-operators who want their browser
// history / open-tab URLs to reflect the active tenant.
//
// Default is off — preserves the #207 picker-vs-URL precedence
// (URL anchors a deep-link; operator picks update only their own
// localStorage and don't mutate a shared link's URL).
export const TENANT_PICKER_URL_TOGGLE_STORAGE_KEY =
  "kora_tenant_picker_update_url" as const;

function readUrlToggle(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return (
      window.localStorage.getItem(TENANT_PICKER_URL_TOGGLE_STORAGE_KEY) ===
      "1"
    );
  } catch {
    return false;
  }
}

function writeUrlToggle(enabled: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (enabled) {
      window.localStorage.setItem(
        TENANT_PICKER_URL_TOGGLE_STORAGE_KEY,
        "1",
      );
    } else {
      window.localStorage.removeItem(TENANT_PICKER_URL_TOGGLE_STORAGE_KEY);
    }
    window.dispatchEvent(new Event("kora:tenant-url-toggle-changed"));
  } catch {
    // Best-effort.
  }
}

/**
 * KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
 * write ``?tenant=<value>`` to the current URL while preserving
 * every other query param. Uses ``history.replaceState`` so the
 * change doesn't push a new entry onto the back-stack (operators
 * picking through tenants shouldn't pollute history). ``default``
 * removes the param entirely so the URL stays clean.
 */
function updateUrlTenantParam(next: string): void {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    if (next === DEFAULT_TENANT_ID) {
      url.searchParams.delete(TENANT_ID_QUERY_PARAM);
    } else {
      url.searchParams.set(TENANT_ID_QUERY_PARAM, tenantToUrlValue(next));
    }
    window.history.replaceState(
      window.history.state,
      "",
      url.pathname + url.search + url.hash,
    );
  } catch {
    // history API restrictions (rare) — silent. localStorage
    // still updated, so the picker still works as before.
  }
}

/**
 * Read + persist the "also update URL" toggle. Listens for in-tab
 * + cross-tab changes so the picker checkbox stays in sync if the
 * operator changes the setting from another tab.
 */
export function useTenantUrlToggle(): [boolean, (enabled: boolean) => void] {
  const [enabled, setEnabled] = useState(() => readUrlToggle());
  useEffect(() => {
    const reread = () => setEnabled(readUrlToggle());
    window.addEventListener("storage", reread);
    window.addEventListener("kora:tenant-url-toggle-changed", reread);
    return () => {
      window.removeEventListener("storage", reread);
      window.removeEventListener("kora:tenant-url-toggle-changed", reread);
    };
  }, []);
  const set = useCallback((nextEnabled: boolean) => {
    writeUrlToggle(nextEnabled);
    setEnabled(nextEnabled);
  }, []);
  return [enabled, set];
}

export function useActiveTenant(): UseActiveTenantResult {
  const [activeTenant, setActiveTenant] = useResolvedTenant();
  const [availableTenants, setAvailableTenants] = useState<string[]>([
    DEFAULT_TENANT_ID,
  ]);
  const [loadingTenants, setLoadingTenants] = useState(true);
  // KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — recent-tenants
  // state. Live-synced via the same custom-event pattern as the
  // active-tenant store so two picker instances stay coherent.
  const [recentRaw, setRecentRaw] = useState<string[]>(() => readRecent());
  useEffect(() => {
    const reread = () => setRecentRaw(readRecent());
    window.addEventListener("storage", reread);
    window.addEventListener("kora:recent-tenants-changed", reread);
    return () => {
      window.removeEventListener("storage", reread);
      window.removeEventListener("kora:recent-tenants-changed", reread);
    };
  }, []);

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

  // KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — filter recents to
  // entries that still exist in availableTenants (a since-deleted
  // tenant_id in localStorage must not leak into the picker). The
  // ALL_TENANTS_SENTINEL is always valid in multi-tenant mode.
  const recentTenants = useMemo(() => {
    const allowed = new Set<string>(availableTenants);
    allowed.add(ALL_TENANTS_SENTINEL);
    return recentRaw.filter((t) => allowed.has(t));
  }, [recentRaw, availableTenants]);

  return {
    activeTenant,
    setActiveTenant,
    availableTenants,
    isAllTenants: activeTenant === ALL_TENANTS_SENTINEL,
    // A.6 — single-tenant degradation: hide picker when only the
    // default tenant has ever been observed.
    isMultiTenant: availableTenants.length >= 2,
    loadingTenants,
    recentTenants,
  };
}
