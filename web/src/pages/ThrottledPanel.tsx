import { useThrottledEndUsers } from '@/api/limits'
import type { Window } from '@/api/monitoring'
import { BarList, ChartFrame } from '@/components/Charts'
import { throttledEmptyHint } from '@/pages/limits'

/**
 * SPEC §11's "an org can see when it is being throttled", by caller.
 *
 * It sits next to the error taxonomy rather than inside it because the two answer
 * different questions. The Errors chart already shows `rate_limited` as a bar — throttling
 * is a gateway error like any other and needs no special case there — but that bar says
 * *how much*, and the only useful next question is *whose*, which no grouping of error
 * codes can answer.
 *
 * Counted from the request log rather than from the live counters, so it covers the window
 * the rest of the screen is showing rather than the current minute, and survives a Redis
 * restart.
 *
 * The empty state distinguishes two things that look identical: nothing was throttled, and
 * things were throttled but nobody was identified. The second is a note about the
 * customer's integration — it is not sending `X-Gateway-User` — rather than about traffic.
 */
export function ThrottledPanel({
  window,
  gatewayId,
  rateLimited,
}: {
  /** The monitoring page's own window, so the panel and the charts agree. */
  window: Window
  gatewayId: string | null | undefined
  /** Rate-limit rejections in this window, from the error taxonomy. Decides which of the
   *  two empty states applies. */
  rateLimited: number
}) {
  const { data, isLoading } = useThrottledEndUsers(window, gatewayId)
  const items = data?.items ?? []

  return (
    <ChartFrame
      title="Throttled end users"
      subtitle="Who was refused most in this window, worst first."
    >
      {isLoading ? (
        <p className="py-8 text-center text-sm text-slate-400">Loading…</p>
      ) : items.length === 0 ? (
        <p className="py-8 text-center text-sm text-slate-400">
          {throttledEmptyHint(rateLimited)}
        </p>
      ) : (
        <BarList
          tone="#d97706"
          slices={items.map((item) => ({
            // The caller's own id when they sent one. Untrusted text, rendered as text.
            label: item.external_id ?? item.end_user_id,
            value: item.rejections,
          }))}
        />
      )}
    </ChartFrame>
  )
}
