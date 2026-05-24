// KR-FE-PHRASEBOOK-EDITOR-AND-CRUD — editor UI for the operator.
//
// Hosts components that PhrasebookPage delegates to in edit mode:
//
//   * EntryEditorRow — one editable row (4 inputs + delete button)
//   * EditModeControls — Save / Cancel / Add Entry buttons
//   * BackupsDialog — modal for revert flow
//   * ClientSidePreview — simulates the live tester against
//     in-progress edits (so operator can preview before saving)
//
// Validation feedback shape mirrors the backend's
// PhrasebookValidationErrorEntry: per-(entry_index, field) error
// messages rendered inline next to the offending input. Root
// errors (entry_index = -1) render at the top of the editor.
//
// Client-side preview implements just enough of dm_phrasebook's
// match + render_reply semantics to give an accurate preview —
// regex.test() (case-insensitive) + walkSnapshotField (mirrors
// _walk_snapshot). Pinned by tests that this matches the backend.

import { useCallback, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  History,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent } from "@/components/ui/card";
import type {
  PhrasebookBackupItem,
  PhrasebookEntryWrite,
  PhrasebookValidationErrorEntry,
  SnapshotResponse,
} from "@/lib/api";

// EditableEntry adds a stable per-row React key. The id is
// generated client-side (crypto.randomUUID) so adding rows in
// edit mode doesn't collide with array indices on re-renders.
export interface EditableEntry extends PhrasebookEntryWrite {
  _localId: string;
}

export function makeEmptyEntry(): EditableEntry {
  return {
    _localId: crypto.randomUUID(),
    pattern: "",
    category: "",
    description: "",
    reply_template: "",
  };
}

export function toEditableEntries(
  entries: PhrasebookEntryWrite[],
): EditableEntry[] {
  return entries.map((e) => ({
    _localId: crypto.randomUUID(),
    pattern: e.pattern,
    category: e.category,
    description: e.description,
    reply_template: e.reply_template,
  }));
}

// ---------------------------------------------------------------
// Per-row editor
// ---------------------------------------------------------------

interface EntryEditorRowProps {
  entry: EditableEntry;
  index: number;
  errors: PhrasebookValidationErrorEntry[];
  onChange: (next: EditableEntry) => void;
  onDelete: () => void;
}

function errorFor(
  errors: PhrasebookValidationErrorEntry[],
  index: number,
  field: string,
): string | null {
  const hit = errors.find(
    (e) => e.entry_index === index && e.field === field,
  );
  return hit ? hit.error : null;
}

export function EntryEditorRow({
  entry,
  index,
  errors,
  onChange,
  onDelete,
}: EntryEditorRowProps) {
  const patternErr = errorFor(errors, index, "pattern");
  const categoryErr = errorFor(errors, index, "category");
  const descriptionErr = errorFor(errors, index, "description");
  const replyErr = errorFor(errors, index, "reply_template");
  const rootErr = errorFor(errors, index, "_root");

  return (
    <Card className={rootErr ? "border-destructive/40" : ""}>
      <CardContent className="py-3 flex flex-col gap-2">
        <div className="flex items-start gap-2">
          <div className="flex-1 grid grid-cols-1 md:grid-cols-2 gap-2">
            <FieldEditor
              label="Pattern (Python regex, case-insensitive)"
              value={entry.pattern}
              onChange={(v) => onChange({ ...entry, pattern: v })}
              error={patternErr}
              mono
            />
            <FieldEditor
              label="Category"
              value={entry.category}
              onChange={(v) => onChange({ ...entry, category: v })}
              error={categoryErr}
            />
            <FieldEditor
              label="Description"
              value={entry.description}
              onChange={(v) => onChange({ ...entry, description: v })}
              error={descriptionErr}
            />
            <FieldEditor
              label="Reply template (supports {snapshot.X.Y} placeholders)"
              value={entry.reply_template}
              onChange={(v) =>
                onChange({ ...entry, reply_template: v })
              }
              error={replyErr}
              mono
              textarea
            />
          </div>
          <Button
            size="sm"
            ghost
            destructive
            onClick={onDelete}
            title="Remove this entry"
          >
            <Trash2 className="h-3 w-3" />
          </Button>
        </div>
        {rootErr && (
          <div className="text-xs text-destructive flex items-center gap-1.5">
            <AlertTriangle className="h-3 w-3" />
            {rootErr}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

interface FieldEditorProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  error: string | null;
  mono?: boolean;
  textarea?: boolean;
}

function FieldEditor({
  label,
  value,
  onChange,
  error,
  mono,
  textarea,
}: FieldEditorProps) {
  const baseInputClass = `w-full px-2 py-1 text-xs rounded border bg-card ${
    mono ? "font-mono" : ""
  } ${
    error
      ? "border-destructive/60 focus:outline-destructive"
      : "border-border focus:outline-primary"
  }`;
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      {textarea ? (
        <textarea
          className={`${baseInputClass} min-h-[44px] resize-y`}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          className={baseInputClass}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {error && (
        <span className="text-[10px] text-destructive italic">{error}</span>
      )}
    </label>
  );
}

// ---------------------------------------------------------------
// Edit-mode controls (Save / Cancel / Add)
// ---------------------------------------------------------------

interface EditModeControlsProps {
  saving: boolean;
  onSave: () => void;
  onCancel: () => void;
  onAddEntry: () => void;
  rootErrors: PhrasebookValidationErrorEntry[];
}

export function EditModeControls({
  saving,
  onSave,
  onCancel,
  onAddEntry,
  rootErrors,
}: EditModeControlsProps) {
  return (
    <div className="flex flex-col gap-2">
      {rootErrors.length > 0 && (
        <Card className="border-destructive/40">
          <CardContent className="py-2 flex flex-col gap-1 text-xs text-destructive">
            <div className="font-medium flex items-center gap-1.5">
              <AlertTriangle className="h-3 w-3" />
              {rootErrors.length} top-level error
              {rootErrors.length === 1 ? "" : "s"}:
            </div>
            {rootErrors.map((e, i) => (
              <div key={i} className="ml-4">
                • {e.error}
              </div>
            ))}
          </CardContent>
        </Card>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        <Button size="sm" onClick={onSave} disabled={saving}>
          {saving ? (
            <Spinner className="h-3 w-3" />
          ) : (
            <Save className="h-3 w-3" />
          )}
          Save changes
        </Button>
        <Button size="sm" ghost onClick={onCancel} disabled={saving}>
          <X className="h-3 w-3" />
          Cancel
        </Button>
        <Button size="sm" outlined onClick={onAddEntry} disabled={saving}>
          <Plus className="h-3 w-3" />
          Add entry
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------
// Backups dialog (revert flow)
// ---------------------------------------------------------------

interface BackupsDialogProps {
  open: boolean;
  loading: boolean;
  backups: PhrasebookBackupItem[];
  rotationKeep: number;
  onClose: () => void;
  onRevert: (filename: string | null) => Promise<void>;
}

export function BackupsDialog({
  open,
  loading,
  backups,
  rotationKeep,
  onClose,
  onRevert,
}: BackupsDialogProps) {
  const [confirmFilename, setConfirmFilename] = useState<string | null>(
    null,
  );
  const [reverting, setReverting] = useState(false);

  if (!open) return null;

  const handleRevert = async (filename: string | null) => {
    setReverting(true);
    try {
      await onRevert(filename);
      onClose();
    } finally {
      setReverting(false);
      setConfirmFilename(null);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <Card
        className="max-w-2xl w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <CardContent className="py-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <History className="h-4 w-4" />
              <span className="text-sm font-medium">
                Phrasebook backups
              </span>
              <span className="text-xs text-muted-foreground">
                (keeping {rotationKeep} most recent)
              </span>
            </div>
            <Button size="sm" ghost onClick={onClose}>
              <X className="h-3 w-3" />
            </Button>
          </div>

          {loading && (
            <div className="flex items-center justify-center py-8">
              <Spinner className="text-xl text-primary" />
            </div>
          )}

          {!loading && backups.length === 0 && (
            <div className="text-xs text-muted-foreground italic py-6 text-center">
              No backups yet. Backups are created automatically each time
              you save changes.
            </div>
          )}

          {!loading && backups.length > 0 && (
            <div className="flex flex-col gap-2">
              {backups.map((b) => {
                const corrupted = b.entry_count === null;
                const isConfirming = confirmFilename === b.filename;
                return (
                  <div
                    key={b.filename}
                    className={`p-2 rounded border ${
                      corrupted
                        ? "border-muted-foreground/30 opacity-60"
                        : "border-border"
                    }`}
                  >
                    <div className="flex items-center gap-3 text-xs">
                      <code className="font-mono flex-1 truncate">
                        {b.filename}
                      </code>
                      <span className="text-muted-foreground">
                        {corrupted
                          ? "corrupt"
                          : `${b.entry_count} entries`}
                      </span>
                      <span className="text-muted-foreground">
                        {b.size_bytes}b
                      </span>
                      {!isConfirming ? (
                        <Button
                          size="sm"
                          outlined
                          disabled={corrupted || reverting}
                          onClick={() => setConfirmFilename(b.filename)}
                        >
                          Revert
                        </Button>
                      ) : (
                        <div className="flex items-center gap-1">
                          <span className="text-warning italic mr-1">
                            Replace current?
                          </span>
                          <Button
                            size="sm"
                            destructive
                            disabled={reverting}
                            onClick={() => void handleRevert(b.filename)}
                          >
                            Yes, revert
                          </Button>
                          <Button
                            size="sm"
                            ghost
                            disabled={reverting}
                            onClick={() => setConfirmFilename(null)}
                          >
                            No
                          </Button>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {!loading && (
            <div className="text-[10px] text-muted-foreground italic">
              Revert clones the chosen backup over the current override
              YAML; the backup file itself is preserved (you can re-revert
              to it later). To remove the override entirely (fall back to
              bundled default), revert with no backups present.
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------
// Client-side live preview against in-progress entries
// ---------------------------------------------------------------
//
// Mirrors dm_phrasebook.match_message + render_reply just enough
// to give an accurate preview of what the live handler WOULD do
// after the operator saves the in-progress entries. Used as the
// LiveTester's backend during edit mode.
//
// Match: first regex that .test()s the stripped operator-text
//   (case-insensitive). Mirrors first-match-wins semantic.
// Render: walk {snapshot.X.Y} placeholders against the live
//   snapshot. Null when snapshot is null OR any referenced field
//   is undefined / null / "unknown" (per render_reply's fall-
//   through rules at dm_phrasebook.py:269-307).

function walkSnapshotField(
  snapshot: SnapshotResponse | null,
  dotted: string,
): unknown {
  if (snapshot === null) return undefined;
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

const _PLACEHOLDER_RE = /\{snapshot\.([a-zA-Z0-9_.]+)\}/g;

export interface ClientPreviewResult {
  matched: false;
  reason: "no_match";
}
export interface ClientPreviewMatchedRendered {
  matched: true;
  reason: "rendered";
  entry: PhrasebookEntryWrite;
  rendered: string;
}
export interface ClientPreviewMatchedFallthrough {
  matched: true;
  reason: "fall_through";
  entry: PhrasebookEntryWrite;
  missing_or_degraded: string[];
}
export type ClientPreview =
  | ClientPreviewResult
  | ClientPreviewMatchedRendered
  | ClientPreviewMatchedFallthrough
  | { matched: false; reason: "invalid_regex"; entry_index: number; error: string };

export function clientSidePreview(
  text: string,
  entries: PhrasebookEntryWrite[],
  snapshot: SnapshotResponse | null,
): ClientPreview {
  const stripped = text.trim();
  if (!stripped) return { matched: false, reason: "no_match" };

  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    let regex: RegExp;
    try {
      regex = new RegExp(entry.pattern, "i");
    } catch (e) {
      return {
        matched: false,
        reason: "invalid_regex",
        entry_index: i,
        error: e instanceof Error ? e.message : String(e),
      };
    }
    if (regex.test(stripped)) {
      // Matched — render the template.
      if (snapshot === null) {
        return {
          matched: true,
          reason: "fall_through",
          entry,
          missing_or_degraded: ["(snapshot unavailable)"],
        };
      }
      const missing: string[] = [];
      const rendered = entry.reply_template.replace(
        _PLACEHOLDER_RE,
        (_match, path) => {
          const value = walkSnapshotField(snapshot, path as string);
          if (value === undefined || value === null || value === "unknown") {
            missing.push(path as string);
            return "";
          }
          return String(value);
        },
      );
      if (missing.length > 0) {
        return {
          matched: true,
          reason: "fall_through",
          entry,
          missing_or_degraded: missing,
        };
      }
      return { matched: true, reason: "rendered", entry, rendered };
    }
  }
  return { matched: false, reason: "no_match" };
}

interface ClientSidePreviewProps {
  entries: PhrasebookEntryWrite[];
  snapshot: SnapshotResponse | null;
}

export function ClientSidePreview({
  entries,
  snapshot,
}: ClientSidePreviewProps) {
  const [text, setText] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const result = useMemo(
    () =>
      submitted !== null ? clientSidePreview(submitted, entries, snapshot) : null,
    [submitted, entries, snapshot],
  );

  const onTest = useCallback(() => {
    setSubmitted(text);
  }, [text]);

  return (
    <Card>
      <CardContent className="py-3 flex flex-col gap-3">
        <div className="text-xs font-medium flex items-center gap-1.5">
          <RefreshCw className="h-3 w-3" />
          Preview against in-progress edits (client-side)
        </div>
        <div className="flex items-center gap-2">
          <input
            className="flex-1 px-2 py-1 text-xs font-mono rounded border bg-card border-border focus:outline-primary"
            placeholder='Sample DM, e.g. "what is my burn"'
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onTest();
            }}
          />
          <Button size="sm" onClick={onTest} disabled={!text.trim()}>
            Test
          </Button>
        </div>

        {result && (
          <div className="text-xs">
            {result.matched === false && result.reason === "no_match" && (
              <Badge tone="outline">
                <XCircle className="h-3 w-3 mr-1 inline" />
                No entry matched · would fall through to reasoning
              </Badge>
            )}
            {result.matched === false &&
              result.reason === "invalid_regex" && (
                <Badge tone="destructive">
                  <AlertTriangle className="h-3 w-3 mr-1 inline" />
                  Entry {result.entry_index} has invalid regex:{" "}
                  {result.error}
                </Badge>
              )}
            {result.matched === true && result.reason === "rendered" && (
              <div className="flex flex-col gap-1">
                <Badge tone="success">
                  <CheckCircle2 className="h-3 w-3 mr-1 inline" />
                  Matched · category {result.entry.category} · $0 reply
                </Badge>
                <div className="font-mono text-xs p-2 rounded bg-muted/30 border border-border whitespace-pre-wrap">
                  {result.rendered}
                </div>
              </div>
            )}
            {result.matched === true &&
              result.reason === "fall_through" && (
                <div className="flex flex-col gap-1">
                  <Badge tone="warning">
                    <AlertTriangle className="h-3 w-3 mr-1 inline" />
                    Matched · category {result.entry.category} · would fall
                    through (fields missing/unknown:{" "}
                    {result.missing_or_degraded.join(", ")})
                  </Badge>
                </div>
              )}
          </div>
        )}

        <div className="text-[10px] text-muted-foreground italic">
          Mirrors dm_phrasebook.match_message + render_reply; preview
          uses your in-progress edits, not the saved phrasebook. Save to
          make these live.
        </div>
      </CardContent>
    </Card>
  );
}

// Lucide imports we use but didn't reference visibly: keep the
// tree-shake out of trouble.
export { ChevronDown };
