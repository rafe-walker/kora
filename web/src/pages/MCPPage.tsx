import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Loader2,
  Plug,
  RefreshCw,
  Server,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import type { MCPProbeTool, MCPServer } from "@/lib/api";

import { usePanelView } from "@/hooks/usePanelView";
interface ProbeState {
  loading: boolean;
  tools: MCPProbeTool[] | null;
  error: string | null;
  elapsed_ms: number | null;
}

const EMPTY_PROBE: ProbeState = {
  loading: false,
  tools: null,
  error: null,
  elapsed_ms: null,
};

function transportLabel(server: MCPServer): string {
  if (server.transport_type === "http") return `HTTP → ${server.transport}`;
  if (server.transport_type === "stdio") return `stdio → ${server.transport}`;
  return server.transport || "—";
}

function deriveEnabledSet(
  server: MCPServer,
  allTools: string[],
): Set<string> {
  const { include, exclude } = server.tools;
  if (include && include.length) {
    return new Set(include.filter((name) => allTools.includes(name)));
  }
  if (exclude && exclude.length) {
    const excludeSet = new Set(exclude);
    return new Set(allTools.filter((name) => !excludeSet.has(name)));
  }
  return new Set(allTools);
}

export default function MCPPage() {
  usePanelView("MCPPage");

  const [servers, setServers] = useState<MCPServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [probes, setProbes] = useState<Record<string, ProbeState>>({});
  const [pendingEnabled, setPendingEnabled] = useState<
    Record<string, Set<string>>
  >({});
  const [saving, setSaving] = useState<string | null>(null);
  const [togglingServer, setTogglingServer] = useState<string | null>(null);
  const { toast, showToast } = useToast();

  const loadServers = useCallback(() => {
    api
      .getMCPServers()
      .then(setServers)
      .catch((e) => showToast(`Failed to load MCP servers: ${e}`, "error"))
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => {
    loadServers();
  }, [loadServers]);

  const runProbe = useCallback(
    async (server: MCPServer) => {
      setProbes((prev) => ({
        ...prev,
        [server.name]: { ...EMPTY_PROBE, loading: true },
      }));
      try {
        const resp = await api.probeMCPServer(server.name);
        const allTools = resp.tools.map((t) => t.name);
        setProbes((prev) => ({
          ...prev,
          [server.name]: {
            loading: false,
            tools: resp.tools,
            error: null,
            elapsed_ms: resp.elapsed_ms,
          },
        }));
        setPendingEnabled((prev) => ({
          ...prev,
          [server.name]: deriveEnabledSet(server, allTools),
        }));
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setProbes((prev) => ({
          ...prev,
          [server.name]: {
            loading: false,
            tools: null,
            error: msg,
            elapsed_ms: null,
          },
        }));
      }
    },
    [],
  );

  const toggleExpanded = useCallback(
    (name: string) => {
      setExpanded((prev) => (prev === name ? null : name));
    },
    [],
  );

  const toggleServerEnabled = useCallback(
    async (server: MCPServer) => {
      setTogglingServer(server.name);
      try {
        const updated = server.enabled
          ? await api.disableMCPServer(server.name)
          : await api.enableMCPServer(server.name);
        setServers((prev) =>
          prev.map((s) => (s.name === server.name ? updated : s)),
        );
        showToast(
          `${server.name} ${updated.enabled ? "enabled" : "disabled"}`,
          "success",
        );
      } catch (e) {
        showToast(`Failed to toggle ${server.name}: ${e}`, "error");
      } finally {
        setTogglingServer(null);
      }
    },
    [showToast],
  );

  const toggleTool = useCallback((serverName: string, toolName: string) => {
    setPendingEnabled((prev) => {
      const current = new Set(prev[serverName] ?? []);
      if (current.has(toolName)) current.delete(toolName);
      else current.add(toolName);
      return { ...prev, [serverName]: current };
    });
  }, []);

  const saveToolGating = useCallback(
    async (server: MCPServer) => {
      const probe = probes[server.name];
      const enabledSet = pendingEnabled[server.name];
      if (!probe?.tools || !enabledSet) return;

      const allTools = probe.tools.map((t) => t.name);
      const enabledTools = allTools.filter((n) => enabledSet.has(n));

      setSaving(server.name);
      try {
        const updated = await api.setMCPServerTools(server.name, {
          enabled_tools: enabledTools,
          all_tools: allTools,
        });
        setServers((prev) =>
          prev.map((s) => (s.name === server.name ? updated : s)),
        );
        showToast(
          `Saved ${enabledTools.length}/${allTools.length} tools for ${server.name}`,
          "success",
        );
      } catch (e) {
        showToast(`Failed to save: ${e}`, "error");
      } finally {
        setSaving(null);
      }
    },
    [pendingEnabled, probes, showToast],
  );

  const isDirty = useMemo(
    () => (server: MCPServer) => {
      const probe = probes[server.name];
      const enabled = pendingEnabled[server.name];
      if (!probe?.tools || !enabled) return false;
      const allTools = probe.tools.map((t) => t.name);
      const baseline = deriveEnabledSet(server, allTools);
      if (baseline.size !== enabled.size) return true;
      for (const name of baseline) if (!enabled.has(name)) return true;
      return false;
    },
    [pendingEnabled, probes],
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-center justify-between">
        <H2>MCP Servers</H2>
        <Button size="sm" ghost onClick={loadServers}>
          <RefreshCw className="h-3 w-3" />
          Refresh
        </Button>
      </div>

      {servers.length === 0 && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No MCP servers configured. Add one with{" "}
            <code className="rounded bg-muted px-1.5 py-0.5">
              hermes mcp add &lt;name&gt; --url &lt;endpoint&gt;
            </code>
            .
          </CardContent>
        </Card>
      )}

      {servers.map((server) => {
        const isExpanded = expanded === server.name;
        const probe = probes[server.name] ?? EMPTY_PROBE;
        const enabledSet = pendingEnabled[server.name];
        const dirty = isDirty(server);

        return (
          <Card key={server.name}>
            <CardContent className="flex flex-col gap-3 py-3">
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => toggleExpanded(server.name)}
                  className="flex items-center gap-2 text-left flex-1 min-w-0"
                  aria-expanded={isExpanded}
                >
                  {isExpanded ? (
                    <ChevronDown className="h-4 w-4 shrink-0" />
                  ) : (
                    <ChevronRight className="h-4 w-4 shrink-0" />
                  )}
                  <Server className="h-4 w-4 shrink-0 text-primary" />
                  <span className="font-medium truncate">{server.name}</span>
                  <span className="text-xs text-muted-foreground truncate hidden sm:inline">
                    {transportLabel(server)}
                  </span>
                </button>

                <Badge tone={server.enabled ? "success" : "outline"}>
                  {server.tools.summary}
                </Badge>

                <div className="flex items-center gap-2">
                  <Switch
                    checked={server.enabled}
                    disabled={togglingServer === server.name}
                    onCheckedChange={() => toggleServerEnabled(server)}
                    aria-label={`${server.enabled ? "Disable" : "Enable"} ${server.name}`}
                  />
                </div>
              </div>

              {isExpanded && (
                <div className="border-t pt-3 flex flex-col gap-3">
                  <div className="text-xs text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
                    <span>Transport: {transportLabel(server)}</span>
                    <span>Auth: {server.auth_type}</span>
                  </div>

                  {probe.tools === null && !probe.loading && !probe.error && (
                    <Button size="sm" onClick={() => runProbe(server)}>
                      <Plug className="h-3 w-3" />
                      Connect & list tools
                    </Button>
                  )}

                  {probe.error && !probe.loading && (
                    <Button
                      size="sm"
                      outlined
                      onClick={() => runProbe(server)}
                    >
                      <Plug className="h-3 w-3" />
                      Retry
                    </Button>
                  )}

                  {probe.loading && (
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Connecting to {server.name}…
                    </div>
                  )}

                  {probe.error && (
                    <div className="flex items-start gap-2 text-sm text-destructive">
                      <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                      <div>
                        <div className="font-medium">Connection failed</div>
                        <div className="text-xs opacity-80 break-words">
                          {probe.error}
                        </div>
                      </div>
                    </div>
                  )}

                  {probe.tools && probe.tools.length > 0 && enabledSet && (
                    <div className="flex flex-col gap-2">
                      <div className="flex items-center justify-between text-xs text-muted-foreground">
                        <span>
                          {enabledSet.size}/{probe.tools.length} tools enabled
                          {probe.elapsed_ms !== null && (
                            <> · probed in {probe.elapsed_ms}ms</>
                          )}
                        </span>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            className="underline disabled:opacity-50"
                            disabled={!dirty}
                            onClick={() =>
                              setPendingEnabled((prev) => ({
                                ...prev,
                                [server.name]: deriveEnabledSet(
                                  server,
                                  probe.tools!.map((t) => t.name),
                                ),
                              }))
                            }
                          >
                            Reset
                          </button>
                          <Button
                            size="sm"
                            disabled={!dirty || saving === server.name}
                            onClick={() => saveToolGating(server)}
                          >
                            {saving === server.name ? "Saving…" : "Save"}
                          </Button>
                        </div>
                      </div>

                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-96 overflow-y-auto pr-2">
                        {probe.tools.map((tool) => {
                          const isEnabled = enabledSet.has(tool.name);
                          return (
                            <div
                              key={tool.name}
                              className="flex flex-col gap-0.5"
                            >
                              <Checkbox
                                id={`${server.name}__${tool.name}`}
                                checked={isEnabled}
                                onChange={() =>
                                  toggleTool(server.name, tool.name)
                                }
                                label={
                                  <span className="font-mono text-xs">
                                    {tool.name}
                                  </span>
                                }
                              />
                              {tool.description && (
                                <div className="ml-6 text-xs text-muted-foreground line-clamp-2">
                                  {tool.description}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {probe.tools && probe.tools.length === 0 && (
                    <div className="text-sm text-muted-foreground">
                      Server connected but reports no tools.
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
