// KR-FE-TENANT-PICKER-COCKPIT-CHROME — cockpit-chrome dropdown for
// switching the active tenant view across cost/audit/promotion pages.
//
// Placement: sidebar header section, directly below the "Hermes
// Agent" branding. Always visible when the sidebar is open (lg+:
// always; mobile: when the menu is toggled). Top-bar would also
// work but the sidebar slot is already present in App.tsx and
// avoids a layout-chrome refactor for the first cut.
//
// Single-tenant degradation: renders nothing when the cost-holder
// registry contains < 2 tenants.
//
// Aggregate-view option ("All tenants"): present iff there are ≥ 2
// real tenants. Stored as ALL_TENANTS_SENTINEL.
//
// KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
// keyboard nav + opt-in URL-toggle layered on top of the #207/#208
// chrome. Keyboard contract pinned via TENANT_PICKER_KEYBOARD_SHORTCUTS
// + asserted by the cross-stack drift-guard test.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { ChevronDown, Users } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  ALL_TENANTS_SENTINEL,
  DEFAULT_TENANT_ID,
  OPEN_TENANT_PICKER_EVENT,
  useActiveTenant,
  useTenantUrlToggle,
} from "@/hooks/useActiveTenant";

// KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
// pinned shortcut contract. Drift-guard test asserts this object's
// keys/values match the implementation below (a rename of one side
// without the other silently breaks documented operator behavior).
export const TENANT_PICKER_KEYBOARD_SHORTCUTS = {
  open: ["Enter", " "] as const, // trigger button focused
  navDown: "ArrowDown" as const, // wraps at end
  navUp: "ArrowUp" as const, // wraps at start
  select: "Enter" as const, // confirms highlighted option
  close: "Escape" as const, // returns focus to trigger
  // Letter-jump: any printable single-char key. Cycles through
  // tenants whose id starts with that letter on repeated press.
  letterJump: "<printable-single-char>" as const,
} as const;

export function TenantPicker() {
  const {
    activeTenant,
    setActiveTenant,
    availableTenants,
    isAllTenants,
    isMultiTenant,
    loadingTenants,
    recentTenants,
  } = useActiveTenant();
  const [urlToggle, setUrlToggle] = useTenantUrlToggle();
  const [open, setOpen] = useState(false);

  // KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — Recent section
  // visible only when there are ≥ 3 tenants available (i.e.
  // enough to warrant a fast-access surface; on 2 tenants the
  // flat list is faster to scan than two sections of one). Take
  // up to 3 entries from recentTenants. Currently-active tenant
  // is excluded from Recent — it's already labelled "active" in
  // the All section so showing it twice is noise.
  const recentSection = useMemo<string[]>(() => {
    if (availableTenants.length < 3) return [];
    return recentTenants
      .filter((t) => t !== activeTenant)
      .slice(0, 3);
  }, [recentTenants, availableTenants.length, activeTenant]);

  // Flat option list for keyboard nav. Each entry carries its
  // section so the visible header can render between sections;
  // tenantId duplicates are fine (same id may appear once in
  // Recent and once in All — keyboard nav indexes operate on
  // the flat list so each visible row has a unique position).
  type Option = { kind: "recent" | "all"; tenantId: string };
  const options = useMemo<Option[]>(() => {
    const all: Option[] = availableTenants.map((t) => ({
      kind: "all" as const,
      tenantId: t,
    }));
    all.push({ kind: "all", tenantId: ALL_TENANTS_SENTINEL });
    const recent: Option[] = recentSection.map((t) => ({
      kind: "recent" as const,
      tenantId: t,
    }));
    return [...recent, ...all];
  }, [availableTenants, recentSection]);

  // Index of the highlighted option for keyboard nav. -1 ≡ none
  // highlighted (closed-state default; reset when picker closes).
  // When picker opens we point this at the currently-active tenant
  // so ↑/↓ start from where the operator already is.
  const [highlight, setHighlight] = useState(-1);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // Track repeated-key presses for letter-jump cycling. Reset by a
  // timer so a fresh keystroke after a pause starts from the top.
  const letterCycleRef = useRef<{ letter: string; lastIdx: number } | null>(
    null,
  );

  // KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — listen for
  // open-requests from page-header tenant badges. Lets the badge
  // open the picker without prop-drilling through Layout.
  useEffect(() => {
    const handler = () => setOpen(true);
    window.addEventListener(OPEN_TENANT_PICKER_EVENT, handler);
    return () => window.removeEventListener(OPEN_TENANT_PICKER_EVENT, handler);
  }, []);

  // When picker opens, highlight the currently-active option so
  // ↑/↓ start from there. When picker closes, drop highlight +
  // return focus to the trigger so keyboard flow continues.
  useEffect(() => {
    if (open) {
      // Prefer the All-section instance of the active tenant for
      // the initial highlight — that section is the canonical
      // ordering operators expect. Recent-section duplicate also
      // matches activeTenant but never selects (currently-active
      // is filtered out of recentSection upstream).
      const activeIdx = options.findIndex(
        (o) => o.kind === "all" && o.tenantId === activeTenant,
      );
      setHighlight(activeIdx >= 0 ? activeIdx : 0);
    } else {
      setHighlight(-1);
      // Defer focus restoration to the next tick — Enter/Esc may
      // have just landed on the option button; refocusing the
      // trigger synchronously fights React's commit ordering.
      const t = window.setTimeout(() => triggerRef.current?.focus(), 0);
      return () => window.clearTimeout(t);
    }
  }, [open, activeTenant, options]);

  // Scroll the highlighted option into view on highlight change
  // (large tenant lists may exceed the dropdown's max-h).
  useEffect(() => {
    if (highlight < 0) return;
    const el = optionRefs.current[highlight];
    el?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  const closePicker = useCallback(() => setOpen(false), []);

  const onPick = useCallback(
    (next: string) => {
      setActiveTenant(next);
      closePicker();
    },
    [setActiveTenant, closePicker],
  );

  const onTriggerKeyDown = useCallback(
    (e: KeyboardEvent<HTMLButtonElement>) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setOpen(true);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setOpen(true);
      }
    },
    [],
  );

  const cycleLetterJump = useCallback(
    (letter: string) => {
      const lower = letter.toLowerCase();
      const matches: number[] = [];
      options.forEach((opt, idx) => {
        const label =
          opt.tenantId === ALL_TENANTS_SENTINEL
            ? "all"
            : opt.tenantId.toLowerCase();
        if (label.startsWith(lower)) matches.push(idx);
      });
      if (matches.length === 0) return;
      const prev = letterCycleRef.current;
      let nextIdx: number;
      if (prev && prev.letter === lower) {
        // Repeated press on the same letter → cycle to the next
        // match (wrap at end). Resolves the dup-prefix case the
        // bucket called out in §4 with the preferred "cycle"
        // behavior.
        const currentPos = matches.indexOf(prev.lastIdx);
        const nextPos = currentPos < 0 ? 0 : (currentPos + 1) % matches.length;
        nextIdx = matches[nextPos];
      } else {
        nextIdx = matches[0];
      }
      letterCycleRef.current = { letter: lower, lastIdx: nextIdx };
      setHighlight(nextIdx);
    },
    [options],
  );

  const onListKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closePicker();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlight((idx) => (idx + 1) % options.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight((idx) =>
          idx <= 0 ? options.length - 1 : idx - 1,
        );
        return;
      }
      if (e.key === "Home") {
        e.preventDefault();
        setHighlight(0);
        return;
      }
      if (e.key === "End") {
        e.preventDefault();
        setHighlight(options.length - 1);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (highlight >= 0 && highlight < options.length) {
          onPick(options[highlight].tenantId);
        }
        return;
      }
      // Letter-jump: any single printable character (length 1, no
      // modifier keys). Modifier check avoids hijacking Cmd-A etc.
      if (
        e.key.length === 1 &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        /[\w]/.test(e.key)
      ) {
        e.preventDefault();
        cycleLetterJump(e.key);
      }
    },
    [closePicker, highlight, onPick, options, cycleLetterJump],
  );

  // A.6: only render when we have observed ≥ 2 tenants. Hides
  // entirely for the single-tenant case (no chrome clutter).
  if (loadingTenants) return null;
  if (!isMultiTenant) return null;

  const label = isAllTenants ? "All tenants" : activeTenant;

  return (
    <div className="relative px-4 pb-2">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onTriggerKeyDown}
        aria-haspopup="listbox"
        aria-expanded={open}
        // KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — clarify the
        // listbox affordance in the accessible name so screen
        // readers announce both the current value and the
        // interaction model on focus.
        aria-label={`Active tenant: ${label}. Press Enter to choose a different tenant.`}
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
          // Combined ref callback: store + auto-focus. Auto-focus
          // is what makes ↑/↓/Enter/letter-jump work without a
          // second click after open.
          ref={(node) => {
            listRef.current = node;
            node?.focus();
          }}
          role="listbox"
          aria-label="Select active tenant"
          aria-activedescendant={
            highlight >= 0 ? `tenant-option-${highlight}` : undefined
          }
          tabIndex={-1}
          onKeyDown={onListKeyDown}
          className={cn(
            "absolute left-4 right-4 z-50 mt-1",
            "rounded border border-current/20 bg-popover shadow-lg",
            "max-h-64 overflow-auto",
            "focus-visible:outline-none",
          )}
        >
          {options.map((opt, idx) => {
            // Render a section header above the first "all" option
            // when a Recent section is present (recentSection.length > 0).
            // The header is decorative (role="presentation") so it
            // doesn't get counted as a listbox option.
            const showAllHeader =
              recentSection.length > 0 &&
              opt.kind === "all" &&
              (idx === 0 || options[idx - 1].kind !== "all");
            const showRecentHeader =
              recentSection.length > 0 && opt.kind === "recent" && idx === 0;
            return (
              <div key={`${opt.kind}:${opt.tenantId}`}>
                {showRecentHeader && <PickerSectionHeader label="Recent" />}
                {showAllHeader && <PickerSectionHeader label="All tenants" />}
                <TenantOption
                  id={`tenant-option-${idx}`}
                  tenantId={opt.tenantId}
                  active={
                    opt.kind === "all" && opt.tenantId === activeTenant
                  }
                  highlighted={idx === highlight}
                  onPick={onPick}
                  onHover={() => setHighlight(idx)}
                  optionRef={(node) => {
                    optionRefs.current[idx] = node;
                  }}
                />
              </div>
            );
          })}

          {/* KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
              opt-in URL-toggle. Persisted via useTenantUrlToggle;
              default off. Keeps the picker-vs-URL precedence from
              #207 intact for everyone who doesn't opt in. */}
          <div className="border-t border-current/10 px-3 py-2">
            <label
              className={cn(
                "flex items-center gap-2 cursor-pointer",
                "text-[10px] uppercase tracking-wide",
                "text-muted-foreground hover:text-foreground",
              )}
              title="When on, picker selections also update the ?tenant URL param"
            >
              <input
                type="checkbox"
                checked={urlToggle}
                onChange={(e) => setUrlToggle(e.target.checked)}
                onKeyDown={(e) => {
                  // Keep Esc/Tab functional from the checkbox; let
                  // Space toggle natively (don't preventDefault).
                  if (e.key === "Escape") {
                    e.preventDefault();
                    closePicker();
                  }
                }}
                className="h-3 w-3"
              />
              <span>Also update URL</span>
            </label>
          </div>
        </div>
      )}
    </div>
  );
}

interface TenantOptionProps {
  id: string;
  tenantId: string;
  active: boolean;
  highlighted: boolean;
  onPick: (id: string) => void;
  onHover: () => void;
  optionRef: (node: HTMLButtonElement | null) => void;
}

// KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — decorative section
// header rendered between Recent + All sections. Role
// "presentation" keeps screen readers from counting it as a
// listbox option (the listbox aria-activedescendant pattern relies
// on each role="option" having a stable index).
function PickerSectionHeader({ label }: { label: string }) {
  return (
    <div
      role="presentation"
      className={cn(
        "px-3 pt-2 pb-0.5",
        "text-[9px] uppercase tracking-wider text-muted-foreground",
        "border-t border-current/10 first:border-t-0",
      )}
    >
      {label}
    </div>
  );
}

function TenantOption({
  id,
  tenantId,
  active,
  highlighted,
  onPick,
  onHover,
  optionRef,
}: TenantOptionProps) {
  const display =
    tenantId === ALL_TENANTS_SENTINEL
      ? "All tenants (aggregate)"
      : tenantId === DEFAULT_TENANT_ID
        ? `${tenantId} (canonical)`
        : tenantId;
  return (
    <button
      ref={optionRef}
      id={id}
      type="button"
      role="option"
      aria-selected={active}
      // Mouse pick — keyboard pick goes through onListKeyDown.
      onClick={() => onPick(tenantId)}
      onMouseEnter={onHover}
      // Keep the listbox focused on hover so a keyboard nav after
      // a stray mouse-over still picks up the right element.
      tabIndex={-1}
      className={cn(
        "block w-full px-3 py-1.5 text-left text-xs font-mono",
        "hover:bg-accent/40",
        // Highlighted state is the keyboard-driven "where would
        // Enter land" indicator. Distinct from active (selected).
        highlighted && "bg-accent/60",
        active && "text-accent-foreground font-semibold",
      )}
    >
      {display}
    </button>
  );
}
