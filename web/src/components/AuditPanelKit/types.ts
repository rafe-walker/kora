// Shared types for the AuditPanelKit. Kept separate from
// component files so consumers can `import type` cleanly and so
// the type-only tree stays slim.

import type { ComponentType } from "react";
import type { BadgeTone } from "./BadgeTone";

// CategoryDef parameterizes both SummaryChips and FilterChips
// over the canonical enum a given panel uses (action / status /
// action_category / etc). The string-literal type parameter
// flows through to onChange so callers get exhaustive type
// checking on chip clicks.
export interface CategoryDef<K extends string> {
  key: K;
  label: string;
  tone: BadgeTone;
  Icon: ComponentType<{ className?: string }>;
}

// Sparkline data point shape — daily count, date keyed as
// YYYY-MM-DD (UTC) so plain string comparison sorts
// chronologically (matches BE backend pre-sort).
export interface DailyCountPoint {
  date: string;
  count: number;
}
