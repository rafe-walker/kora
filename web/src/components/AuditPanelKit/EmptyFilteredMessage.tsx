// Calm green-toned empty-state card. Used by every audit-stream
// panel when the visible event list is empty — either because
// the audit log itself is empty (isAllFilter) OR because the
// current filter narrows to zero.
//
// Per spec discipline established in PR #167 / #171 / #180 /
// #183: empty states are RESEARCH-LAB calm reassurance, NOT a
// sad "no data" stub. The Sparkles glyph + green tone matches
// the "everything's healthy" message in ProbeInvestigationsPage.

import { Sparkles } from "lucide-react";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";

export interface EmptyFilteredMessageProps {
  /** True iff the current filter is "all" — drives the title +
   *  body copy (audit empty vs filter empty). */
  isAllFilter: boolean;
  /** Title when isAllFilter (e.g. "No outbound email events
   *  recorded yet"). */
  titleAll: string;
  /** Title when !isAllFilter (e.g. 'No "Failed" events match
   *  this filter in the current window'). */
  titleFiltered: string;
  /** Body copy when isAllFilter. The non-filter body is a
   *  standard "Try switching ... or click All to see every event"
   *  composed inside this component (so the All-reset link is
   *  always present + wired up). */
  bodyAll: string;
  /** Called when operator clicks the embedded "All" reset link. */
  onResetToAll: () => void;
}

export function EmptyFilteredMessage({
  isAllFilter,
  titleAll,
  titleFiltered,
  bodyAll,
  onResetToAll,
}: EmptyFilteredMessageProps) {
  return (
    <Card className="border-green-500/30 bg-green-500/5">
      <CardContent className="p-8 flex flex-col items-center text-center gap-3">
        <Sparkles className="h-7 w-7 text-green-500" />
        <H2 className="text-base">
          {isAllFilter ? titleAll : titleFiltered}
        </H2>
        <p className="text-sm text-muted-foreground max-w-md">
          {isAllFilter ? (
            <>{bodyAll}</>
          ) : (
            <>
              Try switching to a different filter, or click{" "}
              <button
                onClick={onResetToAll}
                className="underline text-primary"
              >
                All
              </button>{" "}
              to see every event.
            </>
          )}
        </p>
      </CardContent>
    </Card>
  );
}
