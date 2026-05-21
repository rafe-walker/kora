// The dashboard can be served either at the root of its host (e.g.
// https://kanban.tilos.com/) or under a URL prefix when reverse-proxied
// (e.g. https://mission-control.tilos.com/hermes/). The Python backend
// injects ``window.__HERMES_BASE_PATH__`` into index.html based on the
// incoming ``X-Forwarded-Prefix`` header so the SPA can address its own
// ``/api/...`` and ``/dashboard-plugins/...`` URLs correctly without a
// rebuild. Empty string means "served at root".
function readBasePath(): string {
  if (typeof window === "undefined") return "";
  const raw = window.__HERMES_BASE_PATH__ ?? "";
  if (!raw) return "";
  // Normalise: ensure leading slash, strip trailing slash.
  const withLead = raw.startsWith("/") ? raw : `/${raw}`;
  return withLead.replace(/\/+$/, "");
}

export const HERMES_BASE_PATH = readBasePath();
const BASE = HERMES_BASE_PATH;

import type { DashboardTheme } from "@/themes/types";

// Ephemeral session token for protected endpoints.
// Injected into index.html by the server — never fetched via API.
declare global {
  interface Window {
    __HERMES_SESSION_TOKEN__?: string;
    __HERMES_BASE_PATH__?: string;
  }
}
let _sessionToken: string | null = null;
const SESSION_HEADER = "X-Hermes-Session-Token";

function setSessionHeader(headers: Headers, token: string): void {
  if (!headers.has(SESSION_HEADER)) {
    headers.set(SESSION_HEADER, token);
  }
}

export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  // Inject the session token into all /api/ requests.
  const headers = new Headers(init?.headers);
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) {
    setSessionHeader(headers, token);
  }
  const res = await fetch(`${BASE}${url}`, { ...init, headers });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

async function getSessionToken(): Promise<string> {
  if (_sessionToken) return _sessionToken;
  const injected = window.__HERMES_SESSION_TOKEN__;
  if (injected) {
    _sessionToken = injected;
    return _sessionToken;
  }
  throw new Error("Session token not available — page must be served by the Hermes dashboard server");
}

export const api = {
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),
  getOperationalState: () =>
    fetchJSON<OperationalStateResponse>("/api/operational-state"),
  getKoraAssignedSeaTickets: () =>
    fetchJSON<KoraAssignedSeaTicketsResponse>(
      "/api/sea-tickets/kora-assigned",
    ),
  getKoraControlObservedState: () =>
    fetchJSON<KoraControlObservedStateResponse>(
      "/api/kora-control/observed-state",
    ),
  getBootStatus: () => fetchJSON<BootStatusResponse>("/api/boot-status"),
  getCostState: () => fetchJSON<CostStateResponse>("/api/cost-state"),
  getCapabilities: () =>
    fetchJSON<CapabilitiesResponse>("/api/capabilities"),
  getSessions: (limit = 20, offset = 0) =>
    fetchJSON<PaginatedSessions>(`/api/sessions?limit=${limit}&offset=${offset}`),
  getSessionMessages: (id: string) =>
    fetchJSON<SessionMessagesResponse>(`/api/sessions/${encodeURIComponent(id)}/messages`),
  getSessionLatestDescendant: (id: string) =>
    fetchJSON<SessionLatestDescendantResponse>(
      `/api/sessions/${encodeURIComponent(id)}/latest-descendant`,
    ),
  deleteSession: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  getLogs: (params: { file?: string; lines?: number; level?: string; component?: string }) => {
    const qs = new URLSearchParams();
    if (params.file) qs.set("file", params.file);
    if (params.lines) qs.set("lines", String(params.lines));
    if (params.level && params.level !== "ALL") qs.set("level", params.level);
    if (params.component && params.component !== "all") qs.set("component", params.component);
    return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
  },
  getAnalytics: (days: number) =>
    fetchJSON<AnalyticsResponse>(`/api/analytics/usage?days=${days}`),
  getModelsAnalytics: (days: number) =>
    fetchJSON<ModelsAnalyticsResponse>(`/api/analytics/models?days=${days}`),
  getConfig: () => fetchJSON<Record<string, unknown>>("/api/config"),
  getDefaults: () => fetchJSON<Record<string, unknown>>("/api/config/defaults"),
  getSchema: () => fetchJSON<{ fields: Record<string, unknown>; category_order: string[] }>("/api/config/schema"),
  getModelInfo: () => fetchJSON<ModelInfoResponse>("/api/model/info"),
  getModelOptions: () => fetchJSON<ModelOptionsResponse>("/api/model/options"),
  getAuxiliaryModels: () => fetchJSON<AuxiliaryModelsResponse>("/api/model/auxiliary"),
  setModelAssignment: (body: ModelAssignmentRequest) =>
    fetchJSON<ModelAssignmentResponse>("/api/model/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  saveConfig: (config: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean }>("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }),
  getConfigRaw: () => fetchJSON<{ yaml: string }>("/api/config/raw"),
  saveConfigRaw: (yaml_text: string) =>
    fetchJSON<{ ok: boolean }>("/api/config/raw", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml_text }),
    }),
  getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
  setEnvVar: (key: string, value: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    }),
  deleteEnvVar: (key: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),
  revealEnvVar: async (key: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [SESSION_HEADER]: token,
      },
      body: JSON.stringify({ key }),
    });
  },

  // Cron jobs
  getCronJobs: (profile = "all") =>
    fetchJSON<CronJob[]>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`),
  createCronJob: (job: { prompt: string; schedule: string; name?: string; deliver?: string }, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(job),
    }),
  pauseCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/pause?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  resumeCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/resume?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  triggerCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/trigger?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  deleteCronJob: (id: string, profile = "default") =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${encodeURIComponent(id)}?profile=${encodeURIComponent(profile)}`, { method: "DELETE" }),

  // MCP servers
  getMCPServers: () => fetchJSON<MCPServer[]>("/api/mcp/servers"),
  getMCPServer: (name: string) =>
    fetchJSON<MCPServer>(`/api/mcp/servers/${encodeURIComponent(name)}`),
  probeMCPServer: (name: string) =>
    fetchJSON<MCPProbeResponse>(
      `/api/mcp/servers/${encodeURIComponent(name)}/probe`,
      { method: "POST" },
    ),
  setMCPServerTools: (
    name: string,
    body: { enabled_tools: string[]; all_tools: string[] },
  ) =>
    fetchJSON<MCPServer>(`/api/mcp/servers/${encodeURIComponent(name)}/tools`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  enableMCPServer: (name: string) =>
    fetchJSON<MCPServer>(
      `/api/mcp/servers/${encodeURIComponent(name)}/enable`,
      { method: "POST" },
    ),
  disableMCPServer: (name: string) =>
    fetchJSON<MCPServer>(
      `/api/mcp/servers/${encodeURIComponent(name)}/disable`,
      { method: "POST" },
    ),

  // Gateway platform identity (KR-P2-G)
  listGatewayPlatforms: () =>
    fetchJSON<GatewayPlatformIdentity[]>("/api/gateway/platforms"),
  getGatewayPlatform: (platform_id: string) =>
    fetchJSON<GatewayPlatformIdentity>(
      `/api/gateway/platforms/${encodeURIComponent(platform_id)}`,
    ),
  updateGatewayPlatformIdentity: (platform_id: string, display_name: string) =>
    fetchJSON<GatewayPlatformIdentity>(
      `/api/gateway/platforms/${encodeURIComponent(platform_id)}/identity`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name }),
      },
    ),

  // Profiles (minimal)
  getProfiles: () =>
    fetchJSON<{ profiles: ProfileInfo[] }>("/api/profiles"),
  createProfile: (body: { name: string; clone_from_default: boolean }) =>
    fetchJSON<{ ok: boolean; name: string; path: string }>("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  renameProfile: (name: string, newName: string) =>
    fetchJSON<{ ok: boolean; name: string; path: string }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_name: newName }),
      },
    ),
  deleteProfile: (name: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  getProfileSetupCommand: (name: string) =>
    fetchJSON<{ command: string }>(
      `/api/profiles/${encodeURIComponent(name)}/setup-command`,
    ),
  getProfileSoul: (name: string) =>
    fetchJSON<{ content: string; exists: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
    ),
  updateProfileSoul: (name: string, content: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
    ),

  // Skills & Toolsets
  getSkills: () => fetchJSON<SkillInfo[]>("/api/skills"),
  toggleSkill: (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean }>("/api/skills/toggle", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled }),
    }),
  getToolsets: () => fetchJSON<ToolsetInfo[]>("/api/tools/toolsets"),

  // Session search (FTS5)
  searchSessions: (q: string) =>
    fetchJSON<SessionSearchResponse>(`/api/sessions/search?q=${encodeURIComponent(q)}`),

  // OAuth provider management
  getOAuthProviders: () =>
    fetchJSON<OAuthProvidersResponse>("/api/providers/oauth"),
  disconnectOAuthProvider: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean; provider: string }>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}`,
      {
        method: "DELETE",
        headers: { [SESSION_HEADER]: token },
      },
    );
  },
  startOAuthLogin: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthStartResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          [SESSION_HEADER]: token,
        },
        body: "{}",
      },
    );
  },
  submitOAuthCode: async (providerId: string, sessionId: string, code: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthSubmitResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          [SESSION_HEADER]: token,
        },
        body: JSON.stringify({ session_id: sessionId, code }),
      },
    );
  },
  pollOAuthSession: (providerId: string, sessionId: string) =>
    fetchJSON<OAuthPollResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`,
    ),
  cancelOAuthSession: async (sessionId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean }>(
      `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
        headers: { [SESSION_HEADER]: token },
      },
    );
  },

  // Gateway / update actions
  restartGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/restart", { method: "POST" }),
  updateHermes: () =>
    fetchJSON<ActionResponse>("/api/hermes/update", { method: "POST" }),
  getActionStatus: (name: string, lines = 200) =>
    fetchJSON<ActionStatusResponse>(
      `/api/actions/${encodeURIComponent(name)}/status?lines=${lines}`,
    ),

  // Dashboard plugins
  getPlugins: () =>
    fetchJSON<PluginManifestResponse[]>("/api/dashboard/plugins"),
  rescanPlugins: () =>
    fetchJSON<{ ok: boolean; count: number }>("/api/dashboard/plugins/rescan"),

  getPluginsHub: () => fetchJSON<PluginsHubResponse>("/api/dashboard/plugins/hub"),

  installAgentPlugin: (body: AgentPluginInstallRequest) =>
    fetchJSON<AgentPluginInstallResponse>("/api/dashboard/agent-plugins/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body }),
    }),

  enableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${encodeURIComponent(name)}/enable`,
      { method: "POST" },
    ),

  disableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${encodeURIComponent(name)}/disable`,
      { method: "POST" },
    ),

  updateAgentPlugin: (name: string) =>
    fetchJSON<AgentPluginUpdateResponse>(
      `/api/dashboard/agent-plugins/${encodeURIComponent(name)}/update`,
      { method: "POST" },
    ),

  removeAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string }>(
      `/api/dashboard/agent-plugins/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),

  savePluginProviders: (body: PluginProvidersPutRequest) =>
    fetchJSON<{ ok: boolean }>("/api/dashboard/plugin-providers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  setPluginVisibility: (name: string, hidden: boolean) =>
    fetchJSON<{ ok: boolean; name: string; hidden: boolean }>(
      `/api/dashboard/plugins/${encodeURIComponent(name)}/visibility`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hidden }),
      },
    ),

  // Dashboard themes
  getThemes: () =>
    fetchJSON<DashboardThemesResponse>("/api/dashboard/themes"),
  setTheme: (name: string) =>
    fetchJSON<{ ok: boolean; theme: string }>("/api/dashboard/theme", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
};

export interface ActionResponse {
  name: string;
  ok: boolean;
  pid: number;
}

export interface ActionStatusResponse {
  exit_code: number | null;
  lines: string[];
  name: string;
  pid: number | null;
  running: boolean;
}

export interface PlatformStatus {
  error_code?: string;
  error_message?: string;
  state: string;
  updated_at: string;
}

export interface StatusResponse {
  active_sessions: number;
  config_path: string;
  config_version: number;
  env_path: string;
  gateway_exit_reason: string | null;
  gateway_health_url: string | null;
  gateway_pid: number | null;
  gateway_platforms: Record<string, PlatformStatus>;
  gateway_running: boolean;
  gateway_state: string | null;
  gateway_updated_at: string | null;
  hermes_home: string;
  latest_config_version: number;
  release_date: string;
  version: string;
}

export interface SessionInfo {
  id: string;
  source: string | null;
  model: string | null;
  title: string | null;
  started_at: number;
  ended_at: number | null;
  last_active: number;
  is_active: boolean;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  preview: string | null;
  parent_session_id?: string | null;
}

export interface SessionLatestDescendantResponse {
  requested_session_id: string;
  session_id: string;
  path: string[];
  changed: boolean;
}

export interface PaginatedSessions {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface EnvVarInfo {
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  url: string | null;
  category: string;
  is_password: boolean;
  tools: string[];
  advanced: boolean;
}

export interface SessionMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_name?: string;
  tool_call_id?: string;
  timestamp?: number;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
}

export interface LogsResponse {
  file: string;
  lines: string[];
}

export interface AnalyticsDailyEntry {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsModelEntry {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsSkillEntry {
  skill: string;
  view_count: number;
  manage_count: number;
  total_count: number;
  percentage: number;
  last_used_at: number | null;
}

export interface AnalyticsSkillsSummary {
  total_skill_loads: number;
  total_skill_edits: number;
  total_skill_actions: number;
  distinct_skills_used: number;
}

export interface AnalyticsResponse {
  daily: AnalyticsDailyEntry[];
  by_model: AnalyticsModelEntry[];
  totals: {
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  skills: {
    summary: AnalyticsSkillsSummary;
    top_skills: AnalyticsSkillEntry[];
  };
}

export interface ProfileInfo {
  name: string;
  path: string;
  is_default: boolean;
  model: string | null;
  provider: string | null;
  has_env: boolean;
  skill_count: number;
}

export interface ModelsAnalyticsModelEntry {
  model: string;
  provider: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
  tool_calls: number;
  last_used_at: number;
  avg_tokens_per_session: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

export interface ModelsAnalyticsResponse {
  models: ModelsAnalyticsModelEntry[];
  totals: {
    distinct_models: number;
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  period_days: number;
}

export interface CronJob {
  id: string;
  profile?: string | null;
  profile_name?: string | null;
  hermes_home?: string | null;
  is_default_profile?: boolean;
  name?: string | null;
  prompt?: string | null;
  script?: string | null;
  schedule?: { kind?: string; expr?: string; display?: string };
  schedule_display?: string | null;
  enabled: boolean;
  state?: string | null;
  deliver?: string | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_error?: string | null;
}

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

export interface ToolsetInfo {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  tools: string[];
}

export interface SessionSearchResult {
  session_id: string;
  snippet: string;
  role: string | null;
  source: string | null;
  model: string | null;
  session_started: number | null;
}

export interface SessionSearchResponse {
  results: SessionSearchResult[];
}

// ── Model info types ──────────────────────────────────────────────────

export interface ModelInfoResponse {
  model: string;
  provider: string;
  auto_context_length: number;
  config_context_length: number;
  effective_context_length: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

// ── Model options / assignment types ──────────────────────────────────

export interface ModelOptionProvider {
  name: string;
  slug: string;
  models?: string[];
  total_models?: number;
  is_current?: boolean;
  is_user_defined?: boolean;
  source?: string;
  warning?: string;
}

export interface ModelOptionsResponse {
  model?: string;
  provider?: string;
  providers?: ModelOptionProvider[];
}

export interface AuxiliaryTaskAssignment {
  task: string;
  provider: string;
  model: string;
  base_url: string;
}

export interface AuxiliaryModelsResponse {
  tasks: AuxiliaryTaskAssignment[];
  main: { provider: string; model: string };
}

export interface ModelAssignmentRequest {
  scope: "main" | "auxiliary";
  provider: string;
  model: string;
  /** For auxiliary: task slot name, "" for all, "__reset__" to reset all. */
  task?: string;
}

export interface ModelAssignmentResponse {
  ok: boolean;
  scope?: string;
  provider?: string;
  model?: string;
  tasks?: string[];
  reset?: boolean;
}

// ── OAuth provider types ────────────────────────────────────────────────

export interface OAuthProviderStatus {
  logged_in: boolean;
  source?: string | null;
  source_label?: string | null;
  token_preview?: string | null;
  expires_at?: string | null;
  has_refresh_token?: boolean;
  last_refresh?: string | null;
  error?: string;
}

export interface OAuthProvider {
  id: string;
  name: string;
  /** "pkce" (browser redirect + paste code), "device_code" (show code + URL),
   *  or "external" (delegated to a separate CLI like Claude Code or Qwen). */
  flow: "pkce" | "device_code" | "external";
  cli_command: string;
  docs_url: string;
  status: OAuthProviderStatus;
}

export interface OAuthProvidersResponse {
  providers: OAuthProvider[];
}

/** Discriminated union — the shape of /start depends on the flow. */
export type OAuthStartResponse =
  | {
      session_id: string;
      flow: "pkce";
      auth_url: string;
      expires_in: number;
    }
  | {
      session_id: string;
      flow: "device_code";
      user_code: string;
      verification_url: string;
      expires_in: number;
      poll_interval: number;
    };

export interface OAuthSubmitResponse {
  ok: boolean;
  status: "approved" | "error";
  message?: string;
}

export interface OAuthPollResponse {
  session_id: string;
  status: "pending" | "approved" | "denied" | "expired" | "error";
  error_message?: string | null;
  expires_at?: number | null;
}

// ── Dashboard theme types ──────────────────────────────────────────────

export interface DashboardThemeSummary {
  description: string;
  label: string;
  name: string;
  /** Full theme definition for user themes; undefined for built-ins
   *  (which the frontend already has locally). */
  definition?: DashboardTheme;
}

export interface DashboardThemesResponse {
  active: string;
  themes: DashboardThemeSummary[];
}

// ── Dashboard plugin types ─────────────────────────────────────────────

export interface PluginManifestResponse {
  name: string;
  label: string;
  description: string;
  icon: string;
  version: string;
  tab: {
    path: string;
    position?: string;
    override?: string;
    hidden?: boolean;
  };
  slots?: string[];
  entry: string;
  css?: string | null;
  has_api: boolean;
  source: string;
}

export interface HubAgentPluginRow {
  name: string;
  version: string;
  description: string;
  source: string;
  runtime_status: "disabled" | "enabled" | "inactive";
  has_dashboard_manifest: boolean;
  dashboard_manifest: PluginManifestResponse | null;
  path: string;
  can_remove: boolean;
  can_update_git: boolean;
  auth_required: boolean;
  auth_command: string;
  user_hidden: boolean;
}

export interface PluginsHubProviders {
  memory_provider: string;
  memory_options: Array<{ name: string; description: string }>;
  context_engine: string;
  context_options: Array<{ name: string; description: string }>;
}

export interface PluginsHubResponse {
  plugins: HubAgentPluginRow[];
  orphan_dashboard_plugins: PluginManifestResponse[];
  providers: PluginsHubProviders;
}

export interface AgentPluginInstallRequest {
  identifier: string;
  force?: boolean;
  enable?: boolean;
}

export interface AgentPluginInstallResponse {
  ok: boolean;
  plugin_name?: string;
  warnings?: string[];
  missing_env?: string[];
  after_install_path?: string | null;
  enabled?: boolean;
  error?: string;
}

export interface AgentPluginUpdateResponse {
  ok: boolean;
  name?: string;
  output?: string;
  unchanged?: boolean;
  error?: string;
}

export interface PluginProvidersPutRequest {
  memory_provider?: string;
  context_engine?: string;
}

export interface MCPServerToolsConfig {
  include: string[] | null;
  exclude: string[] | null;
  summary: string;
}

export interface MCPServer {
  name: string;
  transport_type: "http" | "stdio" | "unknown";
  transport: string;
  url: string | null;
  command: string | null;
  args: string[];
  enabled: boolean;
  auth_type: string;
  tools: MCPServerToolsConfig;
}

export interface MCPProbeTool {
  name: string;
  description: string;
}

export interface MCPProbeResponse {
  name: string;
  elapsed_ms: number;
  tools: MCPProbeTool[];
}

export interface GatewayPlatformIdentity {
  platform_id: string;
  enabled: boolean;
  display_name: string;
  display_name_source: "config" | "extra" | "default";
  supported: boolean;
  token_status: "configured" | "missing" | "env_referenced";
  extra_keys: string[];
}

// Operational state (KR-P2-OPS-PANEL). Enum values pinned to R4.1 §9.1
// and must match the Python OperationalState enum landed by CC#1's
// KR-P2-I-skeleton bucket.
export type PrimaryState =
  | "booting"
  | "ready"
  | "active"
  | "paused"
  | "stopped";

export type DegradationReason =
  | "cost"
  | "auth"
  | "dispatch"
  | "substrate"
  | "migration"
  | "operator"
  | "token_expiring"
  | "retry_ceiling";

export type ClaimPermission = "none" | "critical_only" | "normal";

export interface OperationalStateTransition {
  timestamp: string;
  from_state: PrimaryState;
  to_state: PrimaryState;
  trigger: string;
}

export interface ValidNextState {
  to_state: PrimaryState;
  trigger: string;
}

export interface OperationalStateResponse {
  primary_state: PrimaryState;
  claim_permission: ClaimPermission;
  degradation_reasons: DegradationReason[];
  is_degraded: boolean;
  transition_history: OperationalStateTransition[];
  valid_next_states: ValidNextState[];
  stub: boolean;
}

// Sea_Tickets — Kora-assigned viewer panel (KR-P2-SEA-PANEL).
// Enum values match the substrate Sea_Tickets schema; if the Python
// side drifts the typed shape will surface the mismatch at compile time.
export type Criticality = "low" | "normal" | "high" | "frontier";
export type ModelTier = "haiku" | "sonnet" | "opus";
export type Resolution =
  | "completed"
  | "released"
  | "failed_retryable"
  | "failed_terminal"
  | "blocked_needs_operator"
  | "deferred_cost_limit";

export interface InProgressTicket {
  id: string;
  title: string;
  criticality: Criticality;
  claimed_at: string;
  claim_count: number;
  work_attempt_count: number;
}

export interface QueuedTicket {
  id: string;
  title: string;
  criticality: Criticality;
  assigned_at: string;
  next_eligible_at: string | null;
}

export interface ResolvedTicket {
  id: string;
  title: string;
  criticality: Criticality;
  resolved_at: string;
  resolution: Resolution;
  model_tier_used: ModelTier;
}

export interface FailedOrBlockedTicket {
  id: string;
  title: string;
  criticality: Criticality;
  state: "failed_terminal" | "blocked_needs_operator";
  failure_count_by_reason: Record<string, number>;
}

export interface KoraAssignedSeaTicketsResponse {
  in_progress: InProgressTicket[];
  queued: QueuedTicket[];
  recently_resolved: ResolvedTicket[];
  failed_or_blocked: FailedOrBlockedTicket[];
  stub: boolean;
}

// kora_control runtime-observed-state (KR-P2-CONTROL-PANEL).
// Enum values match the Python KoraControl schema; future drift surfaces
// as a TS compile error.
export type KoraControlKind = "stop" | "reset";

export type KoraControlLifecycleState =
  | "created"
  | "visible_to_runtime"
  | "acknowledged"
  | "enforcing"
  | "enforced"
  | "superseded"
  | "expired"
  | "failed"
  | "escalated";

export interface KoraControlCommand {
  command_id: string;
  level: number; // 0-5 (STOP-KORA tiers; 0 = reset)
  kind: KoraControlKind;
  reason: string;
  issuer: string;
  sequence: number;
  created_at: string;
  visible_to_runtime_at: string | null;
  observed_at: string | null;
  acknowledged_at: string | null;
  enforced_at: string | null;
  lifecycle_state: KoraControlLifecycleState;
  expires_at: string | null;
  target_session: string | null;
}

export interface KoraControlObservedStateResponse {
  active: KoraControlCommand[];
  recently_enforced: KoraControlCommand[];
  history: KoraControlCommand[];
  stub: boolean;
}

// Boot status (KR-P2-BOOT-PANEL). Enum values match the Python
// BootGateRunner / GateResult schema landed by KR-P2-H.
export type GateOutcome = "pass" | "fail";
export type GateClass = "transient" | "invariant";
export type BootOutcome = "booting" | "ready" | "failed";

export interface GateResult {
  gate_id: string;
  title: string;
  gate_class: GateClass;
  outcome: GateOutcome;
  elapsed_ms: number;
  detail: string;
}

export interface CurrentBoot {
  boot_id: string;
  primary_state: string;
  started_at: string;
  completed_at: string | null;
  elapsed_ms: number;
  outcome: BootOutcome;
  gates: GateResult[];
}

export interface BootHistoryEntry {
  boot_id: string;
  started_at: string;
  completed_at: string | null;
  elapsed_ms: number;
  outcome: BootOutcome;
  failed_gate_id?: string;
  failed_gate_title?: string;
  detail?: string;
}

export interface BootStatusResponse {
  current: CurrentBoot;
  history: BootHistoryEntry[];
  stub: boolean;
}

// Cost ladder (KR-P2-COST-PANEL). Reuses ModelTier + Criticality from
// earlier panels. Enum values match the Python CostStateHolder schema
// landed by KR-P2-K (a TS drift surfaces at compile time).
export type CostRung =
  | "normal"
  | "warn_75"
  | "downshift_90"
  | "hard_stop_100";

export interface CostCurrent {
  billing_period_start: string;
  billing_period_end: string;
  days_remaining: number;
  credit_pool_usd: number;
  spent_to_date_usd: number;
  burn_rate_usd_per_day: number;
  projected_end_of_period_usd: number;
  active_rung: CostRung;
  active_rung_threshold_pct: number;
  current_pct_used: number;
  effective_model_tier: ModelTier;
  downshift_active: boolean;
  downshift_reason: string | null;
  extra_usage_off: boolean;
}

export interface RateLimitWindow {
  limit: number;
  remaining: number;
  reset_at: string;
}

export interface RateLimitPulse {
  captured_at: string;
  requests: RateLimitWindow;
  tokens: RateLimitWindow;
}

export interface DeferredTicket {
  id: string;
  title: string;
  criticality: Criticality;
  state: "deferred_cost_limit";
  deferred_at: string;
  reason: string;
}

export interface ReconciliationEntry {
  reconciled_at: string;
  local_estimator_usd: number;
  anthropic_reported_usd: number;
  delta_usd: number;
  delta_pct: number;
  within_tolerance: boolean;
}

export interface CostStateResponse {
  current: CostCurrent;
  rate_limit_pulse: RateLimitPulse;
  deferred_tickets: DeferredTicket[];
  reconciliation_history: ReconciliationEntry[];
  stub: boolean;
}

// Capabilities inspector (KR-P2-CAP-PANEL). Live read — no stub flag.
// ``unmapped_in_c2_mirror`` is the documented fail-CLOSED state per
// D-krp2a-st1-infra-tier-caps-missing-from-c2-mirror; surfaces when
// the C2 mirror doesn't yet know about a cap_* the Python side
// references. Closes when KR-P2-N extends the mirror.
export type CapVerdict =
  | "granted"
  | "denied"
  | "unmapped_in_c2_mirror"
  | "error";

export interface CapabilityGroup {
  cap_name: string;
  verdict: CapVerdict;
  tools: string[];
}

export interface CapabilitiesResponse {
  groups: CapabilityGroup[];
  substrate_tier: string[];
  total_tools: number;
  total_caps: number;
  unmapped_count: number;
}
