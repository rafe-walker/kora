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

/**
 * Fetch a text response (no JSON parse). Used for endpoints that
 * return raw text/markdown (e.g. /api/runbooks/{id}/content) where
 * the FE renders the body directly. Mirrors fetchJSON's session-token
 * injection + non-2xx-throws behaviour.
 */
export async function fetchText(url: string, init?: RequestInit): Promise<string> {
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
  return res.text();
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
  getHealthRollup: () =>
    fetchJSON<HealthRollupResponse>("/api/health-rollup"),
  getDRState: () => fetchJSON<DRStateResponse>("/api/dr-state"),
  getCharter: () => fetchJSON<CharterResponse>("/api/charter"),
  getChainEvents: (opts?: {
    prefix?: string;
    limit?: number;
    before_ts?: string;
  }) => {
    const qs = new URLSearchParams();
    if (opts?.prefix !== undefined) qs.set("prefix", opts.prefix);
    if (opts?.limit !== undefined) qs.set("limit", String(opts.limit));
    if (opts?.before_ts) qs.set("before_ts", opts.before_ts);
    const q = qs.toString();
    return fetchJSON<ChainEventsResponse>(
      `/api/chain-events${q ? "?" + q : ""}`,
    );
  },
  getRunbooks: () => fetchJSON<RunbooksManifest>("/api/runbooks"),
  getRunbookContent: (id: string) =>
    fetchText(`/api/runbooks/${encodeURIComponent(id)}/content`),
  getHeartbeatServices: () =>
    fetchJSON<HeartbeatServicesResponse>("/api/heartbeat/services"),
  getMCPClients: () =>
    fetchJSON<MCPClientsListResponse>("/api/mcp/clients/list"),
  getRecentWebhookEvents: () =>
    fetchJSON<WebhookEventsResponse>("/api/webhooks/events/recent"),
  getRecentAgentActivity: () =>
    fetchJSON<AgentActivityResponse>("/api/agent-activity/recent"),
  getRecentSlackDM: () =>
    fetchJSON<SlackDMResponse>("/api/slack-dm/recent"),
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

// Health rollup (KR-P2-HEALTH-PANEL). R4.1 §9.7 — distinguish overall /
// control-plane / worker so operators can tell "intentionally stopped"
// from "outage" at a glance. Subsignal status pins per-axis freshness
// against R4.1's thresholds.
export type HealthStatus = "healthy" | "degraded" | "stopped" | "outage";
export type SubsignalStatus = "fresh" | "stale" | "missing" | "degraded";

// Subsignal is a union shape — different subsignals carry different
// fields (e.g. last_successful_write has elapsed_seconds; auth_validity
// has days_remaining). Keeping it as an open interface with optional
// fields lets the FE switch on subsignal key without coercing types.
export interface Subsignal {
  status: SubsignalStatus;
  value_at?: string;
  value?: string | number;
  value_pct?: number;
  threshold_seconds?: number;
  elapsed_seconds?: number;
  threshold_pct?: number;
  threshold_days?: number;
  days_remaining?: number;
  expires_at?: string;
  claim_id?: string;
  rung?: string;
}

export interface HealthRollupResponse {
  overall: HealthStatus;
  control_plane: HealthStatus;
  worker: HealthStatus;
  stopped_reason: string | null;
  subsignals: Record<string, Subsignal>;
  stub: boolean;
}

// Disaster recovery / substrate-epoch (KR-P2-DR-PANEL).
// R4.1 §9.8: detect post-PITR substrate_epoch mismatch and surface the
// runbook need. ``runbook_pending`` drives the FE's top-of-page red
// alert; it's derived from match_status + kora_paused_substrate.
export type DRMatchStatus =
  | "clean"
  | "mismatch_detected"
  | "pending_runbook"
  | "unknown";

export type EpochSource = "boot-success" | "dr-recovery" | "operator-bump";

export interface DRCurrent {
  substrate_epoch: number;
  kora_known_epoch: number | null;
  match_status: DRMatchStatus;
  last_check_at: string;
  kora_paused_substrate: boolean;
}

export interface EpochHistoryEntry {
  epoch: number;
  observed_at: string;
  kora_known_at: string | null;
  source: EpochSource;
}

export interface DRObservedEvent {
  event_type: "kora.dr.observed";
  occurred_at: string;
  from_epoch: number;
  to_epoch: number;
  discarded_operation_ids: number;
  discarded_ledger_rows: number;
  cleared_at: string | null;
  cleared_by: string | null;
}

export interface DRStateResponse {
  current: DRCurrent;
  epoch_history: EpochHistoryEntry[];
  recent_dr_events: DRObservedEvent[];
  runbook_pending: boolean;
  stub: boolean;
  // Present only when stub:true comes from the live-read failure
  // branch (KR-P2-DR-FLIP): surfaces "why" so debugging from the FE
  // toast or the network panel doesn't require a server-log dig.
  error?: string;
}

// Charter / Constitution viewer (KR-P2-CHARTER-PANEL).
// v1 fallback mode: rules[] is always empty + rules_available is always
// false (substrate doesn't expose rule content via Kora-tier read).
// When the substrate-team rule-content SECDEF lands, the same shape
// starts carrying rules — page conditionally renders the rules block.
export interface ConstitutionRule {
  rule_id: string;
  scope: string;
  description: string;
  severity: string;
}

export interface ActiveConstitution {
  revision_id: string | null;
  rules_hash: string | null;
  loaded_at: string;
  workspace_id: string;
  rules: ConstitutionRule[];
  rules_available: boolean;
}

// CharterResponse reuses CharterGroup shape but only carries the
// (cap_name, tools) projection — no per-cap verdict (CAP-PANEL's job).
export interface CharterCapabilityGroup {
  cap_name: string;
  tools: string[];
}

export interface CharterResponse {
  active: ActiveConstitution | null;
  capability_groups: CharterCapabilityGroup[];
  substrate_tier_tools: string[];
  stub: boolean;
}

// Chain events live tail (KR-P2-CHAIN-EVENTS-PANEL).
// actor_kind is nullable in v1 — requires a JOIN to actor_registry the
// backend doesn't do yet. envelope is null except for constitution
// events (carries revision_id + rules_hash for audit visibility).
export interface ChainEvent {
  event_id: string;
  event_type: string;
  actor_id: string | null;
  actor_kind: string | null;
  workspace_id: string;
  occurred_at: string;
  payload: Record<string, unknown>;
  envelope: Record<string, unknown> | null;
}

export interface ChainEventsResponse {
  events: ChainEvent[];
  next_before_ts: string | null;
  stub: boolean;
  // Present only on the stub-fallback branch (uninit/failure path),
  // mirroring the DR/COST flips. FE renders a small error banner.
  error?: string;
}

// Operator runbooks (KR-P2-RUNBOOKS-PANEL).
// available=false entries surface as "[runbook pending]" placeholders
// (file documented in the manifest but not yet authored / not vendored
// into this deploy). The FE conditionally renders the placeholder
// card vs the live markdown content.
export interface RunbookEntry {
  id: string;
  title: string;
  path: string;
  available: boolean;
  size_bytes: number | null;
  last_modified: string | null;
}

export interface RunbooksManifest {
  runbooks: RunbookEntry[];
}

// Diagnostic bundle download URL (KR-P2-DIAG-BUNDLE).
// Browser handles the zip download directly via <a href download>;
// no JS fetcher needed — the response is a binary stream with
// Content-Disposition: attachment. Exposed via a helper so the
// HERMES_BASE_PATH (URL-prefix reverse-proxy mount) is applied
// the same way fetchJSON applies it for /api/* fetches.
export const DIAG_BUNDLE_URL = "/api/diag-bundle";

export function diagBundleHref(): string {
  return `${HERMES_BASE_PATH}${DIAG_BUNDLE_URL}`;
}

// Backend service heartbeat (KR-HB-PANEL).
// status enum: healthy | degraded | unhealthy. Per-service "details"
// shape varies (Sentry has unresolved_issues, Supabase has
// connections_pct, etc.) — surfaced as an opaque Record so each FE
// renderer can read the keys it knows about; unknown keys render as
// plain key/value pairs.
// KR-FEAT-HEARTBEAT ST2: the live probe path adds "unknown" status
// (auth-env missing / probe timeout / probe-loop crash). FE renders
// this as a yellow "no data" badge — distinct from "unhealthy"
// (active failure) so operators don't mistake a configuration gap
// for a real outage.
export type HeartbeatStatus =
  | "healthy"
  | "degraded"
  | "unhealthy"
  | "unknown";

export interface HeartbeatService {
  name: string;
  status: HeartbeatStatus;
  // last_check_at is nullable because an "unknown" snapshot from a
  // probe that never completed a roundtrip has no meaningful
  // timestamp; FE renders "—" in that case.
  last_check_at: string | null;
  // Likewise nullable — auth-missing / timeout cases never measure
  // latency.
  latency_ms: number | null;
  details: Record<string, unknown>;
  // Operator-readable failure string (sanitized — never includes the
  // auth token). Null on healthy paths.
  error: string | null;
}

export interface HeartbeatServicesResponse {
  services: HeartbeatService[];
  generated_at: string;
  stub: boolean;
  // KR-FEAT-HEARTBEAT ST2: ``true`` when the snapshot cache is empty
  // (daemon just started; first probe cycle hasn't completed yet).
  // FE renders "Probes warming up..." instead of an empty state +
  // suppresses any "all services down" alerting heuristic until the
  // first cycle lands.
  cache_warming: boolean;
}

// MCP client picker (KR-MCP-3) — Kora-as-MCP-client surface.
// Distinct from the existing MCPServer types (KR-P2-C ST2) which
// describe Kora-as-MCP-server admin state. SECURITY CONTRACT: the
// shape carries auth_token_env (variable NAME only) +
// auth_token_present (bool); never the token VALUE. The FE renders
// presence/absence only — never expose values in tooltips, copy
// buttons, dev-console, or anywhere else.
export type MCPClientTransport = "stdio" | "streamable_http";
export type MCPClientStatus =
  | "connected"
  | "configured_but_unconnected"
  | "error"
  | "unhealthy";

export interface MCPClient {
  name: string;
  transport: MCPClientTransport;
  endpoint: string;
  status: MCPClientStatus;
  auth_token_env: string;
  auth_token_present: boolean;
  allowed_tools_regex: string | null;
  tools_count: number | null;
  // KR-MCP-CONSUMPTION ST2 additive fields. The daemon's heartbeat
  // scheduler probes each endpoint every
  // KORA_MCP_HEALTH_CHECK_INTERVAL_SEC (default 300s); these
  // capture the last cycle's result.
  //   last_check_at: ISO string when the snapshot was taken
  //                  (null if no cycle has run yet for this endpoint).
  //   last_error:    operator-readable failure string from the last
  //                  cycle (null on success / no snapshot).
  // FE rendering of these fields lands as a small follow-on bucket
  // (KR-MCP-CLIENTS-HEALTH-DISPLAY) on CC#2's lane.
  last_check_at: string | null;
  last_error: string | null;
}

export interface MCPClientsListResponse {
  clients: MCPClient[];
  stub: boolean;
  generated_at: string;
}

// Webhook events lens (KR-WEBHOOK-EVENTS-PANEL).
// SECURITY CONTRACT: source_ip is OCTET-MASKED on the wire
// (e.g. "54.203.x.x", never "54.203.99.142"). The TS type is just
// `string` — the backend enforces the mask shape and a backend
// regex test asserts it. FE renders source_ip verbatim from the
// wire; never reconstructs or de-masks.
export type WebhookEventStatus =
  | "verified"
  | "dead_letter"
  | "rate_limited"
  | "handler_error";

export interface WebhookEvent {
  id: string;
  endpoint: string;
  received_at: string;
  status: WebhookEventStatus;
  source_ip: string; // octet-masked per backend contract
  event_type: string | null;
  details: Record<string, unknown>;
}

export interface WebhookEventsResponse {
  events: WebhookEvent[];
  stub: boolean;
  generated_at: string;
  total_recent_24h: number;
}

// Agent activity lens (KR-AGENT-ACTIVITY-PANEL).
// SECURITY CONTRACT (3-layer, same shape as KR-MCP-3 / WEBHOOK-EVENTS):
//   * result_summary is a SHORT TEXTUAL summary — never raw JSON
//     payloads. Backend tests enforce; FE renders verbatim.
//   * caller_actor_kind is a LABEL (claude_pm / kora_drone_N / etc.)
//     — never bearer-token-shaped or token-hash-shaped. Backend
//     tests enforce against base64/hex patterns.
//   * This TS type is the third enforcement layer — fields are
//     declared as plain strings with the wire-contract documented;
//     no separate "raw_payload" or "auth_token" fields exist.
export type AgentCallStatus =
  | "ok"
  | "capability_denied"
  | "denied_prod_only"
  | "tool_not_found"
  | "handler_error"
  | "timeout";

export interface AgentCall {
  id: string;
  tool_name: string;
  caller_actor_kind: string; // label only — never a token or hash
  called_at: string;
  duration_ms: number;
  status: AgentCallStatus;
  result_summary: string; // textual summary — never raw JSON
}

export interface AgentActivityResponse {
  calls: AgentCall[];
  stub: boolean;
  generated_at: string;
  total_recent_24h: number;
  by_caller_24h: Record<string, number>;
}

// Slack DM conversation lens (KR-SLACK-DM-PANEL).
// SECURITY CONTRACT (4-layer, builds on the established panel pattern):
//   1. user_id_label is a LABEL (joshua / kora_bot / unknown_user)
//      — never a raw Slack user ID (U[A-Z0-9]{8,} shape). Backend
//      tests enforce; FE renders verbatim.
//   2. channel_id is a STUB label in v1 (D_STUB1, D_STUB2). Real
//      channel IDs must be hashed/truncated when CC#3 flips real
//      data (PII-adjacent). Backend tests pin the stub-shape.
//   3. text content is rendered as PLAIN TEXT — React's default
//      child escaping defangs any HTML/markdown/script. FE must
//      never use dangerouslySetInnerHTML for message bodies.
//   4. Walk-the-whole-payload guard against xoxb-/xoxp-/Slack
//      signing-secret token shapes — backend test sweeps the
//      serialized response.
export type SlackDMDirection = "inbound" | "outbound";

export type SlackDMHandledStatus =
  | "received"
  | "sent_ok"
  | "sent_failed"
  | "filtered_non_joshua"
  | "filtered_bot"
  | "filtered_subtype"
  | "handler_error"
  | "dropped_paused";

export interface SlackDMMessage {
  id: string;
  direction: SlackDMDirection;
  timestamp: string;
  channel_id: string; // STUB label in v1; hashed/truncated in real
  thread_ts: string | null;
  user_id_label: string; // label only — never a raw U... Slack ID
  text: string; // rendered as plain text by the FE
  handled_status: SlackDMHandledStatus;
}

export interface SlackDMResponse {
  messages: SlackDMMessage[];
  stub: boolean;
  generated_at: string;
  total_recent_24h: number;
  by_direction_24h: Record<SlackDMDirection, number>;
  by_status_24h: Record<string, number>;
}
