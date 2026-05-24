// AuditPanelKit — shared building blocks for audit-stream
// cockpit panels (event lists with action/status filtering,
// 24h summary, 14-day sparkline, calm empty state).
//
// Used by EmailIntentLogPage (PR #180), OutboundEmailLogPage
// (PR #183), AutofixLogPage (this bucket), KoraActionsPage
// (this bucket), and future audit-stream panels.
//
// See ./README.md for the props contract + theming notes + the
// "what each component does" reference.

export { Sparkline, type SparklineProps } from "./Sparkline";
export { SummaryChips, type SummaryChipsProps } from "./SummaryChips";
export {
  FilterChips,
  type FilterChipsProps,
  type FilterValue,
} from "./FilterChips";
export {
  EmptyFilteredMessage,
  type EmptyFilteredMessageProps,
} from "./EmptyFilteredMessage";
export type { BadgeTone } from "./BadgeTone";
export type { CategoryDef, DailyCountPoint } from "./types";
export {
  formatTimestamp,
  formatRelative,
  truncate,
  formatBytes,
  formatChars,
  formatDurationMs,
} from "./formatters";
