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
  getRecentEmail: () =>
    fetchJSON<EmailResponse>("/api/email/recent"),
  getRecentReasoning: () =>
    fetchJSON<ReasoningResponse>("/api/reasoning/recent"),
  getCurrentAlerts: () =>
    fetchJSON<AlertsResponse>("/api/alerts/current"),
  // KR-FE-DASHBOARD-SNAPSHOT-WIRE: $0-cost daemon snapshot read.
  // Returns either the snapshot dict OR an unavailable marker
  // ({error: "no_snapshot", stale: true}) when the daemon hasn't
  // produced a fresh snapshot. Callers branch on `"error" in resp`.
  getSnapshot: () =>
    fetchJSON<SnapshotResponse | SnapshotUnavailable>("/api/snapshot"),
  // KR-FE-COST-TELEMETRY-PANEL: live read for the cost-telemetry
  // page's Lifetime window + the Force-refresh paths on the other
  // two windows (snapshot covers rolling_24h + monthly only).
  getCostTelemetry: () =>
    fetchJSON<CostTelemetryResponse>("/api/cost_telemetry"),
  // KR-FE-PHRASEBOOK-VIEWER: read-only phrasebook + live regex tester.
  getSlackDmPhrasebook: () =>
    fetchJSON<PhrasebookResponse>("/api/phrasebook/slack_dm"),
  testSlackDmPhrasebook: (text: string) =>
    fetchJSON<PhrasebookTestResponse>("/api/phrasebook/slack_dm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  // KR-FE-PHRASEBOOK-EDITOR-AND-CRUD — write path (PUT + revert
  // + backups list). Server returns 422 with per-entry errors
  // when validation fails; fetchJSON throws on 422, callers
  // catch + parse the JSON body for the structured errors.
  putSlackDmPhrasebook: (entries: PhrasebookEntryWrite[]) =>
    fetchJSON<PhrasebookPutResponse>("/api/phrasebook/slack_dm", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries }),
    }),
  revertSlackDmPhrasebook: (filename?: string | null) =>
    fetchJSON<PhrasebookRevertResponse>(
      "/api/phrasebook/slack_dm/revert",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(filename ? { filename } : {}),
      },
    ),
  getSlackDmPhrasebookBackups: () =>
    fetchJSON<PhrasebookBackupsResponse>("/api/phrasebook/slack_dm/backups"),
  // KR-FE-EMAIL-INTENT-LOG-PANEL — read the audit JSONL filtered
  // to seam=intent.email_to_sea_ticket. Endpoint pre-aggregates
  // by-action counts + daily-created sparkline points so the
  // panel can render without re-computing client-side.
  getEmailIntentEventsRecent: (limit?: number) => {
    const qs = limit !== undefined ? `?limit=${limit}` : "";
    return fetchJSON<EmailIntentEventsResponse>(
      `/api/email-intent/recent${qs}`,
    );
  },
  // KR-FE-OUTBOUND-EMAIL-LOG-PANEL — read the audit JSONL filtered
  // to seam=tool.email_to_operator_sent. Symmetric to
  // getEmailIntentEventsRecent (the inbound-direction panel).
  // Endpoint pre-aggregates by-status counts + daily-sent
  // sparkline points so the panel can render without re-computing
  // client-side.
  getOutboundEmailRecent: (limit?: number) => {
    const qs = limit !== undefined ? `?limit=${limit}` : "";
    return fetchJSON<OutboundEmailEventsResponse>(
      `/api/outbound-email/recent${qs}`,
    );
  },
  // KR-FE-AUTOFIX-LOG-PANEL — read the audit JSONL filtered to
  // seam=tool.probe_autofix_attempted. Endpoint pre-aggregates
  // by-status counts + daily-attempted sparkline.
  getProbeAutofixRecent: (limit?: number) => {
    const qs = limit !== undefined ? `?limit=${limit}` : "";
    return fetchJSON<ProbeAutofixEventsResponse>(
      `/api/probe-autofix/recent${qs}`,
    );
  },
  // KR-FE-KORA-ACTIONS-AGGREGATED-PANEL — apex "what did Kora do"
  // chronological timeline joining all mutating-action seams.
  getKoraActionsRecent: (limit?: number) => {
    const qs = limit !== undefined ? `?limit=${limit}` : "";
    return fetchJSON<KoraActionsResponse>(
      `/api/kora-actions/recent${qs}`,
    );
  },
  // KR-FE-PROBE-INVESTIGATION-VIEWER: joined wake → reasoning → DM
  // xref panel. Window: 24h | 7d | all. limit: 1-200 (server caps).
  // V2 (KR-FE-PROBE-INVESTIGATION-VIEWER-V2): the response now also
  // carries the probe.investigation_completed projection + slack_dm
  // outbound projection joined by caller_session_id (BE extension
  // landed in the same bucket).
  getProbeInvestigations: (opts?: {
    window?: "24h" | "7d" | "all";
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (opts?.window) qs.set("window", opts.window);
    if (opts?.limit !== undefined) qs.set("limit", String(opts.limit));
    const q = qs.toString();
    return fetchJSON<ProbeInvestigationsResponse>(
      `/api/probe-investigations${q ? "?" + q : ""}`,
    );
  },
  // KR-FE-PROMOTION-REVIEW-PANEL — list pending phrasebook promotion
  // proposals (sorted highest-confidence first by the BE).
  getPhrasebookPromotionProposals: () =>
    fetchJSON<PromotionProposalsResponse>(
      "/api/promotions/phrasebook/pending",
    ),
  // Approve a pending proposal. Optional override payload fields:
  // pattern_override / reply_template_override / category_override
  // / review_notes. Empty body = approve as-proposed.
  approvePhrasebookPromotion: (
    proposalId: string,
    overrides?: PromotionApproveOverrides,
  ) =>
    fetchJSON<PromotionApproveResponse>(
      `/api/promotions/phrasebook/${encodeURIComponent(proposalId)}/approve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(overrides ?? {}),
      },
    ),
  // Reject a pending proposal. ``review_notes`` is recorded verbatim
  // in the promotion.rejected audit row (operator-decision-relevant).
  rejectPhrasebookPromotion: (proposalId: string, reviewNotes: string) =>
    fetchJSON<PromotionRejectResponse>(
      `/api/promotions/phrasebook/${encodeURIComponent(proposalId)}/reject`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ review_notes: reviewNotes }),
      },
    ),
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

// Email inbox/outbox lens (KR-EMAIL-PANEL).
// 4-layer SECURITY CONTRACT (extending the established pattern with
// an email-specific token sweep):
//   1. from_label / to_label are LABELS (joshua / kora /
//      unknown_sender) — NEVER raw email addresses. Backend tests
//      enforce; FE renders verbatim.
//   2. message_id is a STUB label in v1; real Purelymail IDs must
//      be hashed/truncated when CC#1 flips real data (PII-adjacent).
//   3. body_text_truncated_400 is rendered as PLAIN TEXT — React's
//      default child escaping defangs HTML/markdown/script. FE
//      must NEVER use dangerouslySetInnerHTML for the body. Real
//      HTML rendering happens in the Purelymail web client, NOT
//      here. has_html is metadata only.
//   4. Walk-the-whole-payload guard for Purelymail token shapes +
//      HMAC secret shapes (32/64-char hex) + bearer token shapes —
//      pinned by the backend tests.
export type EmailDirection = "inbound" | "outbound";

export type EmailHandledStatus =
  | "received"
  | "sent_ok"
  | "sent_failed"
  | "filtered_non_allowlist"
  | "filtered_wrong_recipient"
  | "dropped_paused"
  | "handler_error";

export interface EmailMessage {
  id: string;
  direction: EmailDirection;
  timestamp: string;
  message_id: string; // STUB label in v1; hashed/truncated in real
  from_label: string; // label only — never a raw email address
  to_label: string; // label only — never a raw email address
  subject: string;
  body_text_truncated_400: string; // plain-text only; capped at API
  has_html: boolean; // metadata; FE never renders the HTML body
  attachments_count: number;
  handled_status: EmailHandledStatus;
  spoofing_warning?: boolean; // inbound: DMARC/SPF red flag
  in_reply_to?: string; // outbound: references inbound message_id
}

export interface EmailResponse {
  messages: EmailMessage[];
  stub: boolean;
  generated_at: string;
  total_recent_24h: number;
  by_direction_24h: Record<EmailDirection, number>;
  by_status_24h: Record<string, number>;
}

// Kora reasoning activity lens (KR-REASONING-PANEL).
// 4-layer SECURITY CONTRACT (extending the established pattern
// with reasoning-specific guards):
//   1. response_text_truncated_200 rendered as PLAIN TEXT — React's
//      default child escaping defangs any HTML / markdown / script
//      in Kora's generated text. FE pins via dangerouslySetInnerHTML
//      grep. Real responses may contain anything the model emits.
//   2. NO Anthropic-key shapes (sk-ant- prefix) anywhere in payload
//      — backend test sweeps. There are no token fields on this
//      type; the walk-payload guard catches a future log-entry edit
//      that leaks credential material into the operator view.
//   3. NO PII (email regex / Slack user-ID regex) leaked from the
//      inbound user's message into response_text_truncated_200 —
//      backend test sweeps the response field.
//   4. This TS type enforces shape; no raw_prompt / auth_token /
//      response_html fields exist on ReasoningCall.

// CostRung.value wire strings per agent/cost_state_holder.py:114-117.
// The enum class members are uppercase NAMES (NORMAL, WARN_75, etc.)
// but the wire format / FE pill-color map keys on the lowercase
// `.value` strings — that's what real CC#3 data will emit.
export type ReasoningCostRung =
  | "normal"
  | "warn_75"
  | "downshift_90"
  | "hard_stop_100"
  | "unknown";

export type ReasoningStatus = "ok" | "failed" | "halted" | "paused";

// Model strings match kora_cli/reasoning/anthropic_engine.py's
// cost-ladder model selection.
export type ReasoningModel =
  | "claude-opus-4-7"
  | "claude-sonnet-4-6"
  | "claude-haiku-4-5-20251001";

export interface ReasoningCall {
  id: string;
  triggered_by: string; // "slack_dm" only in v1; future: email/mcp/cron
  started_at: string;
  duration_ms: number;
  model_used: ReasoningModel | null; // null when halted (no SDK call)
  cost_rung_at_call: ReasoningCostRung;
  input_tokens: number;
  output_tokens: number;
  status: ReasoningStatus;
  // ReasoningEngine error code taxonomy (PR #126):
  // sdk_auth | sdk_rate_limited | sdk_5xx | sdk_4xx_<code> |
  // sdk_timeout | sdk_transport | sdk_unknown_<class> |
  // cost_ladder_halted | operational_state_paused |
  // response_projection_failed
  error_code: string | null;
  // Plain-text response excerpt capped at 200 chars at the API
  // edge. FE renders verbatim — NEVER via dangerouslySetInnerHTML.
  response_text_truncated_200: string | null;
}

export interface ReasoningResponse {
  calls: ReasoningCall[];
  stub: boolean;
  generated_at: string;
  total_recent_24h: number;
  // Keys are model strings PLUS "halted_no_model" for the halted
  // bucket (where model_used is null).
  by_model_24h: Record<string, number>;
  by_status_24h: Record<string, number>;
  tokens_total_24h: { input: number; output: number };
}

// Unified operator-attention lens (KR-ALERTS-PANEL).
// 3-layer SECURITY CONTRACT:
//   1. title + detail rendered as PLAIN TEXT — React's default
//      child escaping defangs HTML/markdown/script. FE pins via
//      dangerouslySetInnerHTML grep. Real alert text may
//      eventually quote source-panel state.
//   2. NO PII / secret patterns: backend tests sweep payload for
//      Anthropic key shapes, Slack tokens, email addresses, raw
//      Slack user IDs. Defense-in-depth.
//   3. This TS type enforces shape; no raw_payload / user_message
//      companion fields exist on Alert.
export type AlertSeverity = "critical" | "warning" | "info";

// Open enum: backend may add new categories without breaking the FE.
// Known categories drive specific icons; unknown values fall back to
// a generic AlertTriangle icon.
export type AlertCategory =
  | "cost_ladder"
  | "operational_state"
  | "webhook_dead_letter"
  | "agent_capability_denied"
  | "reasoning_halted"
  | "service_unhealthy"
  | "boot_gate_failure"
  | string;

export interface Alert {
  id: string;
  severity: AlertSeverity;
  category: AlertCategory;
  title: string; // plain text
  detail: string; // plain text
  source_panel: string; // short id (cost / ops / webhook_events / ...)
  source_panel_route: string; // FE route to navigate to
  first_seen_at: string;
}

export interface AlertsResponse {
  alerts: Alert[];
  stub: boolean;
  generated_at: string;
  total_active: number;
  by_severity: Record<AlertSeverity, number>;
}

// Daemon-state snapshot (KR-FE-DASHBOARD-SNAPSHOT-WIRE / backed by
// kora_cli/snapshot/state_snapshot.py). $0-cost read for the
// dashboard's first-paint path. Individual sub-fields degrade to
// "unknown" rather than failing the whole snapshot (fail-soft).
export interface SnapshotResponse {
  schema_version: number;
  computed_at: string; // ISO timestamp; FE uses for freshness badge
  operational_state: {
    primary: string;
    paused: boolean;
    pause_reason: string | null;
  };
  alerts: {
    active_count: number;
    by_severity: { critical: number; warning: number; info: number };
    by_category: Record<string, number>;
  };
  // cost_ladder: schema v3 added spent_to_date_usd + credit_pool_usd
  // (PR #169). Both are number | "unknown" — degraded fields surface
  // as the "unknown" string literal so consumers can branch on
  // presence (KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED uses this to
  // decide whether to project from snapshot or fan-out).
  cost_ladder: {
    current_tier: string;
    monthly_budget_pct_used: number | null;
    model_default: string;
    spent_to_date_usd: number | "unknown";
    credit_pool_usd: number;
  };
  service_health: {
    supabase: string;
    fly: string;
    vercel: string;
    sentry: string;
    doppler: string;
  };
  // daemon_health: schema v4 (PR #170). Kora's own runtime health,
  // distinct from service_health (SaaS-deps). Per-listener detail
  // for last_event_at + consecutive_errors is "unknown" in v1 —
  // wire shape is forward-compat for KR-LISTENER-DETAIL-ACCESSORS.
  daemon_health?: {
    overall_status: "healthy" | "degraded" | "unhealthy" | "unknown";
    boot_at: string | "unknown";
    uptime_seconds: number | "unknown";
    listeners: Record<string, {
      status: "up" | "down" | "unknown";
      last_event_at: string | "unknown";
      consecutive_errors: number | "unknown";
    }>;
    recent_error_count_5min: number;
  };
  // tasks: schema v5 (KR-SNAPSHOT-TASKS) — open_count + in_progress_count
  // populated from the IsoKron Sea_Tickets provider. Throttled refresh
  // (every 30 min) preserves the $0-LLM premise of the snapshot. Both
  // fields stay "unknown" before the first successful provider read
  // (early-boot, daemon without gateway, or provider-side error). FE
  // surfaces tasks panel from this — values are not stub.
  tasks?: {
    open_count: number | "unknown";
    in_progress_count: number | "unknown";
  };
  // KR-CHEAP-COST-TELEMETRY (schema_version 2): snapshot carries the
  // rolling_24h + monthly windows. process_lifetime stays endpoint-
  // only (operator hits /api/cost_telemetry for that) so the on-disk
  // snapshot stays bounded.
  cost_telemetry?: {
    rolling_24h: Record<string, RouteCounters>;
    monthly: Record<string, RouteCounters>;
  };
}

export interface SnapshotUnavailable {
  error: "no_snapshot";
  stale: true;
}

// Per-route cost counters — mirrors kora_cli/telemetry/cost_telemetry.py
// _RouteCounters.to_dict at the wire. All counters initialize to 0
// so a route with no traffic still emits a complete row (operator
// can tell "reserved-route, no consumer yet" from missing data).
export interface RouteCounters {
  calls_count: number;
  input_tokens_total: number;
  output_tokens_total: number;
  cache_read_tokens_total: number;
  cache_creation_tokens_total: number;
  cost_estimate_usd_total: number;
  escalation_count: number;
  model_breakdown: Record<string, number>;
}

// KR-FE-COST-TELEMETRY-PANEL: three-window source-of-truth for any
// cost-economy decisions. process_lifetime is endpoint-only; the
// rolling_24h + monthly windows are also in the snapshot ($0 path).
export interface CostTelemetryResponse {
  process_lifetime: Record<string, RouteCounters>;
  rolling_24h: Record<string, RouteCounters>;
  monthly: Record<string, RouteCounters>;
}

// DM phrasebook (KR-FE-PHRASEBOOK-VIEWER). Source-of-truth shape
// mirrors kora_cli/short_circuit/dm_phrasebook.py PhrasebookEntry.
// The endpoint adds referenced_snapshot_fields so the FE can
// visualize per-entry snapshot-field dependencies without
// re-parsing reply_template client-side.
export interface PhrasebookEntryDto {
  pattern: string; // source regex string (Python re.IGNORECASE)
  category: string;
  description: string;
  reply_template: string;
  referenced_snapshot_fields: string[];
}

export interface PhrasebookResponse {
  source: "override" | "bundled_default";
  source_path: string;
  // Echoed even when absent so operator knows where to put a YAML
  // to start overriding. null when KORA_HOME isn't resolvable.
  override_candidate_path: string | null;
  entries: PhrasebookEntryDto[];
}

export type PhrasebookTestResponse =
  | {
      matched: false;
      would_fall_through_to_reasoning_engine: true;
    }
  | {
      matched: true;
      category: string;
      description: string;
      pattern: string;
      reply_template: string;
      referenced_snapshot_fields: string[];
      // null when snapshot is missing/stale OR any referenced
      // field is "unknown" — in both cases, would_fall_through is
      // true and the live DM handler would defer to the
      // reasoning engine.
      rendered_reply: string | null;
      would_fall_through_to_reasoning_engine: boolean;
      snapshot_present: boolean;
    };

// KR-FE-PHRASEBOOK-EDITOR-AND-CRUD — write-path types.
// The PUT request body's per-entry shape (operator's draft —
// `referenced_snapshot_fields` is derived server-side and not
// part of the write).
export interface PhrasebookEntryWrite {
  pattern: string;
  category: string;
  description: string;
  reply_template: string;
}

// PUT response: echoes the saved entries + reports backup +
// rotation outcome so the cockpit can refresh local state from
// the response without a follow-up GET.
export interface PhrasebookPutResponse {
  source_path: string;
  entry_count: number;
  backup_filename: string | null;
  rotated_backup_count: number;
  entries: PhrasebookEntryDto[];
}

// PUT 422 body — when validation fails, server returns this
// structured shape per offending field. The cockpit unmarshals
// it from the thrown fetchJSON error to render per-row errors.
export interface PhrasebookValidationErrorEntry {
  entry_index: number; // -1 for root-level errors
  field: string; // field name or "_root"
  error: string;
}

export interface PhrasebookValidationErrorBody {
  error: "validation_failed";
  errors: PhrasebookValidationErrorEntry[];
}

// POST /revert response. reverted_to is the backup filename
// restored, OR the literal "bundled_default" when no backup
// existed (override was removed so the live handler falls back
// to the bundled phrasebook).
export interface PhrasebookRevertResponse {
  reverted_to: string;
  source_path: string | null;
}

// GET /backups list item.
export interface PhrasebookBackupItem {
  filename: string; // "slack_dm.YYYY-MM-DDTHH-MM-SSZ.yml"
  timestamp: string; // ISO-like, dashes-only (filename-safe)
  size_bytes: number;
  entry_count: number | null; // null when backup can't be parsed
}

export interface PhrasebookBackupsResponse {
  backups: PhrasebookBackupItem[];
  rotation_keep: number;
}

// KR-FE-EMAIL-INTENT-LOG-PANEL — audit-derived per-event shape.
// Mirrors the projection at
// kora_cli/web_server.py:_project_email_intent_audit. Per-branch
// fields are optional (writer only emits them on the matching
// branch — see kora_cli/intent/email_to_sea_ticket.py for the
// 5 _safe_audit call sites).
export type EmailIntentAction =
  | "created"
  | "logged_only"
  | "dry_run"
  | "cap_exceeded"
  | "failed"
  | "unknown";

// KR-FE-EMAIL-INTENT-LOG-PANEL — drift-guard pinned in the
// Python test against the emit_audit call sites at
// kora_cli/intent/email_to_sea_ticket.py. Exported so the panel
// can iterate filter chips in canonical order without re-typing.
export const EMAIL_INTENT_ACTION_VALUES: readonly EmailIntentAction[] = [
  "created",
  "logged_only",
  "dry_run",
  "cap_exceeded",
  "failed",
];

export interface EmailIntentEvent {
  id: string;
  emitted_at: string;
  action: EmailIntentAction;
  pattern_matched: string;
  confidence: string;
  subject: string;
  caller_session_id: string;
  // Per-branch optional fields:
  ticket_id?: string; // action=created
  tags?: string[]; // action=created
  reason?: string; // action=logged_only
  proposed_title?: string; // action=dry_run
  hourly_cap?: number | null; // action=cap_exceeded
  error?: string; // action=failed
}

export interface EmailIntentDailyCount {
  date: string; // YYYY-MM-DD UTC
  count: number;
}

export interface EmailIntentEventsResponse {
  events: EmailIntentEvent[];
  generated_at: string;
  total_recent_24h: number;
  by_action_24h: Record<string, number>;
  daily_created_14d: EmailIntentDailyCount[];
  // Echoed from the BE so the FE doesn't need to hardcode the
  // list a SECOND time — single source of truth at the wire.
  // The drift-guard test pins both BE source + FE constant.
  action_values: string[];
}

// KR-FE-OUTBOUND-EMAIL-LOG-PANEL — symmetric to EmailIntent
// types above (per the spec's "symmetric to PR #180" framing).
// Surfaces tool.email_to_operator_sent audit rows (PR #179).
//
// PRIVACY discipline: subject + body text are NOT in the audit
// payload (PR #179's hard-coded privacy posture — even subject
// is recorded only as subject_chars/length). The FE therefore
// renders sizes + status + a stable smtp_message_id or
// rejection_reason for triage. Never reconstructs text content.
export type OutboundEmailStatus =
  | "sent"
  | "rejected"
  | "smtp_failure"
  | "unknown";

// KR-FE-OUTBOUND-EMAIL-LOG-PANEL — drift-guard pinned in the
// Python test against the emit_audit STATUS_* literals at
// kora_cli/tools/email_to_operator.py. Exported so the panel
// can iterate filter chips in canonical order without re-typing.
export const OUTBOUND_EMAIL_STATUS_VALUES: readonly OutboundEmailStatus[] = [
  "sent",
  "rejected",
  "smtp_failure",
];

export interface OutboundEmailEvent {
  id: string;
  emitted_at: string;
  status: OutboundEmailStatus;
  // Privacy-preserved size indicators. ALWAYS present per
  // tools/email_to_operator.py:365-369 (initialized at the top
  // of every send path regardless of which branch terminates).
  subject_chars: number;
  body_chars: number;
  attachment_count: number;
  attachment_total_bytes: number;
  caller_session_id: string;
  // Per-status optional fields:
  smtp_message_id?: string; // status="sent"
  sent_at?: string; // status="sent"
  rejection_reason?: string; // status="rejected"
  rejection_detail?: string; // status="rejected" (JSON-serialized, ≤200 chars)
  error?: string; // status="smtp_failure"
  smtp_status?: string; // status="smtp_failure" (SendResult retry path)
}

export interface OutboundEmailDailyCount {
  date: string; // YYYY-MM-DD UTC
  count: number;
}

export interface OutboundEmailEventsResponse {
  events: OutboundEmailEvent[];
  generated_at: string;
  total_recent_24h: number;
  by_status_24h: Record<string, number>;
  daily_sent_14d: OutboundEmailDailyCount[];
  // Echoed from the BE — single source of truth at the wire,
  // pinned by the 3-source drift-guard test.
  status_values: string[];
}

// KR-FE-AUTOFIX-LOG-PANEL — audit-derived shape for
// tool.probe_autofix_attempted (PR #182). Per-status fields
// optional (writer emits them only on the matching branch —
// see kora_cli/tools/probe_autofix.py).
export type ProbeAutofixStatus =
  | "attempted"
  | "rejected"
  | "execution_failed"
  | "unknown";

export const PROBE_AUTOFIX_STATUS_VALUES: readonly ProbeAutofixStatus[] = [
  "attempted",
  "rejected",
  "execution_failed",
];

export interface ProbeAutofixDailyCount {
  date: string;
  count: number;
}

export interface ProbeAutofixEvent {
  id: string;
  emitted_at: string;
  status: ProbeAutofixStatus;
  probe: string;
  action: string;
  action_canonical?: string;
  target_id: string;
  reason_from_reasoning: string;
  caller_session_id: string;
  // attempted-branch optional fields
  action_taken?: string;
  executor_duration_ms?: number;
  before_state_label?: string;
  after_state_label?: string;
  // rejected-branch optional fields
  rejection_reason?: string;
  rejection_detail?: string;
  // execution_failed-branch optional fields
  error?: string;
}

export interface ProbeAutofixEventsResponse {
  events: ProbeAutofixEvent[];
  generated_at: string;
  total_recent_24h: number;
  by_status_24h: Record<string, number>;
  daily_attempted_14d: ProbeAutofixDailyCount[];
  status_values: string[];
}

// KR-FE-KORA-ACTIONS-AGGREGATED-PANEL — apex "what did Kora do"
// chronological timeline. Joins all mutating-action audit seams.
// Drift-guarded action_categories pinned by the kora-actions
// drift-guard test.
//
// KR-FE-KORA-ACTIONS-EXTENDED-SEAMS extension: ``promotion_proposed``
// + ``promotion_approved`` + ``promotion_rejected`` surface the
// PR #186 promotion-loop audit rows in the timeline. The
// ``investigation_completed`` row already existed (PR #184 made it
// productive — see PROBE-INVESTIGATION-DATA-COMPLETION).
export type KoraActionCategory =
  | "email_sent"
  | "sea_ticket_created"
  | "autofix_attempted"
  | "investigation_completed"
  | "phrasebook_proposal_approved"
  | "promotion_proposed"
  | "promotion_approved"
  | "promotion_rejected"
  | "other";

export const KORA_ACTION_CATEGORIES: readonly KoraActionCategory[] = [
  "email_sent",
  "sea_ticket_created",
  "autofix_attempted",
  "investigation_completed",
  "phrasebook_proposal_approved",
  "promotion_proposed",
  "promotion_approved",
  "promotion_rejected",
  "other",
];

export interface KoraActionItem {
  id: string;
  emitted_at: string;
  action_category: KoraActionCategory;
  caller_session_id: string;
  summary: string;
  status: string;
  deep_link?: string;
}

export interface KoraActionDailyCount {
  date: string;
  count: number;
}

export interface KoraActionsResponse {
  items: KoraActionItem[];
  generated_at: string;
  total_recent_24h: number;
  by_category_24h: Record<string, number>;
  daily_actions_14d: KoraActionDailyCount[];
  action_categories: string[];
}

// KR-FE-PROBE-INVESTIGATION-VIEWER — joined wake event + downstream
// reasoning + current health. Source-of-truth shape pinned by the
// backend endpoint at /api/probe-investigations.
//
// v1 deferred fields documented in v1_notes; the panel surfaces
// those notes inline so operator knows what's coming.
export type ProbeResolutionStatus = "resolved" | "active" | "unknown";

export interface ProbeReasoningToolCall {
  tool_name: string;
  triggered_by: string;
  tool_duration_ms: number;
  tool_status: string;
  emitted_at: string;
  exc_type?: string;
}

// KR-FE-PROBE-INVESTIGATION-VIEWER-V2 — operator-facing dm_status
// enum. PR #184 introduced the 4-value `dm_status` literal in the
// wake_consumer's investigation outcome ({sent, failed_send,
// engine_unavailable_fallback, engine_unavailable_failed_send}).
// The probe-investigations endpoint echoes the same values for
// chip filtering. Drift-guarded by test_probe_investigations_dm_status.
export type ProbeDmStatus =
  | "sent"
  | "failed_send"
  | "engine_unavailable_fallback"
  | "engine_unavailable_failed_send"
  | "unknown";

export const PROBE_DM_STATUS_VALUES: readonly ProbeDmStatus[] = [
  "sent",
  "failed_send",
  "engine_unavailable_fallback",
  "engine_unavailable_failed_send",
];

// KR-FE-PROBE-INVESTIGATION-VIEWER-V2 — investigation_completed
// projection from probe.investigation_completed audit (PR #184).
// Optional — null when no investigation_completed row joins this
// wake by caller_session_id (e.g. consumer wasn't running yet,
// emit_audit failed best-effort, or older wake pre-#184).
export interface ProbeInvestigationCompleted {
  emitted_at: string;
  summary_text: string;
  model_used: string | null;
  total_cost_usd: number | null;
  investigation_duration_ms: number | null;
  dm_status: ProbeDmStatus;
  autofix_attempted: boolean;
  reasoning_error: string | null;
}

// KR-FE-PROBE-INVESTIGATION-VIEWER-V2 — slack DM projection from
// slack_dm_log.jsonl entry written by the wake consumer (PR #184
// extracted append_outbound_log_entry to wire this path). Optional
// — null when no DM row joins by caller_session_id.
export interface ProbeInvestigationDmEntry {
  sent_at: string;
  send_status: string; // "ok" | "failed"
  channel_id: string;
  slack_message_ts: string | null;
  failure_reason: string | null;
}

export interface ProbeInvestigationItem {
  // Stable for FE react key — composed of wake_timestamp + probe +
  // category. Repeats of the SAME wake (debounce window) get
  // distinct ids via the timestamp component.
  wake_event_id: string;
  wake_timestamp: string;
  probe_name: string;
  issue_category: string;
  severity: string;
  title: string;
  detail: string;
  envelope_enabled: boolean;
  envelope_fix_name: string;
  caller_session_id: string;
  // null when no reasoning.tool_called rows are joined to this
  // wake's session-id (e.g. engine_unavailable fallback, or wake
  // emitted but consumer not running yet).
  investigation: {
    tool_calls: ProbeReasoningToolCall[];
    total_duration_ms: number;
    any_errored: boolean;
    call_count: number;
  } | null;
  // KR-FE-PROBE-INVESTIGATION-VIEWER-V2 additions.
  investigation_completed: ProbeInvestigationCompleted | null;
  dm_entry: ProbeInvestigationDmEntry | null;
  current_probe_health: string; // "healthy" | "degraded" | "unhealthy" | "unknown"
  resolution_status: ProbeResolutionStatus;
}

export interface ProbeInvestigationsResponse {
  window: "24h" | "7d" | "all";
  since: string | null; // null when window=all
  generated_at: string;
  total_count: number;
  active_count: number;
  resolved_count: number;
  unknown_count: number;
  current_probe_health: Record<string, string>;
  items: ProbeInvestigationItem[];
  // KR-FE-PROBE-INVESTIGATION-VIEWER-V2 — echoed allowlist of
  // dm_status values, drift-guard pinned against the FE constant.
  dm_status_values: string[];
  by_dm_status_24h: Record<string, number>;
}

// KR-FE-PROMOTION-REVIEW-PANEL — phrasebook promotion proposal
// shape (mirror of kora_cli/promote/phrasebook/proposer.py
// :PromotionProposal serialized via proposal_to_dict). The four
// status values are drift-guard pinned against the BE
// _PROMOTION_STATUS_VALUES tuple in web_server.py.
export type PromotionStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "expired";

export const PROMOTION_STATUS_VALUES: readonly PromotionStatus[] = [
  "pending",
  "approved",
  "rejected",
  "expired",
];

export interface PromotionProposal {
  proposal_id: string;
  cluster_size: number;
  sample_questions: string[];
  proposed_pattern: string;
  proposed_reply_template: string;
  proposed_category: string;
  confidence: number;
  created_at: string;
  status: PromotionStatus;
  review_notes: string;
  cluster_caller_session_ids: string[];
  haiku_synthesized: boolean;
}

export interface PromotionProposalsResponse {
  proposals: PromotionProposal[];
  // Echoed from the BE so the FE doesn't need to hardcode the list
  // a SECOND time — single source of truth at the wire. The
  // drift-guard test pins both BE source + FE constant.
  status_values: string[];
}

export interface PromotionApproveOverrides {
  pattern_override?: string;
  reply_template_override?: string;
  category_override?: string;
  review_notes?: string;
}

export interface PromotionApproveResponse {
  proposal_id: string;
  status: "approved";
  committed_entry: {
    pattern: string;
    category: string;
    description: string;
    reply_template: string;
  };
  entry_count_after: number;
  backup_filename: string | null;
}

export interface PromotionRejectResponse {
  proposal_id: string;
  status: "rejected";
  review_notes: string;
}
