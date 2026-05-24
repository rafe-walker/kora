import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import {
  Routes,
  Route,
  NavLink,
  Navigate,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  BellRing,
  BookOpen,
  BookOpenCheck,
  Brain,
  ChevronDown,
  ChevronRight,
  Clock,
  Code,
  Cpu,
  Database,
  DollarSign,
  Download,
  Eye,
  FileText,
  Globe,
  Heart,
  HeartPulse,
  Inbox,
  KeyRound,
  Lightbulb,
  Mail,
  LayoutDashboard,
  Menu,
  MessageCircle,
  MessageSquare,
  OctagonAlert,
  Package,
  Cable,
  Plug,
  PowerSquare,
  Puzzle,
  Radio,
  RotateCw,
  Scroll,
  Send,
  Settings,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Star,
  Terminal,
  UserCircle,
  Users,
  Waves,
  Workflow,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { ListItem } from "@nous-research/ui/ui/components/list-item";
import { SelectionSwitcher } from "@nous-research/ui/ui/components/selection-switcher";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Typography } from "@/components/NouiTypography";
import { cn } from "@/lib/utils";
import { Backdrop } from "@/components/Backdrop";
import { SidebarFooter } from "@/components/SidebarFooter";
import { SidebarStatusStrip } from "@/components/SidebarStatusStrip";
import { PageHeaderProvider } from "@/contexts/PageHeaderProvider";
import { useSystemActions } from "@/contexts/useSystemActions";
import type { SystemAction } from "@/contexts/system-actions-context";
import ConfigPage from "@/pages/ConfigPage";
import DocsPage from "@/pages/DocsPage";
import EnvPage from "@/pages/EnvPage";
import SessionsPage from "@/pages/SessionsPage";
import LogsPage from "@/pages/LogsPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import ModelsPage from "@/pages/ModelsPage";
import CronPage from "@/pages/CronPage";
import MCPPage from "@/pages/MCPPage";
import IdentityPage from "@/pages/IdentityPage";
import OperationalStatePage from "@/pages/OperationalStatePage";
import HealthRollupPage from "@/pages/HealthRollupPage";
import HeartbeatPanel from "@/pages/HeartbeatPanel";
import MCPClientsPanel from "@/pages/MCPClientsPanel";
import WebhookEventsPanel from "@/pages/WebhookEventsPanel";
import AgentActivityPanel from "@/pages/AgentActivityPanel";
import SlackDMPanel from "@/pages/SlackDMPanel";
import EmailPanel from "@/pages/EmailPanel";
import EmailIntentLogPage from "@/pages/EmailIntentLogPage";
import OutboundEmailLogPage from "@/pages/OutboundEmailLogPage";
import AutofixLogPage from "@/pages/AutofixLogPage";
import KoraActionsPage from "@/pages/KoraActionsPage";
import ReasoningPanel from "@/pages/ReasoningPanel";
import AlertsPanel from "@/pages/AlertsPanel";
import BootStatusPage from "@/pages/BootStatusPage";
import DRStatePage from "@/pages/DRStatePage";
import CostStatePage from "@/pages/CostStatePage";
import CostTelemetryPage from "@/pages/CostTelemetryPage";
import PhrasebookPage from "@/pages/PhrasebookPage";
import PromotionReviewPage from "@/pages/PromotionReviewPage";
import EmailLoggedOnlyAnalyzerPage from "@/pages/EmailLoggedOnlyAnalyzerPage";
import InvestigationDrillDownPage from "@/pages/InvestigationDrillDownPage";
import ProbeInvestigationsPage from "@/pages/ProbeInvestigationsPage";
import AlertInvestigationsPage from "@/pages/AlertInvestigationsPage";
import CapabilitiesPage from "@/pages/CapabilitiesPage";
import CharterPage from "@/pages/CharterPage";
import KoraControlPage from "@/pages/KoraControlPage";
import SeaTicketsPage from "@/pages/SeaTicketsPage";
import ChainEventsPage from "@/pages/ChainEventsPage";
import ProfilesPage from "@/pages/ProfilesPage";
import SkillsPage from "@/pages/SkillsPage";
import PluginsPage from "@/pages/PluginsPage";
import ChatPage from "@/pages/ChatPage";
import DashboardPage from "@/pages/DashboardPage";
import RunbooksPage from "@/pages/RunbooksPage";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { ThemeSwitcher } from "@/components/ThemeSwitcher";
import { useI18n } from "@/i18n";
import type { Translations } from "@/i18n/types";
import { PluginPage, PluginSlot, usePlugins } from "@/plugins";
import type { PluginManifest } from "@/plugins";
import { useTheme } from "@/themes";
import { isDashboardEmbeddedChatEnabled } from "@/lib/dashboard-flags";
import { api } from "@/lib/api";
import { usePromotionPendingCount } from "@/hooks/usePromotionPendingCount";
import { useSidebarGroupCollapse } from "@/hooks/useSidebarGroupCollapse";

function UnknownRouteFallback({ pluginsLoading }: { pluginsLoading: boolean }) {
  if (pluginsLoading) {
    // Render nothing during the plugin-load window — a spinner here would just flash.
    return null;
  }
  return <Navigate to="/sessions" replace />;
}

const CHAT_NAV_ITEM: NavItem = {
  path: "/chat",
  labelKey: "chat",
  label: "Chat",
  icon: Terminal,
};

/**
 * Built-in routes except /chat.  Chat is rendered persistently (outside
 * <Routes>) when embedded — see the persistent chat host block rendered
 * inline near the bottom of this file — so the PTY child, WebSocket,
 * and xterm instance survive when the user visits another tab and comes
 * back.  A `display:none` toggle hides the terminal without unmounting.
 * Routing still owns the URL so /chat deep-links, browser back/forward,
 * and nav highlight keep working.
 */
const BUILTIN_ROUTES_CORE: Record<string, ComponentType> = {
  "/": DashboardPage,
  "/sessions": SessionsPage,
  "/operational-state": OperationalStatePage,
  "/health-rollup": HealthRollupPage,
  "/heartbeat": HeartbeatPanel,
  "/mcp-clients": MCPClientsPanel,
  "/webhook-events": WebhookEventsPanel,
  "/agent-activity": AgentActivityPanel,
  "/reasoning": ReasoningPanel,
  "/alerts": AlertsPanel,
  "/slack-dm": SlackDMPanel,
  "/email": EmailPanel,
  "/email-intent-log": EmailIntentLogPage,
  "/outbound-email-log": OutboundEmailLogPage,
  "/probe-autofix-log": AutofixLogPage,
  "/kora-actions": KoraActionsPage,
  "/boot-status": BootStatusPage,
  "/dr-state": DRStatePage,
  "/cost-state": CostStatePage,
  "/cost-telemetry": CostTelemetryPage,
  "/phrasebook": PhrasebookPage,
  "/promotions/phrasebook": PromotionReviewPage,
  "/probe-investigations": ProbeInvestigationsPage,
  "/alert-investigations": AlertInvestigationsPage,
  "/email-intent-log/logged-only": EmailLoggedOnlyAnalyzerPage,
  // KR-FE-INVESTIGATION-DRILL-DOWN — drill into the unified
  // per-caller_session_id timeline. ``:callerSessionId`` is a path
  // segment captured by react-router; the page reads useParams.
  // Deep-linked from KoraActionsPage + ProbeInvestigationsPage
  // rows; no sidebar nav (the page only makes sense reached from
  // a specific row).
  "/investigations/:callerSessionId": InvestigationDrillDownPage,
  "/capabilities": CapabilitiesPage,
  "/charter": CharterPage,
  "/kora-control": KoraControlPage,
  "/sea-tickets": SeaTicketsPage,
  "/chain-events": ChainEventsPage,
  "/analytics": AnalyticsPage,
  "/models": ModelsPage,
  "/logs": LogsPage,
  "/cron": CronPage,
  "/mcp": MCPPage,
  "/skills": SkillsPage,
  "/plugins": PluginsPage,
  "/profiles": ProfilesPage,
  "/identity": IdentityPage,
  "/config": ConfigPage,
  "/env": EnvPage,
  "/docs": DocsPage,
  "/runbooks": RunbooksPage,
};

// Route placeholder for /chat.  The persistent ChatPage host (rendered
// outside <Routes> when embedded chat is on) paints on top; this empty
// element just claims the path so the `*` catch-all redirect doesn't
// fire when the user navigates to /chat.
function ChatRouteSink() {
  return null;
}

// KR-FE-COCKPIT-NAV-RESTRUCTURE — group definition shape. ``key`` is
// the localStorage key for collapse-state persistence (kebab-case,
// stable across renames of ``label``). ``defaultCollapsed`` controls
// initial state when the operator has no stored preference — the
// less-used groups (Daemon / Settings / Diagnostic) default
// collapsed so the sidebar opens to the operator-priority surface.
interface SidebarNavGroupDef {
  key: string;
  label: string;
  defaultCollapsed: boolean;
  items: NavItem[];
}

const BUILTIN_NAV_GROUPS: readonly SidebarNavGroupDef[] = [
  {
    key: "overview",
    label: "Overview",
    defaultCollapsed: false,
    items: [
      // Alerts pinned to the very top of the sidebar (priority
      // position carried over from the pre-restructure layout) —
      // operator sees attention surface first.
      { path: "/alerts", labelKey: "alerts", label: "Alerts", icon: AlertTriangle },
      { path: "/", labelKey: "overview", label: "Overview", icon: LayoutDashboard },
      // KR-FE-KORA-ACTIONS-AGGREGATED-PANEL — apex "what did Kora do"
      // timeline. Lives at the top with operator-attention surfaces.
      { path: "/kora-actions", labelKey: "koraActions", label: "Kora Actions", icon: Activity },
      { path: "/sessions", labelKey: "sessions", label: "Sessions", icon: MessageSquare },
      { path: "/operational-state", labelKey: "operationalState", label: "Operational", icon: Activity },
    ],
  },
  {
    key: "watch",
    label: "Watch",
    defaultCollapsed: false,
    items: [
      { path: "/health-rollup", labelKey: "healthRollup", label: "Health", icon: HeartPulse },
      { path: "/heartbeat", labelKey: "heartbeat", label: "Heartbeat", icon: Heart },
      // KR-FE-PROBE-INVESTIGATION-VIEWER + KR-FE-ALERT-INVESTIGATIONS-VIEWER
      // — the two wake-driven investigation surfaces sit together.
      { path: "/probe-investigations", labelKey: "probeInvestigations", label: "Probe Investigations", icon: Sparkles },
      { path: "/alert-investigations", labelKey: "alertInvestigations", label: "Alert Investigations", icon: BellRing },
      // Cost watching belongs to operator-attention since the
      // cheap-substrate thesis depends on it staying visible.
      { path: "/cost-state", labelKey: "costState", label: "Cost", icon: DollarSign },
      { path: "/cost-telemetry", labelKey: "costTelemetry", label: "Cost Telemetry", icon: BarChart3 },
    ],
  },
  {
    key: "email",
    label: "Email",
    defaultCollapsed: false,
    items: [
      { path: "/email", labelKey: "email", label: "Email", icon: Mail },
      { path: "/email-intent-log", labelKey: "emailIntentLog", label: "Email Intent Log", icon: Inbox },
      // KR-FE-EMAIL-LOGGED-ONLY-ANALYZER — un-acted-on lens.
      { path: "/email-intent-log/logged-only", labelKey: "emailLoggedOnly", label: "Logged-Only", icon: Sparkles },
      { path: "/outbound-email-log", labelKey: "outboundEmailLog", label: "Outbound Email Log", icon: Send },
    ],
  },
  {
    key: "promotion",
    label: "Promotion Loops",
    defaultCollapsed: false,
    items: [
      // KR-FE-PROMOTION-REVIEW-PANEL + multi-loop extend — operator-
      // approval UX. PendingBadge populates from /api/promotions/counts.
      { path: "/promotions/phrasebook", labelKey: "promotionReview", label: "Promotion Review", icon: Lightbulb },
      { path: "/phrasebook", labelKey: "phrasebook", label: "Phrasebook", icon: BookOpen },
    ],
  },
  {
    key: "activity",
    label: "Activity & Reasoning",
    defaultCollapsed: false,
    items: [
      { path: "/reasoning", labelKey: "reasoning", label: "Reasoning", icon: Brain },
      { path: "/slack-dm", labelKey: "slackDM", label: "Slack DM", icon: MessageCircle },
      { path: "/webhook-events", labelKey: "webhookEvents", label: "Webhook Events", icon: Inbox },
      { path: "/agent-activity", labelKey: "agentActivity", label: "Agent Activity", icon: Workflow },
      { path: "/chain-events", labelKey: "chainEvents", label: "Chain Events", icon: Radio },
      // KR-FE-AUTOFIX-LOG-PANEL — per-seam tool.probe_autofix_attempted.
      { path: "/probe-autofix-log", labelKey: "probeAutofixLog", label: "Probe Autofix Log", icon: Wrench },
    ],
  },
  {
    key: "control",
    label: "Control & Tickets",
    defaultCollapsed: false,
    items: [
      { path: "/sea-tickets", labelKey: "seaTickets", label: "Sea Tickets", icon: Waves },
      { path: "/kora-control", labelKey: "koraControl", label: "Kora Control", icon: OctagonAlert },
      { path: "/capabilities", labelKey: "capabilities", label: "Capabilities", icon: ShieldCheck },
      { path: "/charter", labelKey: "charter", label: "Charter", icon: Scroll },
    ],
  },
  {
    key: "daemon",
    label: "Daemon & Listeners",
    // Collapsed by default — diagnostic/listener pages are less-used
    // for routine operator triage; collapsing keeps the top-of-
    // sidebar focused on attention surfaces.
    defaultCollapsed: true,
    items: [
      { path: "/boot-status", labelKey: "bootStatus", label: "Boot Status", icon: PowerSquare },
      { path: "/dr-state", labelKey: "drState", label: "DR", icon: ShieldAlert },
      { path: "/mcp", labelKey: "mcp", label: "MCP", icon: Plug },
      { path: "/mcp-clients", labelKey: "mcpClients", label: "MCP Clients", icon: Cable },
      { path: "/cron", labelKey: "cron", label: "Cron", icon: Clock },
      { path: "/plugins", labelKey: "plugins", label: "Plugins", icon: Puzzle },
      { path: "/skills", labelKey: "skills", label: "Skills", icon: Package },
      { path: "/analytics", labelKey: "analytics", label: "Analytics", icon: BarChart3 },
    ],
  },
  {
    key: "settings",
    label: "Settings",
    defaultCollapsed: true,
    items: [
      { path: "/models", labelKey: "models", label: "Models", icon: Cpu },
      { path: "/config", labelKey: "config", label: "Config", icon: Settings },
      { path: "/env", labelKey: "keys", label: "Keys", icon: KeyRound },
      { path: "/profiles", labelKey: "profiles", label: "Profiles", icon: Users },
      { path: "/identity", labelKey: "identity", label: "Identity", icon: UserCircle },
    ],
  },
  {
    key: "diagnostic",
    label: "Diagnostic & Docs",
    defaultCollapsed: true,
    items: [
      { path: "/logs", labelKey: "logs", label: "Logs", icon: FileText },
      { path: "/docs", labelKey: "documentation", label: "Documentation", icon: BookOpen },
      { path: "/runbooks", labelKey: "runbooks", label: "Runbooks", icon: BookOpenCheck },
    ],
  },
];

// Derived flat list — kept for backward-compat with buildNavItems +
// partitionSidebarNav (which insert plugin items into the flat
// merged sequence). Source-of-truth lives in BUILTIN_NAV_GROUPS;
// the flat list is generated to avoid drift between the two views.
const BUILTIN_NAV_REST: NavItem[] = BUILTIN_NAV_GROUPS.flatMap(
  (g) => g.items,
);

const ICON_MAP: Record<string, ComponentType<{ className?: string }>> = {
  Activity,
  BarChart3,
  Clock,
  Cpu,
  FileText,
  KeyRound,
  MessageSquare,
  Package,
  Settings,
  Puzzle,
  Sparkles,
  Terminal,
  Globe,
  Database,
  Shield,
  Users,
  Wrench,
  Zap,
  Heart,
  Star,
  Code,
  Eye,
};

function resolveIcon(name: string): ComponentType<{ className?: string }> {
  return ICON_MAP[name] ?? Puzzle;
}

function buildNavItems(
  builtIn: NavItem[],
  manifests: PluginManifest[],
): NavItem[] {
  const items = [...builtIn];

  for (const manifest of manifests) {
    if (manifest.tab.override) continue;
    if (manifest.tab.hidden) continue;

    const pluginItem: NavItem = {
      path: manifest.tab.path,
      label: manifest.label,
      icon: resolveIcon(manifest.icon),
    };

    const pos = manifest.tab.position ?? "end";
    if (pos === "end") {
      items.push(pluginItem);
    } else if (pos.startsWith("after:")) {
      const target = "/" + pos.slice(6);
      const idx = items.findIndex((i) => i.path === target);
      items.splice(idx >= 0 ? idx + 1 : items.length, 0, pluginItem);
    } else if (pos.startsWith("before:")) {
      const target = "/" + pos.slice(7);
      const idx = items.findIndex((i) => i.path === target);
      items.splice(idx >= 0 ? idx : items.length, 0, pluginItem);
    } else {
      items.push(pluginItem);
    }
  }

  return items;
}

/** Split merged nav into built-in sidebar entries vs plugin tabs, preserving plugin order hints. */
function partitionSidebarNav(
  builtIn: NavItem[],
  manifests: PluginManifest[],
): { coreItems: NavItem[]; pluginItems: NavItem[] } {
  const merged = buildNavItems(builtIn, manifests);
  const builtinPaths = new Set(builtIn.map((i) => i.path));
  const coreItems: NavItem[] = [];
  const pluginItems: NavItem[] = [];
  for (const item of merged) {
    if (builtinPaths.has(item.path)) coreItems.push(item);
    else pluginItems.push(item);
  }
  return { coreItems, pluginItems };
}

// KR-FE-COCKPIT-NAV-RESTRUCTURE — re-group a flat coreItems list
// back into the canonical BUILTIN_NAV_GROUPS shape. ``coreItems``
// may have plugin-inserted entries spliced into the flat sequence
// (via the ``after:`` / ``before:`` manifest positioning); those
// entries pick up the group of the item they sit next to so the
// sidebar respects plugin author intent.
interface RenderedNavGroup {
  key: string;
  label: string;
  defaultCollapsed: boolean;
  items: NavItem[];
}

function groupCoreItems(
  coreItems: NavItem[],
  groups: readonly SidebarNavGroupDef[],
): RenderedNavGroup[] {
  // Build the path → groupKey lookup from the canonical groups.
  const pathToGroup = new Map<string, string>();
  for (const group of groups) {
    for (const item of group.items) {
      pathToGroup.set(item.path, group.key);
    }
  }
  // Initialise rendered groups in canonical order with empty
  // items[] — preserves operator-expected group ordering even
  // when a group has zero items after a feature flag hides it.
  const rendered: Map<string, RenderedNavGroup> = new Map();
  for (const group of groups) {
    rendered.set(group.key, {
      key: group.key,
      label: group.label,
      defaultCollapsed: group.defaultCollapsed,
      items: [],
    });
  }
  // Walk coreItems in their flat order. For plugin items spliced
  // into the flat list (not in pathToGroup), drop them into the
  // group whose last-seen item preceded them — keeping the
  // ``after:`` semantic visible in the sidebar.
  let lastSeenGroup: string | null = null;
  for (const item of coreItems) {
    let groupKey = pathToGroup.get(item.path);
    if (groupKey === undefined) {
      // Plugin-inserted item OR a route hidden by feature flag
      // (e.g. /analytics gated by show_token_analytics). The flag
      // drops the item from coreItems entirely so we never hit
      // this branch for built-in routes; plugin paths fall back
      // to the last-seen group, or the first group if none seen.
      groupKey = lastSeenGroup ?? groups[0]?.key ?? "overview";
    }
    rendered.get(groupKey)?.items.push(item);
    lastSeenGroup = groupKey;
  }
  return [...rendered.values()];
}

// Drift-guard: every built-in path must belong to exactly one
// group. Exported for the test_sidebar_orphan_pages_caught test
// to walk BUILTIN_NAV_REST and assert no orphans creep in.
export const SIDEBAR_GROUP_KEYS_IN_ORDER: readonly string[] =
  BUILTIN_NAV_GROUPS.map((g) => g.key);
export const SIDEBAR_PATH_TO_GROUP: Readonly<Record<string, string>> =
  Object.freeze(
    Object.fromEntries(
      BUILTIN_NAV_GROUPS.flatMap((g) =>
        g.items.map((i) => [i.path, g.key] as const),
      ),
    ),
  );

function buildRoutes(
  builtinRoutes: Record<string, ComponentType>,
  manifests: PluginManifest[],
): Array<{
  key: string;
  path: string;
  element: ReactNode;
}> {
  const byOverride = new Map<string, PluginManifest>();
  const addons: PluginManifest[] = [];

  for (const m of manifests) {
    if (m.tab.override) {
      byOverride.set(m.tab.override, m);
    } else {
      addons.push(m);
    }
  }

  const routes: Array<{
    key: string;
    path: string;
    element: ReactNode;
  }> = [];

  for (const [path, Component] of Object.entries(builtinRoutes)) {
    const om = byOverride.get(path);
    if (om) {
      routes.push({
        key: `override:${om.name}`,
        path,
        element: <PluginPage name={om.name} />,
      });
    } else {
      routes.push({ key: `builtin:${path}`, path, element: <Component /> });
    }
  }

  for (const m of addons) {
    if (m.tab.hidden) continue;
    if (m.tab.path === "/plugins") continue;
    if (builtinRoutes[m.tab.path]) continue;
    routes.push({
      key: `plugin:${m.name}`,
      path: m.tab.path,
      element: <PluginPage name={m.name} />,
    });
  }

  for (const m of manifests) {
    if (!m.tab.hidden) continue;
    if (m.tab.path === "/plugins") continue;
    if (builtinRoutes[m.tab.path] || m.tab.override) continue;
    routes.push({
      key: `plugin:hidden:${m.name}`,
      path: m.tab.path,
      element: <PluginPage name={m.name} />,
    });
  }

  return routes;
}

export default function App() {
  const { t } = useI18n();
  const { pathname } = useLocation();
  const { manifests, loading: pluginsLoading } = usePlugins();
  const { theme } = useTheme();
  const [mobileOpen, setMobileOpen] = useState(false);
  const closeMobile = useCallback(() => setMobileOpen(false), []);
  const isDocsRoute = pathname === "/docs" || pathname === "/docs/";
  const normalizedPath = pathname.replace(/\/$/, "") || "/";
  const isChatRoute = normalizedPath === "/chat";
  const embeddedChat = isDashboardEmbeddedChatEnabled();

  // `dashboard.show_token_analytics` gates the Analytics nav item.  The
  // page itself remains reachable by URL (it renders an explanation when
  // the flag is off — see AnalyticsPage), but hiding the nav entry avoids
  // surfacing misleading token/cost numbers in the sidebar.  Default off.
  const [showTokenAnalytics, setShowTokenAnalytics] = useState(false);
  useEffect(() => {
    api
      .getConfig()
      .then((cfg) => {
        const dash = (cfg?.dashboard ?? {}) as { show_token_analytics?: unknown };
        setShowTokenAnalytics(dash.show_token_analytics === true);
      })
      .catch(() => setShowTokenAnalytics(false));
  }, []);

  // A plugin can replace the built-in /chat page via `tab.override: "/chat"`
  // in its manifest.  When one does, `buildRoutes` already swaps the route
  // element for <PluginPage /> — but we also have to suppress the
  // persistent ChatPage host below, or the plugin's page and the built-in
  // terminal would paint on top of each other.  The override is niche
  // (nothing ships overriding /chat today) but it's an advertised
  // extension point, so preserve the pre-persistence contract: when a
  // plugin owns /chat, the built-in chat UI is entirely absent.
  //
  // Waiting on `pluginsLoading` is load-bearing: manifests arrive
  // asynchronously from /api/dashboard/plugins, so on initial render
  // `chatOverriddenByPlugin` is always false.  Without the loading
  // gate, the persistent host would mount, spawn a PTY, and THEN get
  // yanked out from under the user when the plugin's manifest resolves
  // — killing the session mid-paint.  Delaying host mount by the
  // plugin-load window (typically <50ms, worst case 2s safety timeout)
  // is the cheaper trade-off.
  const chatOverriddenByPlugin = useMemo(
    () => manifests.some((m) => m.tab.override === "/chat"),
    [manifests],
  );

  const builtinRoutes = useMemo(
    () => ({
      ...BUILTIN_ROUTES_CORE,
      ...(embeddedChat ? { "/chat": ChatRouteSink } : {}),
    }),
    [embeddedChat],
  );

  const builtinNav = useMemo(() => {
    const base = embeddedChat
      ? [CHAT_NAV_ITEM, ...BUILTIN_NAV_REST]
      : BUILTIN_NAV_REST;
    return showTokenAnalytics ? base : base.filter((n) => n.path !== "/analytics");
  }, [embeddedChat, showTokenAnalytics]);

  // KR-FE-PROMOTION-REVIEW-PANEL — pending-proposals count for the
  // sidebar attention badge. Polls every 60s so the operator sees
  // new proposals land without refreshing. Best-effort: a failed
  // fetch silently leaves the badge hidden (null) rather than
  // surfacing a transient network error in the nav.
  const promotionPendingCount = usePromotionPendingCount();

  const sidebarNav = useMemo(() => {
    const partitioned = partitionSidebarNav(builtinNav, manifests);
    if (promotionPendingCount === null) return partitioned;
    return {
      pluginItems: partitioned.pluginItems,
      coreItems: partitioned.coreItems.map((item) =>
        item.path === "/promotions/phrasebook"
          ? { ...item, badgeCount: promotionPendingCount }
          : item,
      ),
    };
  }, [builtinNav, manifests, promotionPendingCount]);

  // KR-FE-COCKPIT-NAV-RESTRUCTURE — re-group the flat coreItems into
  // the canonical SIDEBAR_GROUPS shape. Plugin-inserted items take
  // the group of the item they sit next to (see groupCoreItems).
  const sidebarGroups = useMemo(
    () => groupCoreItems(sidebarNav.coreItems, BUILTIN_NAV_GROUPS),
    [sidebarNav.coreItems],
  );

  // Per-group collapse state with localStorage persistence — the
  // hook resolves operator-override vs group default in one place.
  const groupCollapse = useSidebarGroupCollapse();
  const routes = useMemo(
    () => buildRoutes(builtinRoutes, manifests),
    [builtinRoutes, manifests],
  );
  const pluginTabMeta = useMemo(
    () =>
      manifests
        .filter((m) => !m.tab.hidden)
        .map((m) => ({
          path: m.tab.override ?? m.tab.path,
          label: m.label,
        })),
    [manifests],
  );

  const layoutVariant = theme.layoutVariant ?? "standard";

  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMobileOpen(false);
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [mobileOpen]);

  useEffect(() => {
    const mql = window.matchMedia("(min-width: 1024px)");
    const onChange = (e: MediaQueryListEvent) => {
      if (e.matches) setMobileOpen(false);
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return (
    <div
      data-layout-variant={layoutVariant}
      className="font-mondwest flex h-dvh max-h-dvh min-h-0 flex-col overflow-hidden bg-black uppercase text-midground antialiased"
    >
      <SelectionSwitcher />
      <Backdrop />
      <PluginSlot name="backdrop" />

      <header
        className={cn(
          "lg:hidden fixed top-0 left-0 right-0 z-40 min-h-14",
          "flex items-center gap-2 px-4 py-2",
          "border-b border-current/20",
          "bg-background-base/90 backdrop-blur-sm",
        )}
        style={{
          background: "var(--component-header-background)",
          borderImage: "var(--component-header-border-image)",
          clipPath: "var(--component-header-clip-path)",
        }}
      >
        <Button
          ghost
          size="icon"
          onClick={() => setMobileOpen(true)}
          aria-label={t.app.openNavigation}
          aria-expanded={mobileOpen}
          aria-controls="app-sidebar"
          className="text-midground/70 hover:text-midground"
        >
          <Menu />
        </Button>

        <Typography
          className="font-bold text-[0.95rem] leading-[0.95] tracking-[0.05em] text-midground"
          style={{ mixBlendMode: "plus-lighter" }}
        >
          {t.app.brand}
        </Typography>
      </header>

      {mobileOpen && (
        <Button
          ghost
          aria-label={t.app.closeNavigation}
          onClick={closeMobile}
          className={cn(
            "lg:hidden fixed inset-0 z-40 p-0 block",
            "bg-black/60 backdrop-blur-sm",
          )}
        />
      )}

      <PluginSlot name="header-banner" />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden pt-14 lg:pt-0">
        <div className="flex min-h-0 min-w-0 flex-1">
          <aside
            id="app-sidebar"
            aria-label={t.app.navigation}
            className={cn(
              "fixed top-0 left-0 z-50 flex h-dvh max-h-dvh w-64 min-h-0 flex-col",
              "border-r border-current/20",
              "bg-background-base/95 backdrop-blur-sm",
              "transition-transform duration-200 ease-out",
              mobileOpen ? "translate-x-0" : "-translate-x-full",
              "lg:sticky lg:top-0 lg:translate-x-0 lg:shrink-0",
            )}
            style={{
              background: "var(--component-sidebar-background)",
              clipPath: "var(--component-sidebar-clip-path)",
              borderImage: "var(--component-sidebar-border-image)",
            }}
          >
            <div
              className={cn(
                "flex h-14 shrink-0 items-center justify-between gap-2 px-4",
                "border-b border-current/20",
              )}
            >
              <div className="flex items-center gap-2">
                <PluginSlot name="header-left" />

                <Typography
                  className="font-bold text-[1.125rem] leading-[0.95] tracking-[0.0525rem] text-midground"
                  style={{ mixBlendMode: "plus-lighter" }}
                >
                  Hermes
                  <br />
                  Agent
                </Typography>
              </div>

              <Button
                ghost
                size="icon"
                onClick={closeMobile}
                aria-label={t.app.closeNavigation}
                className="lg:hidden text-midground/70 hover:text-midground"
              >
                <X />
              </Button>
            </div>

            <nav
              className="min-h-0 w-full flex-1 overflow-y-auto overflow-x-hidden border-t border-current/10 py-2"
              aria-label={t.app.navigation}
            >
              {/* KR-FE-COCKPIT-NAV-RESTRUCTURE — grouped sidebar.
                  Each group has a collapsible header with optional
                  badge-sum. Plugin items keep their own bottom
                  section (existing behavior). */}
              {sidebarGroups.map((group) => (
                <SidebarNavGroup
                  key={group.key}
                  group={group}
                  isCollapsed={groupCollapse.isCollapsed(
                    group.key,
                    group.defaultCollapsed,
                  )}
                  onToggle={() =>
                    groupCollapse.toggle(group.key, group.defaultCollapsed)
                  }
                  closeMobile={closeMobile}
                  t={t}
                />
              ))}

              {sidebarNav.pluginItems.length > 0 && (
                <div
                  aria-labelledby="hermes-sidebar-plugin-nav-heading"
                  className="flex flex-col border-t border-current/10 pb-2"
                  role="group"
                >
                  <span
                    className={cn(
                      "px-5 pt-2.5 pb-1",
                      "font-mondwest text-[0.6rem] tracking-[0.15em] uppercase opacity-30",
                    )}
                    id="hermes-sidebar-plugin-nav-heading"
                  >
                    {t.app.pluginNavSection}
                  </span>

                  <ul className="flex flex-col">
                    {sidebarNav.pluginItems.map((item) => (
                      <SidebarNavLink
                        closeMobile={closeMobile}
                        item={item}
                        key={item.path}
                        t={t}
                      />
                    ))}
                  </ul>
                </div>
              )}
            </nav>

            <SidebarSystemActions onNavigate={closeMobile} />

            <div
              className={cn(
                "flex shrink-0 items-center justify-between gap-2",
                "px-3 py-2",
                "border-t border-current/20",
              )}
            >
              <div className="flex min-w-0 items-center gap-2">
                <PluginSlot name="header-right" />
                <ThemeSwitcher dropUp />
                <LanguageSwitcher dropUp />
              </div>
            </div>

            <SidebarFooter />
          </aside>

          <PageHeaderProvider pluginTabs={pluginTabMeta}>
            <div
              className={cn(
                "relative z-2 flex min-w-0 min-h-0 flex-1 flex-col",
                "px-3 sm:px-6",
                isChatRoute
                  ? "pb-0 pt-1 sm:pt-2 lg:pt-4"
                  : "pt-2 sm:pt-4 lg:pt-6",
                isDocsRoute && "min-h-0 flex-1",
              )}
            >
              <PluginSlot name="pre-main" />
              <div
                className={cn(
                  "w-full min-w-0",
                  !isChatRoute &&
                    "pb-[calc(2rem+env(safe-area-inset-bottom,0px))] lg:pb-8",
                  (isDocsRoute || isChatRoute) &&
                    "min-h-0 flex flex-1 flex-col",
                )}
              >
                <Routes>
                  {routes.map(({ key, path, element }) => (
                    <Route key={key} path={path} element={element} />
                  ))}
                  <Route
                    path="*"
                    element={
                      <UnknownRouteFallback pluginsLoading={pluginsLoading} />
                    }
                  />
                </Routes>

                {embeddedChat &&
                  !chatOverriddenByPlugin &&
                  (pluginsLoading ? (
                    isChatRoute ? (
                      <div
                        className="flex min-h-0 min-w-0 flex-1 items-center justify-center"
                        aria-busy="true"
                        aria-live="polite"
                      >
                        <div className="flex items-center gap-2 text-sm text-muted-foreground">
                          <Spinner />
                          <span>Loading chat…</span>
                        </div>
                      </div>
                    ) : null
                  ) : (
                    <div
                      data-chat-active={isChatRoute ? "true" : "false"}
                      className={cn(
                        "min-h-0 min-w-0",
                        isChatRoute ? "flex flex-1 flex-col" : "hidden",
                      )}
                      aria-hidden={!isChatRoute}
                    >
                      <ChatPage isActive={isChatRoute} />
                    </div>
                  ))}
              </div>
              <PluginSlot name="post-main" />
            </div>
          </PageHeaderProvider>
        </div>
      </div>

      <PluginSlot name="overlay" />
    </div>
  );
}

function SidebarNavLink({ closeMobile, item, t }: SidebarNavLinkProps) {
  const { path, label, labelKey, icon: Icon, badgeCount } = item;

  const navLabel = labelKey
    ? ((t.app.nav as Record<string, string>)[labelKey] ?? label)
    : label;

  return (
    <li>
      <NavLink
        to={path}
        end={path === "/sessions"}
        onClick={closeMobile}
        className={({ isActive }) =>
          cn(
            "group relative flex items-center gap-3",
            "px-5 py-2.5",
            "font-mondwest text-[0.8rem] tracking-[0.12em]",
            "whitespace-nowrap transition-colors cursor-pointer",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground",
            isActive ? "text-midground" : "opacity-60 hover:opacity-100",
          )
        }
        style={{
          clipPath: "var(--component-tab-clip-path)",
        }}
      >
        {({ isActive }) => (
          <>
            <Icon className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">{navLabel}</span>

            {typeof badgeCount === "number" && badgeCount > 0 && (
              <span
                aria-label={`${badgeCount} awaiting review`}
                className={cn(
                  "ml-auto flex-shrink-0 rounded-sm px-1.5 py-0.5",
                  "font-mono text-[0.55rem] tracking-normal",
                  "bg-yellow-500/30 text-yellow-200",
                )}
              >
                {badgeCount}
              </span>
            )}

            <span
              aria-hidden
              className="absolute inset-y-0.5 left-1.5 right-1.5 bg-midground opacity-0 pointer-events-none transition-opacity duration-200 group-hover:opacity-5"
            />

            {isActive && (
              <span
                aria-hidden
                className="absolute left-0 top-0 bottom-0 w-px bg-midground"
                style={{ mixBlendMode: "plus-lighter" }}
              />
            )}
          </>
        )}
      </NavLink>
    </li>
  );
}

// KR-FE-COCKPIT-NAV-RESTRUCTURE — group header + collapsible body.
// The header is keyboard-actionable (operator can tab + space/enter
// to toggle). Per-group ``badgeCount`` is the SUM of in-group items'
// badgeCounts (so the operator sees "Promotion Loops ▾ 3" without
// expanding the group to find which sub-item is demanding attention).
function SidebarNavGroup({
  group,
  isCollapsed,
  onToggle,
  closeMobile,
  t,
}: {
  group: RenderedNavGroup;
  isCollapsed: boolean;
  onToggle: () => void;
  closeMobile: () => void;
  t: Translations;
}) {
  if (group.items.length === 0) return null;
  // Group label honors i18n if a matching key exists in t.app.nav,
  // otherwise falls back to the declared label — same fallback
  // pattern SidebarNavLink uses for per-item labels.
  const navLabels = t.app.nav as Record<string, string>;
  const groupLabel = navLabels[`group_${group.key}`] ?? group.label;
  const badgeSum = group.items.reduce(
    (acc, item) =>
      acc + (typeof item.badgeCount === "number" ? item.badgeCount : 0),
    0,
  );
  return (
    <div className="flex flex-col" role="group" aria-labelledby={`sidebar-group-${group.key}`}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!isCollapsed}
        aria-controls={`sidebar-group-${group.key}-items`}
        id={`sidebar-group-${group.key}`}
        className={cn(
          "flex items-center gap-2 px-5 pt-2.5 pb-1",
          "font-mondwest text-[0.6rem] tracking-[0.15em] uppercase",
          "opacity-50 hover:opacity-90 transition-opacity",
          "cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground",
        )}
      >
        {isCollapsed ? (
          <ChevronRight className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronDown className="h-3 w-3 shrink-0" />
        )}
        <span className="flex-1 text-left">{groupLabel}</span>
        {badgeSum > 0 && (
          <span
            aria-label={`${badgeSum} total awaiting review across ${groupLabel}`}
            className="rounded-sm px-1.5 py-0.5 font-mono text-[0.55rem] tracking-normal bg-yellow-500/30 text-yellow-200"
          >
            {badgeSum}
          </span>
        )}
      </button>
      {!isCollapsed && (
        <ul
          id={`sidebar-group-${group.key}-items`}
          className="flex flex-col"
        >
          {group.items.map((item) => (
            <SidebarNavLink
              closeMobile={closeMobile}
              item={item}
              key={item.path}
              t={t}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function SidebarSystemActions({ onNavigate }: { onNavigate: () => void }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { activeAction, isBusy, isRunning, pendingAction, runAction } =
    useSystemActions();

  const items: SystemActionItem[] = [
    {
      action: "restart",
      icon: RotateCw,
      label: t.status.restartGateway,
      runningLabel: t.status.restartingGateway,
      spin: true,
    },
    {
      action: "update",
      icon: Download,
      label: t.status.updateHermes,
      runningLabel: t.status.updatingHermes,
      spin: false,
    },
  ];

  const handleClick = (action: SystemAction) => {
    if (isBusy) return;
    void runAction(action);
    navigate("/sessions");
    onNavigate();
  };

  return (
    <div
      className={cn(
        "shrink-0 flex flex-col",
        "border-t border-current/10",
        "py-1",
      )}
    >
      <span
        className={cn(
          "px-5 pt-0.5 pb-0.5",
          "font-mondwest text-[0.6rem] tracking-[0.15em] uppercase opacity-30",
        )}
      >
        {t.app.system}
      </span>

      <SidebarStatusStrip />

      <ul className="flex flex-col">
        {items.map(({ action, icon: Icon, label, runningLabel, spin }) => {
          const isPending = pendingAction === action;
          const isActionRunning =
            activeAction === action && isRunning && !isPending;
          const busy = isPending || isActionRunning;
          const displayLabel = isActionRunning ? runningLabel : label;
          const disabled = isBusy && !busy;

          return (
            <li key={action}>
              <ListItem
                onClick={() => handleClick(action)}
                disabled={disabled}
                aria-busy={busy}
                active={busy}
                className={cn(
                  "gap-3 px-5 py-1.5 whitespace-nowrap",
                  "font-mondwest text-[0.75rem] tracking-[0.1em]",
                  "transition-opacity",
                  busy
                    ? "text-midground opacity-100"
                    : "opacity-60 hover:opacity-100",
                  "disabled:opacity-30",
                )}
              >
                {isPending ? (
                  <Spinner className="shrink-0 text-[0.875rem]" />
                ) : isActionRunning && spin ? (
                  <Spinner className="shrink-0 text-[0.875rem]" />
                ) : (
                  <Icon
                    className={cn(
                      "h-3.5 w-3.5 shrink-0",
                      isActionRunning && !spin && "animate-pulse",
                    )}
                  />
                )}

                <span className="truncate">{displayLabel}</span>

                <span
                  aria-hidden
                  className="absolute inset-y-0.5 left-1.5 right-1.5 bg-midground opacity-0 pointer-events-none transition-opacity duration-200 group-hover:opacity-5"
                />

                {busy && (
                  <span
                    aria-hidden
                    className="absolute left-0 top-0 bottom-0 w-px bg-midground"
                    style={{ mixBlendMode: "plus-lighter" }}
                  />
                )}
              </ListItem>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

interface NavItem {
  icon: ComponentType<{ className?: string }>;
  label: string;
  labelKey?: string;
  path: string;
  // KR-FE-PROMOTION-REVIEW-PANEL — optional attention-count chip
  // rendered after the nav label. ``null`` skips the chip; ``0``
  // still renders (operator can see "nothing pending" at a glance);
  // numbers render in a yellow pill so the eye catches it.
  badgeCount?: number | null;
}

interface SidebarNavLinkProps {
  closeMobile: () => void;
  item: NavItem;
  t: Translations;
}

interface SystemActionItem {
  action: SystemAction;
  icon: ComponentType<{ className?: string }>;
  label: string;
  runningLabel: string;
  spin: boolean;
}
