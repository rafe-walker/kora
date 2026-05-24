// KR-FE-COCKPIT-NAV-RESTRUCTURE — per-group collapse state with
// localStorage persistence. Operator's collapse choices survive a
// page refresh + carry across nav clicks (collapsing a group then
// opening a page in another group doesn't lose state).
//
// Storage shape: `{[groupKey]: "collapsed" | "expanded"}` stored
// as a single JSON blob under one localStorage key. The blob is
// best-effort: a corrupt / parse-failing value silently resets to
// "no overrides" rather than blocking sidebar render.
//
// Operator-tunable: when the user hasn't expressed a preference
// for a group, the group's ``defaultCollapsed`` declaration wins.
// Once the operator toggles it once, the override persists forever
// (until they toggle back). Reading the override map is O(1) per
// group — the whole map lives in memory after one parse.

import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "kora.sidebar.groupCollapse.v1";

type CollapseState = "collapsed" | "expanded";

function readStored(): Record<string, CollapseState> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const out: Record<string, CollapseState> = {};
      for (const [k, v] of Object.entries(parsed)) {
        if (v === "collapsed" || v === "expanded") {
          out[k] = v;
        }
      }
      return out;
    }
  } catch {
    // Corrupt JSON — silently reset.
  }
  return {};
}

function writeStored(state: Record<string, CollapseState>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Quota exceeded / disabled storage — best-effort.
  }
}

export interface GroupRef {
  key: string;
  defaultCollapsed: boolean;
}

export interface UseSidebarGroupCollapseResult {
  /** True iff the group is currently collapsed (operator override OR default). */
  isCollapsed: (groupKey: string, defaultCollapsed: boolean) => boolean;
  /** Toggle a single group; persisted to localStorage. */
  toggle: (groupKey: string, defaultCollapsed: boolean) => void;
  /**
   * Snapshot — true iff EVERY group in the supplied list is currently
   * collapsed. Drives the "Collapse all" / "Expand all" shortcut
   * label state (KR-FE-SIDEBAR-MOBILE-COLLAPSE-UX, this bucket).
   */
  allCollapsed: (groups: readonly GroupRef[]) => boolean;
  /** Force every supplied group into the same state; persisted. */
  setAll: (
    groups: readonly GroupRef[],
    nextState: CollapseState,
  ) => void;
}

export function useSidebarGroupCollapse(): UseSidebarGroupCollapseResult {
  const [overrides, setOverrides] = useState<Record<string, CollapseState>>(
    () => readStored(),
  );

  // Re-read on mount in case storage was mutated by another tab
  // (rare for sidebar collapse, but cheap to handle).
  useEffect(() => {
    setOverrides(readStored());
  }, []);

  const isCollapsed = useCallback(
    (groupKey: string, defaultCollapsed: boolean) => {
      const override = overrides[groupKey];
      if (override === "collapsed") return true;
      if (override === "expanded") return false;
      return defaultCollapsed;
    },
    [overrides],
  );

  const toggle = useCallback(
    (groupKey: string, defaultCollapsed: boolean) => {
      setOverrides((prev) => {
        const currentlyCollapsed =
          prev[groupKey] === "collapsed" ||
          (prev[groupKey] === undefined && defaultCollapsed);
        const next: Record<string, CollapseState> = {
          ...prev,
          [groupKey]: currentlyCollapsed ? "expanded" : "collapsed",
        };
        writeStored(next);
        return next;
      });
    },
    [],
  );

  const allCollapsed = useCallback(
    (groups: readonly GroupRef[]) => {
      if (groups.length === 0) return false;
      return groups.every((g) => {
        const override = overrides[g.key];
        if (override === "collapsed") return true;
        if (override === "expanded") return false;
        return g.defaultCollapsed;
      });
    },
    [overrides],
  );

  const setAll = useCallback(
    (groups: readonly GroupRef[], nextState: CollapseState) => {
      setOverrides((prev) => {
        const next = { ...prev };
        for (const g of groups) {
          next[g.key] = nextState;
        }
        writeStored(next);
        return next;
      });
    },
    [],
  );

  return { isCollapsed, toggle, allCollapsed, setAll };
}
