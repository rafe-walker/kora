import { useCallback, useEffect, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  HelpCircle,
  KeyRound,
  Network,
  Plug,
  PowerOff,
  RefreshCw,
  Terminal,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import type {
  MCPClient,
  MCPClientStatus,
  MCPClientsListResponse,
  MCPClientTransport,
} from "@/lib/api";

const STATUS_TONE: Record<MCPClientStatus, "success" | "warning" | "destructive" | "outline"> = {
  connected: "success",
  configured_but_unconnected: "outline",
  unhealthy: "warning",
  error: "destructive",
};

const STATUS_LABEL: Record<MCPClientStatus, string> = {
  connected: "connected",
  configured_but_unconnected: "configured · unconnected",
  unhealthy: "unhealthy",
  error: "error",
};

function StatusIcon({ status }: { status: MCPClientStatus }) {
  switch (status) {
    case "connected":
      return <Plug className="h-4 w-4 text-success" />;
    case "configured_but_unconnected":
      return <PowerOff className="h-4 w-4 text-muted-foreground" />;
    case "unhealthy":
      return <AlertTriangle className="h-4 w-4 text-warning" />;
    case "error":
      return <AlertOctagon className="h-4 w-4 text-destructive" />;
  }
}

function TransportIcon({ transport }: { transport: MCPClientTransport }) {
  return transport === "stdio" ? (
    <Terminal className="h-3 w-3" />
  ) : (
    <Network className="h-3 w-3" />
  );
}

function truncateEndpoint(value: string, max = 60): string {
  if (value.length <= max) return value;
  return value.slice(0, max) + "…";
}

interface MCPClientRowProps {
  client: MCPClient;
  expanded: boolean;
  onToggle: () => void;
}

function MCPClientRow({ client, expanded, onToggle }: MCPClientRowProps) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-2 py-3">
        <button
          type="button"
          onClick={onToggle}
          className="flex items-center gap-3 text-left w-full"
          aria-expanded={expanded}
        >
          {expanded ? (
            <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0" />
          ) : (
            <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0" />
          )}
          <StatusIcon status={client.status} />
          <span className="font-medium">{client.name}</span>
          <Badge tone="outline">
            <TransportIcon transport={client.transport} />
            <span className="ml-1">{client.transport}</span>
          </Badge>
          <Badge tone={STATUS_TONE[client.status]}>
            {STATUS_LABEL[client.status]}
          </Badge>
          {client.tools_count !== null && (
            <span className="text-xs text-muted-foreground">
              {client.tools_count} tool{client.tools_count === 1 ? "" : "s"}
            </span>
          )}
          {/* Auth indicator — presence-only, never the value */}
          <span className="text-xs text-muted-foreground ml-auto flex items-center gap-1">
            <KeyRound className="h-3 w-3" />
            {client.auth_token_present ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-success" />
            ) : (
              <XCircle className="h-3.5 w-3.5 text-destructive" />
            )}
          </span>
        </button>

        {expanded && (
          <div className="ml-7 flex flex-col gap-2 pt-2 border-t border-border text-xs">
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                endpoint
              </span>
              <code
                className="font-mono break-all"
                title={client.endpoint}
              >
                {truncateEndpoint(client.endpoint, 100)}
              </code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                auth_token_env
              </span>
              <code className="font-mono">{client.auth_token_env}</code>
              <span className="flex items-center gap-1 ml-auto">
                {client.auth_token_present ? (
                  <>
                    <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                    <span className="text-success">present in Doppler</span>
                  </>
                ) : (
                  <>
                    <XCircle className="h-3.5 w-3.5 text-destructive" />
                    <span className="text-destructive">missing</span>
                  </>
                )}
              </span>
            </div>
            {/* Token VALUE never rendered — only the env-var name + presence */}
            <div className="text-[10px] text-muted-foreground italic">
              Token values live in Doppler and are never displayed here.
              Use{" "}
              <code className="font-mono not-italic">
                doppler secrets set {client.auth_token_env}
              </code>{" "}
              to rotate.
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                allowed_tools_regex
              </span>
              <code className="font-mono">
                {client.allowed_tools_regex ?? (
                  <span className="text-muted-foreground not-italic">
                    null (all tools allowed)
                  </span>
                )}
              </code>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground min-w-[120px]">
                tools_count
              </span>
              <span>
                {client.tools_count === null
                  ? "— (not connected)"
                  : client.tools_count}
              </span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function MCPClientsPanel() {
  const [data, setData] = useState<MCPClientsListResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedNames, setExpandedNames] = useState<Set<string>>(new Set());
  const { toast, showToast } = useToast();

  const loadClients = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      api
        .getMCPClients()
        .then((resp) => setData(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(msg);
          showToast(`Failed to load MCP clients: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadClients(false);
  }, [loadClients]);

  const toggleExpand = useCallback((name: string) => {
    setExpandedNames((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  if (data === null && !loadError) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  // Aggregate counts for the header summary strip.
  const counts = data
    ? {
        connected: data.clients.filter((c) => c.status === "connected").length,
        configured: data.clients.filter(
          (c) => c.status === "configured_but_unconnected",
        ).length,
        errors: data.clients.filter(
          (c) => c.status === "error" || c.status === "unhealthy",
        ).length,
      }
    : null;

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-start justify-between gap-4">
        <div>
          <H2>Installed MCPs</H2>
          <p className="text-sm text-muted-foreground mt-1">
            External MCP servers Kora is configured to consume.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadClients(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load MCP clients</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {data?.stub && (
        <Card className="border-warning/40 bg-warning/10">
          <CardContent className="py-3 flex items-start gap-3 text-sm">
            <Plug className="h-4 w-4 mt-0.5 text-warning" />
            <div>
              <div className="font-medium">
                STUB — real data wires in via KR-MCP-1 ST2
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Values shown are hardcoded sample data, not live catalog
                state. CC#1's KR-MCP-1 ST2 swaps the endpoint body to
                project from the live <code>kora_mcp/</code> pool.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          {/* ── Aggregate summary strip ─────────────────────────── */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
              <Plug className="h-4 w-4 text-primary" />
              <span className="font-medium">
                {data.clients.length} MCP{data.clients.length === 1 ? "" : "s"}{" "}
                configured
              </span>
              {counts && (
                <>
                  <span className="flex items-center gap-1.5 text-xs">
                    <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                    {counts.connected} connected
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <PowerOff className="h-3.5 w-3.5 text-muted-foreground" />
                    {counts.configured} configured · unconnected
                  </span>
                  {counts.errors > 0 && (
                    <span className="flex items-center gap-1.5 text-xs text-destructive">
                      <AlertOctagon className="h-3.5 w-3.5" />
                      {counts.errors} error{counts.errors === 1 ? "" : "s"}
                    </span>
                  )}
                </>
              )}
              <span className="text-xs text-muted-foreground ml-auto italic">
                Token values never displayed — managed in Doppler.
              </span>
            </CardContent>
          </Card>

          {/* ── Clients list ──────────────────────────────────── */}
          {data.clients.length === 0 ? (
            <Card>
              <CardContent className="py-8 text-center text-sm text-muted-foreground">
                <HelpCircle className="h-6 w-6 mx-auto mb-2 opacity-50" />
                No MCP clients configured.
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {data.clients.map((c) => (
                <MCPClientRow
                  key={c.name}
                  client={c}
                  expanded={expandedNames.has(c.name)}
                  onToggle={() => toggleExpand(c.name)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
