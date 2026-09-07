export function FullPageSpinner({ label }: { label: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50" role="status">
      <div className="flex items-center gap-3 text-slate-500">
        <span
          className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600"
          aria-hidden="true"
        />
        <span className="text-sm">{label}</span>
      </div>
    </div>
  )
}
