import { NavLink, Outlet } from 'react-router-dom'
import { useState, type ReactNode } from 'react'

import { useAuth } from '@/auth/AuthContext'
import { NAVIGATION, visibleNavigation } from '@/layout/navigation'

/** The frame every signed-in screen renders into. */

export function AppShell({ breadcrumb }: { breadcrumb?: ReactNode }) {
  const { user } = useAuth()
  const entries = visibleNavigation(NAVIGATION, user?.role)

  return (
    <div className="min-h-screen bg-slate-50">
      <div className="flex">
        <aside className="hidden w-60 shrink-0 border-r border-slate-200 bg-white md:block">
          <div className="flex h-14 items-center gap-2 border-b border-slate-200 px-4">
            <span className="text-sm font-semibold text-slate-900">Memory Gateway</span>
          </div>
          <nav aria-label="Main" className="p-2">
            {entries.map((entry) => (
              <NavLink
                key={entry.to}
                to={entry.to}
                end={entry.to === '/'}
                className={({ isActive }) =>
                  `block rounded-md px-3 py-2 text-sm ${
                    isActive
                      ? 'bg-slate-100 font-medium text-slate-900'
                      : 'text-slate-600 hover:bg-slate-50'
                  }`
                }
              >
                {entry.label}
              </NavLink>
            ))}
          </nav>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-14 items-center justify-between border-b border-slate-200 bg-white px-4">
            <div className="min-w-0 text-sm text-slate-500">
              {breadcrumb ?? <OrganizationName />}
            </div>
            <UserMenu />
          </header>

          <main className="flex-1 p-6">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  )
}

function OrganizationName() {
  const { user } = useAuth()
  if (!user) return null
  return (
    <span className="truncate">
      {user.organization ? user.organization.name : 'Platform administration'}
    </span>
  )
}

export function UserMenu() {
  const { user, signOut } = useAuth()
  const [open, setOpen] = useState(false)
  if (!user) return null

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="menu"
        className="flex items-center gap-2 rounded-md px-2 py-1 text-sm text-slate-700 hover:bg-slate-50"
      >
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-slate-200 text-xs font-medium text-slate-700">
          {initials(user.name || user.email)}
        </span>
        <span className="hidden sm:inline">{user.name || user.email}</span>
      </button>

      {open ? (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1 w-56 rounded-md border border-slate-200 bg-white py-1 shadow-lg"
        >
          <div className="px-3 py-2 text-xs text-slate-500">
            <div className="truncate font-medium text-slate-700">{user.email}</div>
            <div className="mt-0.5">{user.role}</div>
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={() => void signOut()}
            className="block w-full px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-50"
          >
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  )
}

function initials(value: string): string {
  const parts = value.split(/[\s@.]+/).filter(Boolean)
  return parts
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('')
}
