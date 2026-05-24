import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";
import { PageHeaderContext } from "./page-header-context";
import { resolvePageTitle } from "@/lib/resolve-page-title";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";
import {
  DEFAULT_TENANT_ID,
  useActiveTenant,
} from "@/hooks/useActiveTenant";

// KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
// browser-tab title suffix. Mirrors the static <title> in
// web/index.html so per-page document.title updates keep the
// "· Kora" / "Hermes Agent" anchor a screen-reader / tab-switcher
// expects. Single point of change if the brand suffix ever moves.
const TAB_TITLE_SUFFIX = "Hermes Agent" as const;

/**
 * Format a per-page browser-tab title:
 *   single-tenant or default-active → ``"Probe Investigations · Hermes Agent"``
 *   tenant-active                   → ``"[marvin] Probe Investigations · Hermes Agent"``
 *   aggregate-active                → ``"[all tenants] Probe Investigations · Hermes Agent"``
 *
 * Exported for the drift-guard test so renaming the brand suffix
 * or changing the prefix format trips the cross-stack pin.
 */
export function formatBrowserTabTitle(
  displayTitle: string,
  options: {
    activeTenant: string;
    isAllTenants: boolean;
    isMultiTenant: boolean;
  },
): string {
  const base = `${displayTitle} · ${TAB_TITLE_SUFFIX}`;
  // Hide prefix on single-tenant deployments (no value — nothing
  // to switch to) and on the canonical "default" tenant (every
  // operator's idle baseline).
  if (!options.isMultiTenant) return base;
  if (options.isAllTenants) return `[all tenants] ${base}`;
  if (options.activeTenant === DEFAULT_TENANT_ID) return base;
  return `[${options.activeTenant}] ${base}`;
}

export function PageHeaderProvider({
  children,
  pluginTabs,
}: {
  children: ReactNode;
  pluginTabs: { path: string; label: string }[];
}) {
  const { pathname } = useLocation();
  const { t } = useI18n();
  const [titleOverride, setTitleOverride] = useState<string | null>(null);
  const [afterTitle, setAfterTitle] = useState<ReactNode>(null);
  const [end, setEnd] = useState<ReactNode>(null);

  // Clear any per-page title / toolbar slots when the path changes. Child routes
  // re-fill these on mount via usePageHeader.
  /* eslint-disable react-hooks/set-state-in-effect */
  useLayoutEffect(() => {
    setTitleOverride(null);
    setAfterTitle(null);
    setEnd(null);
  }, [pathname]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const defaultTitle = useMemo(
    () => resolvePageTitle(pathname, t, pluginTabs),
    [pathname, t, pluginTabs],
  );
  const displayTitle = titleOverride ?? defaultTitle;

  // KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
  // sync document.title to the active page header + active-tenant
  // prefix. Lets operators distinguish ProbeInvestigations-for-
  // Kora vs ProbeInvestigations-for-Marvin tabs at a glance in
  // their browser tab strip. No-op until at least 2 tenants exist
  // OR the active tenant is non-default — keeps the original
  // static title intact for single-tenant operators.
  const { activeTenant, isAllTenants, isMultiTenant } = useActiveTenant();
  useEffect(() => {
    document.title = formatBrowserTabTitle(displayTitle, {
      activeTenant,
      isAllTenants,
      isMultiTenant,
    });
  }, [displayTitle, activeTenant, isAllTenants, isMultiTenant]);

  const isChatRoute = pathname === "/chat" || pathname === "/chat/";
  /** Env jump-nav is wide — stack below title on small screens so KEYS stays readable. */
  const isEnvRoute =
    pathname === "/env" || pathname.startsWith("/env/");

  const value = useMemo(
    () => ({
      setAfterTitle,
      setEnd,
      setTitle: setTitleOverride,
    }),
    [],
  );

  return (
    <PageHeaderContext.Provider value={value}>
      <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col overflow-hidden">
        <header
          className={cn(
            "z-1 w-full shrink-0",
            "box-border border-b border-current/20",
            "bg-background-base/40 backdrop-blur-sm",
            // Mobile stacks title + toolbar — fixed h-14 clips content; desktop stays one row.
            "min-h-0 overflow-x-hidden overflow-y-visible py-3 sm:h-14 sm:min-h-[3.5rem] sm:overflow-hidden sm:py-0",
          )}
          role="banner"
        >
          <div
            className={cn(
              "flex w-full min-w-0 flex-1 gap-3 px-3 sm:h-full sm:gap-3 sm:px-6",
              isChatRoute
                ? "flex-row items-center"
                : "flex-col justify-center sm:flex-row sm:items-center",
            )}
          >
            <div
              className={cn(
                "flex min-w-0 flex-1 gap-2 sm:gap-3",
                afterTitle && isEnvRoute
                  ? "flex-col items-start sm:flex-row sm:items-center"
                  : afterTitle
                    ? "flex-row flex-wrap items-center"
                    : "flex-row items-center",
              )}
            >
              <h1
                className={cn(
                  "font-expanded min-w-0 text-sm font-bold tracking-[0.08em] text-midground",
                  afterTitle && isEnvRoute
                    ? "max-w-full sm:min-w-0 sm:shrink sm:truncate"
                    : afterTitle
                      ? "shrink truncate"
                      : "truncate",
                )}
                style={{ mixBlendMode: "plus-lighter" }}
              >
                {displayTitle}
              </h1>
              {afterTitle ? (
                <div
                  className={cn(
                    "min-w-0 scrollbar-none",
                    isEnvRoute
                      ? "w-full overflow-x-auto sm:flex-1 sm:overflow-x-auto"
                      : "shrink-0 overflow-visible",
                  )}
                >
                  {afterTitle}
                </div>
              ) : null}
            </div>

            {end ? (
              <div
                className={cn(
                  "flex min-w-0 sm:max-w-md sm:flex-1",
                  isChatRoute
                    ? "w-auto shrink-0 justify-end"
                    : "w-full justify-start sm:justify-end",
                )}
              >
                {end}
              </div>
            ) : null}
          </div>
        </header>

        <main
          className={cn(
            "min-h-0 w-full min-w-0 flex-1 flex flex-col",
            // Bottom inset for scrolled pages lives on the route outlet wrapper in
            // `App.tsx` (`w-full min-w-0`) so it pads scrollable content, not flex chrome.
            isChatRoute
              ? "overflow-hidden"
              : "overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable]",
          )}
        >
          {children}
        </main>
      </div>
    </PageHeaderContext.Provider>
  );
}
