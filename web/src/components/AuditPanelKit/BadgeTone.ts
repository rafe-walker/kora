// Pin to @nous-research/ui Badge's tone union — must match
// node_modules/@nous-research/ui/dist/ui/components/badge.d.ts.
// "outline" is the neutral / non-toned chip.
//
// Used across the AuditPanelKit + every audit-stream panel that
// renders status / action / severity badges. Kept as a separate
// file so a future Badge library swap can update one place.
export type BadgeTone =
  | "default"
  | "destructive"
  | "outline"
  | "secondary"
  | "success"
  | "warning";
