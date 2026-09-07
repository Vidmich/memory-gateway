import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <div className="text-center">
        <h1 className="text-lg font-semibold text-slate-900">Page not found</h1>
        <p className="mt-1 text-sm text-slate-500">
          That address does not match anything in this application.
        </p>
        <Link to="/" className="mt-4 inline-block text-sm text-slate-900 underline">
          Go to the dashboard
        </Link>
      </div>
    </div>
  )
}
