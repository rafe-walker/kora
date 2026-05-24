// Per-category filter-chip row with leading "All" chip.
// Extracted from EmailIntentLogPage (PR #180) +
// OutboundEmailLogPage (PR #183).
//
// Filter value type: `K | "all"` where K is the canonical-values
// type-parameter (action / status / action_category). "all" is
// the kit-defined sentinel meaning no filter; consumers branch
// `if (filter === "all")`.

import type { CategoryDef } from "./types";

export type FilterValue<K extends string> = K | "all";

export interface FilterChipsProps<K extends string> {
  categories: readonly CategoryDef<K>[];
  counts: Record<string, number>;
  current: FilterValue<K>;
  onChange: (next: FilterValue<K>) => void;
  /** "All" chip label override (e.g. "All actions"). Default "All". */
  allLabel?: string;
}

export function FilterChips<K extends string>({
  categories,
  counts,
  current,
  onChange,
  allLabel = "All",
}: FilterChipsProps<K>) {
  const totalAll = categories.reduce(
    (acc, c) => acc + (counts[c.key] ?? 0),
    0,
  );
  return (
    <div className="flex items-center gap-1 flex-wrap">
      <button
        onClick={() => onChange("all")}
        className={`px-2.5 py-1 text-xs rounded-md border transition-colors ${
          current === "all"
            ? "bg-primary text-primary-foreground border-primary"
            : "border-border hover:bg-accent"
        }`}
        aria-pressed={current === "all"}
      >
        {allLabel} <span className="opacity-70">({totalAll})</span>
      </button>
      {categories.map((cat) => {
        const Icon = cat.Icon;
        const count = counts[cat.key] ?? 0;
        const active = current === cat.key;
        return (
          <button
            key={cat.key}
            onClick={() => onChange(cat.key)}
            className={`px-2.5 py-1 text-xs rounded-md border transition-colors inline-flex items-center gap-1 ${
              active
                ? "bg-primary text-primary-foreground border-primary"
                : "border-border hover:bg-accent"
            }`}
            aria-pressed={active}
          >
            <Icon className="h-3 w-3" />
            {cat.label} <span className="opacity-70">({count})</span>
          </button>
        );
      })}
    </div>
  );
}
