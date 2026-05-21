import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Key,
  KeyRound,
  RefreshCw,
  RotateCcw,
  ShieldOff,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import type { GatewayPlatformIdentity } from "@/lib/api";

// The canonical default display_name lives in gateway/config.py
// (PlatformConfig.display_name). The API returns the resolved effective
// value, so this page never hardcodes the literal.
const DISPLAY_NAME_MAX_LEN = 64;

function platformLabel(platform_id: string): string {
  if (!platform_id) return "—";
  return platform_id
    .split("_")
    .map((p) => (p.length ? p[0].toUpperCase() + p.slice(1) : p))
    .join(" ");
}

function sourceHelperText(
  source: GatewayPlatformIdentity["display_name_source"],
  effective: string,
): string {
  switch (source) {
    case "config":
      return "Set in config.yaml top-level";
    case "extra":
      return "Set in config.yaml extra: block";
    case "default":
    default:
      return `Currently using default (“${effective}”)`;
  }
}

function tokenStatusLabel(
  status: GatewayPlatformIdentity["token_status"],
): { text: string; tone: "success" | "warning" | "outline" } {
  switch (status) {
    case "configured":
      return { text: "Token configured", tone: "success" };
    case "env_referenced":
      return { text: "Token via env var", tone: "outline" };
    case "missing":
    default:
      return { text: "Token missing", tone: "warning" };
  }
}

interface CardState {
  draft: string;
  saving: boolean;
}

function initialCardState(p: GatewayPlatformIdentity): CardState {
  // Empty draft = "use default"; non-empty = explicit override.
  return {
    draft: p.display_name_source === "default" ? "" : p.display_name,
    saving: false,
  };
}

export default function IdentityPage() {
  const [platforms, setPlatforms] = useState<GatewayPlatformIdentity[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, CardState>>({});
  const { toast, showToast } = useToast();

  const loadAll = useCallback(() => {
    setLoadError(null);
    api
      .listGatewayPlatforms()
      .then((rows) => {
        setPlatforms(rows);
        setDrafts((prev) => {
          const next: Record<string, CardState> = {};
          for (const p of rows) {
            next[p.platform_id] = prev[p.platform_id] ?? initialCardState(p);
          }
          return next;
        });
      })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        setLoadError(msg);
        showToast(`Failed to load gateway platforms: ${msg}`, "error");
      });
  }, [showToast]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const updateDraft = useCallback((platform_id: string, value: string) => {
    setDrafts((prev) => ({
      ...prev,
      [platform_id]: { ...(prev[platform_id] ?? { draft: "", saving: false }), draft: value },
    }));
  }, []);

  const resetDraft = useCallback((p: GatewayPlatformIdentity) => {
    setDrafts((prev) => ({ ...prev, [p.platform_id]: initialCardState(p) }));
  }, []);

  const save = useCallback(
    async (p: GatewayPlatformIdentity) => {
      const draft = drafts[p.platform_id]?.draft ?? "";
      setDrafts((prev) => ({
        ...prev,
        [p.platform_id]: { ...(prev[p.platform_id] ?? { draft, saving: false }), saving: true },
      }));
      try {
        const updated = await api.updateGatewayPlatformIdentity(p.platform_id, draft);
        setPlatforms((prev) =>
          prev ? prev.map((row) => (row.platform_id === p.platform_id ? updated : row)) : prev,
        );
        setDrafts((prev) => ({
          ...prev,
          [p.platform_id]: initialCardState(updated),
        }));
        showToast(
          updated.display_name_source === "default"
            ? `${platformLabel(p.platform_id)}: override cleared`
            : `${platformLabel(p.platform_id)}: display_name → "${updated.display_name}"`,
          "success",
        );
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setDrafts((prev) => ({
          ...prev,
          [p.platform_id]: {
            ...(prev[p.platform_id] ?? { draft, saving: false }),
            saving: false,
          },
        }));
        showToast(`${platformLabel(p.platform_id)}: ${msg}`, "error");
      }
    },
    [drafts, showToast],
  );

  const isDirty = useCallback(
    (p: GatewayPlatformIdentity): boolean => {
      const baseline = initialCardState(p).draft;
      const draft = drafts[p.platform_id]?.draft ?? baseline;
      return draft.trim() !== baseline.trim();
    },
    [drafts],
  );

  const { supportedPlatforms, orphanPlatforms } = useMemo(() => {
    const rows = platforms ?? [];
    return {
      supportedPlatforms: rows.filter((p) => p.supported),
      orphanPlatforms: rows.filter((p) => !p.supported),
    };
  }, [platforms]);

  if (platforms === null && !loadError) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-start justify-between gap-4">
        <div>
          <H2>Identity</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Set Kora's user-facing display name per gateway platform.
          </p>
        </div>
        <Button size="sm" ghost onClick={loadAll}>
          <RefreshCw className="h-3 w-3" />
          Reload
        </Button>
      </div>

      {loadError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load platforms</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {supportedPlatforms.length === 0 && !loadError && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No gateway platforms discovered.
          </CardContent>
        </Card>
      )}

      {supportedPlatforms.map((p) => {
        const state = drafts[p.platform_id] ?? initialCardState(p);
        const dirty = isDirty(p);
        const tokenInfo = tokenStatusLabel(p.token_status);
        const overByte = state.draft.length > DISPLAY_NAME_MAX_LEN;

        return (
          <Card key={p.platform_id}>
            <CardContent className="flex flex-col gap-3 py-4">
              <div className="flex items-center gap-3">
                <span className="font-medium">{platformLabel(p.platform_id)}</span>
                <Badge tone={p.enabled ? "success" : "outline"}>
                  {p.enabled ? "Enabled" : "Disabled"}
                </Badge>
                <Badge tone={tokenInfo.tone}>
                  <KeyRound className="h-3 w-3 mr-1" />
                  {tokenInfo.text}
                </Badge>
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`display-name-${p.platform_id}`}>
                  Display name
                </Label>
                <Input
                  id={`display-name-${p.platform_id}`}
                  value={state.draft}
                  maxLength={DISPLAY_NAME_MAX_LEN * 2}
                  placeholder={p.display_name}
                  onChange={(e) => updateDraft(p.platform_id, e.target.value)}
                  disabled={state.saving}
                />
                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">
                    {sourceHelperText(p.display_name_source, p.display_name)}
                  </span>
                  {overByte && (
                    <span className="text-destructive">
                      Too long ({state.draft.length}/{DISPLAY_NAME_MAX_LEN})
                    </span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  disabled={!dirty || state.saving || overByte}
                  onClick={() => save(p)}
                >
                  {state.saving ? (
                    "Saving…"
                  ) : (
                    <>
                      <Check className="h-3 w-3" />
                      Save
                    </>
                  )}
                </Button>
                <Button
                  size="sm"
                  ghost
                  disabled={!dirty || state.saving}
                  onClick={() => resetDraft(p)}
                >
                  <RotateCcw className="h-3 w-3" />
                  Reset
                </Button>
              </div>
            </CardContent>
          </Card>
        );
      })}

      {orphanPlatforms.length > 0 && (
        <div className="flex flex-col gap-3 pt-2">
          <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
            <ShieldOff className="h-4 w-4" />
            Unconfigured platforms
          </div>
          <p className="text-xs text-muted-foreground -mt-1">
            These platform IDs appear in config.yaml but no matching adapter is installed.
            Identity cannot be set until the adapter is installed or the entry is removed
            from config.yaml.
          </p>
          {orphanPlatforms.map((p) => (
            <Card key={p.platform_id}>
              <CardContent className="flex items-center gap-3 py-3">
                <Key className="h-4 w-4 text-muted-foreground" />
                <span className="font-medium">{platformLabel(p.platform_id)}</span>
                <Badge tone="warning">Orphan</Badge>
                <span className="text-xs text-muted-foreground ml-auto">
                  display_name: {p.display_name}
                </span>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
