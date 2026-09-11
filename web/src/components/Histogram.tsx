import { useId } from 'react'

import type { HistogramResponse } from '@/api/types'

/**
 * Chunk sizes as bars: how many chunks fell in each token band, with the ceiling marked.
 *
 * The histogram task 20's Compare left out, built once for the audit (task 103) and used
 * by Compare too. Buckets come from the server's rule — twelve across one and a half
 * ceilings, the last one open — so two connectors with the same `chunk_size` draw on the
 * same axis, and a screen can be read across them.
 */
export function Histogram({
  histogram,
  chunkSize,
  compact = false,
}: {
  histogram: HistogramResponse
  chunkSize: number
  compact?: boolean
}) {
  const labelId = useId()
  const max = Math.max(1, ...histogram.buckets.map((bucket) => bucket.count))
  const total = histogram.buckets.reduce((sum, bucket) => sum + bucket.count, 0)
  const height = compact ? 'h-16' : 'h-28'

  if (total === 0) {
    return <p className="py-4 text-center text-xs text-slate-400">No chunks to draw.</p>
  }

  return (
    <figure aria-describedby={labelId} data-testid="chunk-histogram">
      <figcaption id={labelId} className="sr-only">
        {total} chunks by token count, in buckets of {histogram.bucket_tokens} tokens; the ceiling
        is {chunkSize}.
      </figcaption>
      <div className={`flex items-end gap-px ${height}`}>
        {histogram.buckets.map((bucket, index) => {
          const last = index === histogram.buckets.length - 1
          const overCeiling = bucket.lower >= chunkSize
          return (
            <div
              key={bucket.lower}
              className="flex flex-1 flex-col justify-end"
              title={`${bucket.count} chunk${bucket.count === 1 ? '' : 's'} of ${bucket.lower}–${
                last ? '∞' : bucket.upper
              } tokens`}
            >
              <div
                className={`w-full rounded-t ${overCeiling ? 'bg-amber-400' : 'bg-blue-500'}`}
                style={{
                  height: `${Math.max(bucket.count > 0 ? 2 : 0, (bucket.count / max) * 100)}%`,
                }}
              />
            </div>
          )
        })}
      </div>
      <div className="mt-1 flex justify-between text-[10px] tabular-nums text-slate-500">
        <span>0</span>
        <span>{chunkSize} (ceiling)</span>
        <span>{histogram.buckets[histogram.buckets.length - 1]?.lower ?? 0}+</span>
      </div>
    </figure>
  )
}
