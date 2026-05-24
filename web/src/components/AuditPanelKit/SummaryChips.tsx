// Per-category count chips for the 24h summary band. Extracted
// from EmailIntentLogPage (PR #180) + OutboundEmailLogPage
// (PR #183). Iterates a canonical CategoryDef[] so the calling
// panel doesn't have to re-implement the chip-tone color map.
//
// Zero-count categories are SKIPPED (not rendered as empty chips)
// so the summary band scales with what's actually happening.

import type { CategoryDef } from "./types";

export interface SummaryChipsProps<K extends string> {
  categories: readonly CategoryDef<K>[];
  counts: Record<string, number>;
  total: number;
  /** Window label suffix; default "last 24h". */
  windowLabel?: string;
  /** Plural noun for the total. Default "events". Pages using
   *  domain-specific verbs override: "composed" (outbound email),
   *  "actions" (kora-actions aggregated). */
  totalNoun?: string;
}

// Map BadgeTone to the Tailwind color class used for the icon.
// Mirror of the inline switch that lived in both PR #180 + #183.
function iconColorClass(tone: CategoryDef<string>["tone"]): string {
  if (tone === "success") return "text-green-500";
  if (tone === "destructive") return "text-destructive";
  if (tone === "warning") return "text-yellow-500";
  return "text-muted-foreground";
}

export function SummaryChips<K extends string>({
  categories,
  counts,
  total,
  windowLabel = "last 24h",
  totalNoun = "events",
}: SummaryChipsProps<K>) {
  return (
    <div className="flex items-center gap-3 flex-wrap text-sm">
      <span>
        <strong>{total}</strong>{" "}
        <span className="text-muted-foreground">
          {totalNoun} · {windowLabel}
        </span>
      </span>
      {total > 0 && (
        <span className="text-muted-foreground">·</span>
      )}
      {categories.map((cat) => {
        const count = counts[cat.key] ?? 0;
        if (count === 0) return null;
        const Icon = cat.Icon;
        return (
          <div
            key={cat.key}
            className="flex items-center gap-1 text-xs"
            title={`${count} ${cat.label.toLowerCase()} in ${windowLabel}`}
          >
            <Icon className={`h-3 w-3 ${iconColorClass(cat.tone)}`} />
            <span className="font-medium">{count}</span>
            <span className="text-muted-foreground">
              {cat.label.toLowerCase()}
            </span>
          </div>
        );
      })}
    </div>
  );
}
