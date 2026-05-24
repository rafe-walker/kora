// DM phrasebook viewer + live tester — KR-FE-PHRASEBOOK-VIEWER.
//
// Read-only v1. Surfaces the bundled-or-overridden phrasebook the
// live Slack DM handler consults before invoking the reasoning
// engine, plus a tester input that previews what the handler would
// do for an operator-supplied sample message AGAINST THE CURRENT
// SNAPSHOT.
//
// The would_fall_through_to_reasoning_engine signal is the headline
// answer for each tester call — operator wants to know "is this
// going to short-circuit ($0) or go to the engine (cents)?" The
// fall-through happens in three cases (per dm_phrasebook.py:269-307):
//   1. No entry matched (matched=false)
//   2. Snapshot is null/stale
//   3. Any referenced snapshot field is "unknown" or null
//
// Write/edit path is a future bucket — KR-FE-PHRASEBOOK-EDITOR +
// KR-API-PHRASEBOOK-CRUD.

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowDownToLine,
  Bot,
  CheckCircle2,
  FileEdit,
  History,
  Info,
  Pencil,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import type {
  PhrasebookBackupItem,
  PhrasebookEntryDto,
  PhrasebookResponse,
  PhrasebookTestResponse,
  PhrasebookValidationErrorBody,
  PhrasebookValidationErrorEntry,
  SnapshotResponse,
  SnapshotUnavailable,
} from "@/lib/api";
import {
  BackupsDialog,
  ClientSidePreview,
  EditModeControls,
  EntryEditorRow,
  makeEmptyEntry,
  toEditableEntries,
  type EditableEntry,
} from "@/pages/PhrasebookEditor";

// Walk a dotted path through nested dicts. Mirrors
// dm_phrasebook._walk_snapshot so the FE's "is this field unknown?"
// affordance agrees with what render_reply would see at runtime.
function walkSnapshotField(
  snapshot: SnapshotResponse | null,
  dotted: string,
): unknown {
  if (snapshot === null) return undefined;
  // The placeholder paths in dm_phrasebook templates are written
  // like "{snapshot.cost_ladder.current_tier}" — the endpoint's
  // extractor strips the "snapshot." prefix so we walk against
  // the snapshot dict directly.
  let cur: unknown = snapshot;
  for (const seg of dotted.split(".")) {
    if (cur === null || typeof cur !== "object" || Array.isArray(cur)) {
      return undefined;
    }
    cur = (cur as Record<string, unknown>)[seg];
    if (cur === undefined) return undefined;
  }
  return cur;
}

// "unknown" sentinel match — dm_phrasebook.render_reply treats the
// literal string "unknown" as degraded (alongside null/missing).
function isDegradedSnapshotValue(value: unknown): boolean {
  return value === undefined || value === null || value === "unknown";
}

interface EntryRowProps {
  entry: PhrasebookEntryDto;
  snapshot: SnapshotResponse | null;
}

function EntryRow({ entry, snapshot }: EntryRowProps) {
  // Identify which of this entry's referenced fields are currently
  // "unknown" / missing / null in the live snapshot. Drives the
  // "would fall through" badge — operator sees per-entry whether
  // the short-circuit path is viable right now.
  const degradedFields = entry.referenced_snapshot_fields.filter((path) =>
    isDegradedSnapshotValue(walkSnapshotField(snapshot, path)),
  );
  const willFallThrough =
    snapshot === null || degradedFields.length > 0;
  return (
    <tr className="border-b border-border/40 align-top">
      <td className="py-2 pr-3 align-top">
        <Badge tone="outline">
          <span className="text-[10px] font-mono">{entry.category}</span>
        </Badge>
      </td>
      <td className="py-2 pr-3 align-top">
        <code className="font-mono text-[11px] break-all">
          {entry.pattern}
        </code>
        {entry.description && (
          <div className="text-[10px] text-muted-foreground mt-0.5 italic">
            {entry.description}
          </div>
        )}
      </td>
      <td className="py-2 pr-3 align-top">
        <code className="font-mono text-[11px] break-all">
          {entry.reply_template}
        </code>
        {entry.referenced_snapshot_fields.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {entry.referenced_snapshot_fields.map((path) => {
              const degraded = degradedFields.includes(path);
              return (
                <Badge
                  key={path}
                  tone={degraded ? "warning" : "outline"}
                  title={
                    degraded
                      ? `Currently 'unknown'/missing — render would fall through to reasoning`
                      : "Field present in current snapshot"
                  }
                >
                  <span className="font-mono text-[10px]">{path}</span>
                </Badge>
              );
            })}
          </div>
        )}
      </td>
      <td className="py-2 align-top text-right">
        {willFallThrough ? (
          <Badge tone="warning" title="Snapshot stale OR fields unknown">
            <ArrowDownToLine className="h-3 w-3" />
            <span className="ml-1">→ reasoning</span>
          </Badge>
        ) : (
          <Badge tone="success" title="Short-circuit viable; $0 reply">
            <CheckCircle2 className="h-3 w-3" />
            <span className="ml-1">$0 reply</span>
          </Badge>
        )}
      </td>
    </tr>
  );
}

interface LiveTesterProps {
  onTest: (text: string) => Promise<void>;
  result: PhrasebookTestResponse | null;
  testing: boolean;
  testError: string | null;
}

function LiveTester({ onTest, result, testing, testError }: LiveTesterProps) {
  const [text, setText] = useState("hey");
  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    void onTest(text);
  }
  return (
    <Card>
      <CardContent className="py-4 flex flex-col gap-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Bot className="h-4 w-4 text-primary" />
          Live tester
        </div>
        <p className="text-xs text-muted-foreground">
          Type a sample DM. Read-only preview of what the live handler
          would do for this text against the current snapshot — does
          NOT call the reasoning engine, does NOT send DMs.
        </p>
        <form onSubmit={handleSubmit} className="flex gap-2 items-stretch">
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="hey / what's my burn? / any alerts?"
            className="flex-1 px-3 py-1.5 text-sm bg-background border border-border rounded-md font-mono"
            maxLength={1024}
          />
          <Button size="sm" disabled={testing} onClick={handleSubmit}>
            {testing ? (
              <RefreshCw className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3" />
            )}
            Test
          </Button>
        </form>

        {testError && (
          <div className="flex items-start gap-2 text-xs text-destructive">
            <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
            <span>{testError}</span>
          </div>
        )}

        {result && !testError && <TesterResult result={result} />}
      </CardContent>
    </Card>
  );
}

function TesterResult({ result }: { result: PhrasebookTestResponse }) {
  // Three outcome shapes per spec § "3 tester states":
  //   * Unmatched → reasoning fallback
  //   * Matched + rendered → short-circuit $0 reply
  //   * Matched + null render → would-fall-through-because-field-unknown
  if (!result.matched) {
    return (
      <div className="flex items-start gap-2 text-sm p-3 rounded-md border border-warning/30 bg-warning/5">
        <XCircle className="h-4 w-4 text-warning mt-0.5 shrink-0" />
        <div className="flex flex-col gap-1">
          <div className="font-medium">No phrasebook entry matched</div>
          <div className="text-xs text-muted-foreground">
            Would fall through to the reasoning engine → costs cents.
          </div>
        </div>
      </div>
    );
  }

  const isFallThrough = result.would_fall_through_to_reasoning_engine;
  return (
    <div
      className={`flex flex-col gap-2 p-3 rounded-md border ${
        isFallThrough
          ? "border-warning/30 bg-warning/5"
          : "border-success/30 bg-success/5"
      }`}
    >
      <div className="flex items-start gap-2 text-sm">
        {isFallThrough ? (
          <ArrowDownToLine className="h-4 w-4 text-warning mt-0.5 shrink-0" />
        ) : (
          <CheckCircle2 className="h-4 w-4 text-success mt-0.5 shrink-0" />
        )}
        <div className="flex-1 min-w-0">
          <div className="font-medium flex items-center gap-2 flex-wrap">
            {isFallThrough ? "Matched but would fall through" : "Matched · $0 reply"}
            <Badge tone="outline">
              <span className="font-mono text-[10px]">{result.category}</span>
            </Badge>
          </div>
          {result.description && (
            <div className="text-xs text-muted-foreground italic mt-0.5">
              {result.description}
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-1.5 text-xs">
        <div className="flex gap-2">
          <span className="text-muted-foreground min-w-[120px]">Pattern</span>
          <code className="font-mono text-[11px] break-all">
            {result.pattern}
          </code>
        </div>
        <div className="flex gap-2">
          <span className="text-muted-foreground min-w-[120px]">
            Reply template
          </span>
          <code className="font-mono text-[11px] break-all">
            {result.reply_template}
          </code>
        </div>
        {result.referenced_snapshot_fields.length > 0 && (
          <div className="flex gap-2">
            <span className="text-muted-foreground min-w-[120px]">
              Referenced fields
            </span>
            <div className="flex flex-wrap gap-1">
              {result.referenced_snapshot_fields.map((path) => (
                <Badge key={path} tone="outline">
                  <span className="font-mono text-[10px]">{path}</span>
                </Badge>
              ))}
            </div>
          </div>
        )}
        <div className="flex gap-2">
          <span className="text-muted-foreground min-w-[120px]">
            Rendered reply
          </span>
          {result.rendered_reply !== null ? (
            <div className="flex-1 font-mono text-sm bg-background/50 border border-border rounded px-2 py-1">
              {result.rendered_reply}
            </div>
          ) : (
            <span className="italic text-warning">
              null (snapshot stale OR field "unknown" → fall through to
              reasoning)
            </span>
          )}
        </div>
        {!result.snapshot_present && (
          <div className="flex gap-2 text-warning">
            <Info className="h-3.5 w-3.5 mt-0.5" />
            <span>
              No snapshot available — every matched entry would fall
              through to the reasoning engine until the next snapshot
              refresh.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

export default function PhrasebookPage() {
  usePanelView("PhrasebookPage");

  const [phrasebook, setPhrasebook] = useState<PhrasebookResponse | null>(
    null,
  );
  const [snapshot, setSnapshot] = useState<SnapshotResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [testResult, setTestResult] = useState<PhrasebookTestResponse | null>(
    null,
  );
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);

  // KR-FE-PHRASEBOOK-EDITOR-AND-CRUD — edit-mode state.
  // editingEntries === null ≡ view mode (read-only); non-null ≡
  // operator is editing locally and hasn't saved yet.
  const [editingEntries, setEditingEntries] = useState<
    EditableEntry[] | null
  >(null);
  const [saving, setSaving] = useState(false);
  const [validationErrors, setValidationErrors] = useState<
    PhrasebookValidationErrorEntry[]
  >([]);

  // Backups dialog state — separate from edit mode so operator
  // can revert from view-mode or edit-mode equally.
  const [backupsOpen, setBackupsOpen] = useState(false);
  const [backupsLoading, setBackupsLoading] = useState(false);
  const [backups, setBackups] = useState<PhrasebookBackupItem[]>([]);
  const [backupsRotationKeep, setBackupsRotationKeep] = useState(10);

  const { toast, showToast } = useToast();

  const loadAll = useCallback(
    async (isManual: boolean) => {
      if (isManual) setRefreshing(true);
      setLoadError(null);
      try {
        const [pb, snap] = await Promise.all([
          api.getSlackDmPhrasebook(),
          api.getSnapshot(),
        ]);
        setPhrasebook(pb);
        // Snapshot may be unavailable — that's a valid render state
        // (the per-row "would fall through" affordance treats null
        // snapshot as universal fall-through).
        setSnapshot(
          snap !== null && !("error" in (snap as SnapshotUnavailable))
            ? (snap as SnapshotResponse)
            : null,
        );
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setLoadError(msg);
        if (isManual) showToast(`Failed to load: ${msg}`, "error");
      } finally {
        if (isManual) setRefreshing(false);
      }
    },
    [showToast],
  );

  const handleTest = useCallback(async (text: string) => {
    setTesting(true);
    setTestError(null);
    try {
      const result = await api.testSlackDmPhrasebook(text);
      setTestResult(result);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setTestError(msg);
      setTestResult(null);
    } finally {
      setTesting(false);
    }
  }, []);

  useEffect(() => {
    void loadAll(false);
  }, [loadAll]);

  // ---- Edit mode handlers ----

  const enterEditMode = useCallback(() => {
    if (phrasebook === null) return;
    setEditingEntries(toEditableEntries(phrasebook.entries));
    setValidationErrors([]);
  }, [phrasebook]);

  const exitEditMode = useCallback(() => {
    setEditingEntries(null);
    setValidationErrors([]);
  }, []);

  const handleSave = useCallback(async () => {
    if (editingEntries === null) return;
    setSaving(true);
    setValidationErrors([]);
    try {
      const writeBody = editingEntries.map(({ _localId: _ig, ...rest }) => rest);
      const result = await api.putSlackDmPhrasebook(writeBody);
      // Refresh local state from the response (server-canonicalized
      // entries; includes referenced_snapshot_fields).
      setPhrasebook({
        source: "override",
        source_path: result.source_path,
        override_candidate_path: result.source_path,
        entries: result.entries,
      });
      setEditingEntries(null);
      const backupNote = result.backup_filename
        ? ` · backup: ${result.backup_filename}`
        : " · no prior override (first edit)";
      showToast(
        `Saved ${result.entry_count} entries${backupNote}`,
        "success",
      );
    } catch (e: unknown) {
      // fetchJSON throws "STATUS: BODY" — parse for 422 structured
      // errors so the editor can render per-row feedback.
      const msg = e instanceof Error ? e.message : String(e);
      const match = msg.match(/^(\d+):\s*(.*)$/s);
      if (match && match[1] === "422") {
        try {
          const body: PhrasebookValidationErrorBody = JSON.parse(match[2]);
          if (body && body.error === "validation_failed") {
            setValidationErrors(body.errors);
            showToast(
              `Save refused — ${body.errors.length} validation error${
                body.errors.length === 1 ? "" : "s"
              }`,
              "error",
            );
            return;
          }
        } catch {
          /* fall through to generic error */
        }
      }
      showToast(`Save failed: ${msg}`, "error");
    } finally {
      setSaving(false);
    }
  }, [editingEntries, showToast]);

  const updateEntry = useCallback(
    (localId: string, next: EditableEntry) => {
      setEditingEntries((prev) =>
        prev === null
          ? prev
          : prev.map((e) => (e._localId === localId ? next : e)),
      );
    },
    [],
  );

  const deleteEntry = useCallback((localId: string) => {
    setEditingEntries((prev) =>
      prev === null ? prev : prev.filter((e) => e._localId !== localId),
    );
  }, []);

  const addEntry = useCallback(() => {
    setEditingEntries((prev) =>
      prev === null ? prev : [...prev, makeEmptyEntry()],
    );
  }, []);

  // ---- Backups + revert ----

  const openBackups = useCallback(async () => {
    setBackupsOpen(true);
    setBackupsLoading(true);
    try {
      const result = await api.getSlackDmPhrasebookBackups();
      setBackups(result.backups);
      setBackupsRotationKeep(result.rotation_keep);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      showToast(`Failed to load backups: ${msg}`, "error");
    } finally {
      setBackupsLoading(false);
    }
  }, [showToast]);

  const handleRevert = useCallback(
    async (filename: string | null) => {
      try {
        const result = await api.revertSlackDmPhrasebook(filename);
        showToast(`Reverted to ${result.reverted_to}`, "success");
        // Refresh phrasebook + exit edit mode (the in-progress
        // edits are now stale relative to the reverted content).
        setEditingEntries(null);
        setValidationErrors([]);
        await loadAll(false);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        showToast(`Revert failed: ${msg}`, "error");
      }
    },
    [loadAll, showToast],
  );

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex flex-col gap-1">
          <H2>Phrasebook (Slack DM short-circuit)</H2>
          <p className="text-xs text-muted-foreground max-w-2xl">
            Read-only view of the regex patterns the live Slack DM
            handler consults before invoking the reasoning engine.
            Matched + renderable → $0 reply; otherwise falls through
            to reasoning (cents).
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {editingEntries === null && (
            <>
              <Button
                size="sm"
                outlined
                disabled={refreshing || phrasebook === null}
                onClick={enterEditMode}
                title="Edit the phrasebook entries — saves write to the operator override"
              >
                <Pencil className="h-3 w-3" />
                Edit phrasebook
              </Button>
              <Button
                size="sm"
                ghost
                onClick={() => void openBackups()}
                title="View + revert from backups"
              >
                <History className="h-3 w-3" />
                Backups
              </Button>
            </>
          )}
          <Button
            size="sm"
            ghost
            disabled={refreshing}
            onClick={() => void loadAll(true)}
          >
            <RefreshCw
              className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`}
            />
            Reload
          </Button>
        </div>
      </div>

      {loadError && (
        <Card className="border-destructive/40">
          <CardContent className="py-4 flex items-start gap-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load phrasebook</div>
              <div className="text-xs opacity-80">{loadError}</div>
            </div>
          </CardContent>
        </Card>
      )}

      {phrasebook === null && !loadError && (
        <div className="flex items-center justify-center py-24">
          <Spinner className="text-2xl text-primary" />
        </div>
      )}

      {phrasebook !== null && (
        <>
          {/* Source banner — operator needs to know whether they're
              looking at the bundled default or their override. */}
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
              <span className="font-medium">Source:</span>
              {phrasebook.source === "override" ? (
                <Badge tone="success">
                  <FileEdit className="h-3 w-3" />
                  <span className="ml-1">operator override</span>
                </Badge>
              ) : (
                <Badge tone="outline">bundled default</Badge>
              )}
              <code className="font-mono text-muted-foreground break-all">
                {phrasebook.source_path}
              </code>
              {phrasebook.source === "bundled_default" &&
                phrasebook.override_candidate_path && (
                  <span className="text-muted-foreground italic ml-2">
                    Override path:{" "}
                    <code className="font-mono">
                      {phrasebook.override_candidate_path}
                    </code>{" "}
                    (create to override)
                  </span>
                )}
            </CardContent>
          </Card>

          {/* Live tester — view-mode uses the backend's live
              phrasebook; edit-mode swaps in the ClientSidePreview
              that runs against in-progress edits instead. */}
          {editingEntries === null ? (
            <LiveTester
              onTest={handleTest}
              result={testResult}
              testing={testing}
              testError={testError}
            />
          ) : (
            <ClientSidePreview
              entries={editingEntries}
              snapshot={snapshot}
            />
          )}

          {/* Entries — read-only table in view-mode, editor in
              edit-mode. */}
          {editingEntries === null ? (
            <Card>
              <CardContent className="py-4 flex flex-col gap-3">
                <div className="text-sm font-medium">
                  All entries ({phrasebook.entries.length})
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-muted-foreground border-b border-border">
                        <th className="text-left font-medium py-1.5 pr-3">
                          Category
                        </th>
                        <th className="text-left font-medium py-1.5 pr-3">
                          Pattern
                        </th>
                        <th className="text-left font-medium py-1.5 pr-3">
                          Reply template + referenced snapshot fields
                        </th>
                        <th className="text-right font-medium py-1.5">
                          Current viability
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {phrasebook.entries.map((entry, i) => (
                        <EntryRow
                          key={`${entry.category}-${i}`}
                          entry={entry}
                          snapshot={snapshot}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="text-[10px] text-muted-foreground italic">
                  "Current viability" is computed against the live snapshot:
                  if any referenced field is currently "unknown" or missing,
                  the live handler would fall through to the reasoning
                  engine for that entry. Click "Edit phrasebook" to
                  add / modify / delete entries.
                </div>
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              <EditModeControls
                saving={saving}
                onSave={() => void handleSave()}
                onCancel={exitEditMode}
                onAddEntry={addEntry}
                rootErrors={validationErrors.filter(
                  (e) => e.entry_index === -1,
                )}
              />
              {editingEntries.map((entry, i) => (
                <EntryEditorRow
                  key={entry._localId}
                  entry={entry}
                  index={i}
                  errors={validationErrors}
                  onChange={(next) => updateEntry(entry._localId, next)}
                  onDelete={() => deleteEntry(entry._localId)}
                />
              ))}
              <EditModeControls
                saving={saving}
                onSave={() => void handleSave()}
                onCancel={exitEditMode}
                onAddEntry={addEntry}
                rootErrors={[]}
              />
            </div>
          )}
        </>
      )}

      <BackupsDialog
        open={backupsOpen}
        loading={backupsLoading}
        backups={backups}
        rotationKeep={backupsRotationKeep}
        onClose={() => setBackupsOpen(false)}
        onRevert={handleRevert}
      />
    </div>
  );
}
