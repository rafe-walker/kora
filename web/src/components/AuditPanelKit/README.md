# AuditPanelKit

Shared building blocks for audit-stream cockpit panels. Extracted
from the duplication between `EmailIntentLogPage.tsx` (PR #180)
and `OutboundEmailLogPage.tsx` (PR #183) per the CC#2-flagged
"3rd-consumer threshold" — that 3rd consumer (`AutofixLogPage`)
shipped alongside this kit.

## What's in here

| Component / helper | Purpose |
|---|---|
| `Sparkline` | 14-day daily-count bars; plain SVG, no chart lib. |
| `SummaryChips` | 24h count band — zero-count categories skipped. |
| `FilterChips` | "All" + per-category buttons with counts. |
| `EmptyFilteredMessage` | Calm green empty state w/ inline "All" reset link. |
| `BadgeTone` | Type alias pinned to `@nous-research/ui` Badge tones. |
| `CategoryDef<K>` | Parameterizes both chip components over the enum a panel uses. |
| `DailyCountPoint` | `{date, count}` shape for sparkline. |
| `formatTimestamp` / `formatRelative` / `truncate` | Audit-row timestamp/text formatters. |
| `formatBytes` / `formatChars` / `formatDurationMs` | Size + duration formatters used across panels. |

## Props contract

### `Sparkline`
- `points: DailyCountPoint[]` — pre-sorted chronologically; empty
  array hides the component entirely (no zero-bar skeleton).
- `width / height` — pixel dimensions, defaults 220×36.
- `totalSuffix` — caller-composed (e.g. `"created · 14d"`,
  `"sent · 14d"`, `"actions · 14d"`).
- `ariaLabel` — optional override; sensible default built from
  `points.length`.

### `SummaryChips<K>` / `FilterChips<K>`
Both take a `categories: readonly CategoryDef<K>[]` — the
canonical-values list, with `{key, label, tone, Icon}`. K must be a
string-literal union so `onChange` is exhaustively type-checked
in `FilterChips`.

- `SummaryChips` SKIPS zero-count categories (visual scales with
  what's actually happening).
- `FilterChips` ALWAYS renders every category (operator needs to
  see every filter option even when count=0).

### `EmptyFilteredMessage`
- `isAllFilter` discriminator drives title + body:
  - `true` → "audit log empty" copy (`titleAll` + `bodyAll`)
  - `false` → "filter narrowed to zero" copy (`titleFiltered` +
    auto-composed body with inline `onResetToAll` link)

## Theming

The kit components use Tailwind classes consistent with
`@nous-research/ui` panel styling. Color tokens:

- `success` → `green-500`
- `destructive` → `text-destructive` (theme-driven)
- `warning` → `yellow-500`
- `outline` / `secondary` / `default` → `muted-foreground`

`Sparkline` uses `fill-green-500` for non-zero bars and
`fill-muted/40` for ghost-bars on empty days (so the day-grid
position is visible).

No new component-library deps. Charts stay plain-SVG per CC#2's
established discipline (CostTelemetryPage PR #164, sparklines in
#180 / #183).

## Adding a new consumer

1. Define your enum's `categories: CategoryDef<YourStatus>[]`
   array with `{key, label, tone, Icon}` per value.
2. Define a `FilterValue<YourStatus>` state with default `"all"`.
3. Render `<SummaryChips>` + `<FilterChips>` + your event list +
   `<EmptyFilteredMessage>` for the zero-events branch.
4. Use the formatters for timestamps + sizes + durations.

## Future consumers (recommended)

- `KoraActionsPage` (this bucket) — joined cross-seam timeline.
- Future audit panels for `phrasebook.updated` operator-edit
  history, `mcp.tool_called` per-call cost xref, etc.

The kit grows AS new consumers reveal needs — don't pre-add
abstractions. If a new consumer needs a slightly different chip
shape, copy first + revisit kit after the second new variation.
