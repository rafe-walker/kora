import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BookOpenCheck,
  CheckCircle2,
  Clock,
  FileText,
  Printer,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Markdown } from "@/components/Markdown";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import type { RunbookEntry, RunbooksManifest } from "@/lib/api";

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const deltaMs = d.getTime() - Date.now();
  const absSec = Math.abs(deltaMs) / 1000;
  if (absSec < 60) return deltaMs < 0 ? "just now" : "in <1m";
  const absMin = absSec / 60;
  if (absMin < 60) {
    const n = Math.round(absMin);
    return deltaMs < 0 ? `${n}m ago` : `in ${n}m`;
  }
  const absHr = absMin / 60;
  if (absHr < 24) {
    const n = Math.round(absHr);
    return deltaMs < 0 ? `${n}h ago` : `in ${n}h`;
  }
  const absDay = absHr / 24;
  const n = Math.round(absDay);
  return deltaMs < 0 ? `${n}d ago` : `in ${n}d`;
}

function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface SidebarItemProps {
  runbook: RunbookEntry;
  isSelected: boolean;
  onSelect: () => void;
}

function SidebarItem({ runbook, isSelected, onSelect }: SidebarItemProps) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`text-left w-full p-3 rounded border transition-colors ${
        isSelected
          ? "border-primary/50 bg-primary/10"
          : "border-border hover:border-primary/30 hover:bg-muted/30"
      }`}
    >
      <div className="flex items-start gap-2">
        {runbook.available ? (
          <CheckCircle2 className="h-3.5 w-3.5 text-success shrink-0 mt-0.5" />
        ) : (
          <XCircle className="h-3.5 w-3.5 text-muted-foreground shrink-0 mt-0.5" />
        )}
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium leading-snug">{runbook.title}</div>
          <code className="text-[10px] text-muted-foreground block mt-1 truncate">
            {runbook.path}
          </code>
        </div>
      </div>
    </button>
  );
}

interface PendingPlaceholderProps {
  runbook: RunbookEntry;
}

function PendingPlaceholder({ runbook }: PendingPlaceholderProps) {
  return (
    <Card className="border-warning/40 bg-warning/10">
      <CardContent className="py-6 flex flex-col gap-3 text-sm">
        <div className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 text-warning" />
          <span className="font-medium">[runbook pending]</span>
        </div>
        <p>
          Operationally required; not yet authored — pending kora-docs work.
        </p>
        <div className="text-xs text-muted-foreground">
          Expected path:{" "}
          <code className="text-xs">{runbook.path}</code>
        </div>
      </CardContent>
    </Card>
  );
}

interface ContentPaneProps {
  runbook: RunbookEntry | null;
  content: string | null;
  loading: boolean;
  error: string | null;
  onPrint: () => void;
}

function ContentPane({ runbook, content, loading, error, onPrint }: ContentPaneProps) {
  if (runbook === null) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted-foreground">
          <FileText className="h-6 w-6 mx-auto mb-2 opacity-50" />
          Select a runbook from the sidebar.
        </CardContent>
      </Card>
    );
  }

  if (!runbook.available) {
    return <PendingPlaceholder runbook={runbook} />;
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex items-start justify-between gap-3 pb-2 border-b">
          <div className="flex-1 min-w-0">
            <div className="text-base font-medium">{runbook.title}</div>
            <div className="text-xs text-muted-foreground mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="flex items-center gap-1">
                <FileText className="h-3 w-3" />
                <code>{runbook.path}</code>
              </span>
              <span>{formatBytes(runbook.size_bytes)}</span>
              {runbook.last_modified && (
                <span className="flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  updated {formatRelative(runbook.last_modified)} (
                  {formatTimestamp(runbook.last_modified)})
                </span>
              )}
            </div>
          </div>
          <Button size="sm" outlined onClick={onPrint} disabled={loading || content === null}>
            <Printer className="h-3 w-3" />
            Print
          </Button>
        </div>

        {loading && (
          <div className="flex items-center justify-center py-12">
            <Spinner className="text-2xl text-primary" />
          </div>
        )}

        {error && (
          <div className="flex items-start gap-3 text-sm text-destructive py-4">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load runbook</div>
              <div className="text-xs opacity-80">{error}</div>
            </div>
          </div>
        )}

        {!loading && !error && content !== null && (
          <div id="runbook-print-content" className="overflow-y-auto max-h-[calc(100vh-260px)]">
            <Markdown content={content} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function RunbooksPage() {
  const [manifest, setManifest] = useState<RunbooksManifest | null>(null);
  const [manifestError, setManifestError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);
  const { toast, showToast } = useToast();

  const loadManifest = useCallback(
    (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setManifestError(null);
      api
        .getRunbooks()
        .then((resp) => setManifest(resp))
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : String(e);
          setManifestError(msg);
          showToast(`Failed to load runbooks: ${msg}`, "error");
        })
        .finally(() => {
          if (isManual) setRefreshing(false);
        });
    },
    [showToast],
  );

  useEffect(() => {
    loadManifest(false);
  }, [loadManifest]);

  // Load content when selectedId changes (and is available).
  useEffect(() => {
    if (selectedId === null) {
      setContent(null);
      setContentError(null);
      return;
    }
    const entry = manifest?.runbooks.find((r) => r.id === selectedId);
    if (entry === undefined || !entry.available) {
      // Placeholder — no content to fetch
      setContent(null);
      setContentError(null);
      return;
    }
    setContentLoading(true);
    setContentError(null);
    setContent(null);
    api
      .getRunbookContent(selectedId)
      .then((text) => setContent(text))
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        setContentError(msg);
      })
      .finally(() => setContentLoading(false));
  }, [selectedId, manifest]);

  const handlePrint = useCallback(() => {
    // Browser's print dialog renders the current page; the print
    // stylesheet (or default styles) will paginate. Operators get a
    // paper-friendly view of the markdown.
    window.print();
  }, []);

  const selectedRunbook =
    selectedId !== null && manifest
      ? manifest.runbooks.find((r) => r.id === selectedId) ?? null
      : null;

  if (manifest === null && !manifestError) {
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
          <H2>Runbooks</H2>
          <p className="text-sm text-muted-foreground mt-1">
            Operator procedures — read here, execute outside.
          </p>
        </div>
        <Button size="sm" ghost disabled={refreshing} onClick={() => loadManifest(true)}>
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
          Reload
        </Button>
      </div>

      {manifestError && (
        <Card>
          <CardContent className="py-6 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load runbooks manifest</div>
              <div className="text-xs opacity-80">{manifestError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-4">
        {/* ── Sidebar ──────────────────────────────────────────── */}
        <div className="flex flex-col gap-2">
          <div className="text-xs text-muted-foreground flex items-center gap-1 px-1">
            <BookOpenCheck className="h-3 w-3" />
            {manifest?.runbooks.length ?? 0} runbook
            {manifest?.runbooks.length === 1 ? "" : "s"}
            {manifest && (
              <span className="ml-auto">
                <Badge tone="success">
                  {manifest.runbooks.filter((r) => r.available).length}{" "}
                  available
                </Badge>
              </span>
            )}
          </div>
          {(manifest?.runbooks ?? []).map((rb) => (
            <SidebarItem
              key={rb.id}
              runbook={rb}
              isSelected={selectedId === rb.id}
              onSelect={() => setSelectedId(rb.id)}
            />
          ))}
        </div>

        {/* ── Content pane ─────────────────────────────────────── */}
        <ContentPane
          runbook={selectedRunbook}
          content={content}
          loading={contentLoading}
          error={contentError}
          onPrint={handlePrint}
        />
      </div>
    </div>
  );
}
