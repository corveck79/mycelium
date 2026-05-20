import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';

const navItems = [
  { to: '/', label: 'Discover', icon: '✨', exact: true },
  { to: '/library', label: 'Library', icon: '📚' },
  { to: '/watchlist', label: 'Watchlist', icon: '★' },
  { to: '/search', label: 'Search', icon: '🔍' },
  { to: '/requests', label: 'My Requests', icon: '📋' },
  { to: '/wanted', label: 'Wanted', icon: '⏳' },
];

const adminItems = [
  { to: '/admin', label: 'Admin', icon: '⚙️' },
];

export default function Layout() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const location = useLocation();
  const { data: session } = useQuery({
    queryKey: ['session'],
    queryFn: api.session,
    staleTime: 60_000,
  });

  const isAdmin = session?.user?.role === 'admin';
  const showAdmin = isAdmin || !session?.authenticated;  // bootstrap visible

  return (
    <div className="min-h-screen flex bg-bg text-white">
      {/* Sidebar (desktop) + Drawer (mobile) */}
      <aside
        className={`
          fixed lg:sticky top-0 left-0 h-screen w-56 bg-card border-r border-border z-40
          transition-transform duration-200
          ${drawerOpen ? 'translate-x-0' : '-translate-x-full'} lg:translate-x-0
        `}
      >
        <div className="px-5 py-5 flex items-center gap-3 border-b border-border">
          <svg width="28" height="28" viewBox="0 0 40 40" aria-hidden="true">
            <g stroke="#22d3ee" strokeWidth="1.5" opacity="0.7">
              <line x1="10" y1="20" x2="30" y2="10"/>
              <line x1="10" y1="20" x2="30" y2="30"/>
              <line x1="30" y1="10" x2="30" y2="30"/>
              <line x1="20" y1="5"  x2="10" y2="20"/>
              <line x1="20" y1="35" x2="10" y2="20"/>
            </g>
            <circle cx="10" cy="20" r="3.5" fill="#0d9488"/>
            <circle cx="30" cy="10" r="3"   fill="#22d3ee"/>
            <circle cx="30" cy="30" r="3"   fill="#22d3ee"/>
            <circle cx="20" cy="5"  r="2.2" fill="#5eead4"/>
            <circle cx="20" cy="35" r="2.2" fill="#5eead4"/>
          </svg>
          <span className="font-mono font-bold tracking-wide text-lg">
            myc<span className="text-accent">3</span>l<span className="text-accent">1</span>um
          </span>
        </div>
        <nav className="py-3">
          <SidebarSection title="Browse" items={navItems} onClick={() => setDrawerOpen(false)} />
          {showAdmin && (
            <SidebarSection title="Manage" items={adminItems} onClick={() => setDrawerOpen(false)} />
          )}
          <SidebarSection
            title=""
            items={[{ to: '/settings', label: 'Settings', icon: '🛠' }]}
            onClick={() => setDrawerOpen(false)}
          />
        </nav>
        <div className="absolute bottom-0 left-0 right-0 p-4 border-t border-border text-xs text-muted">
          {session?.user ? (
            <div className="flex items-center justify-between">
              <span>👤 {session.user.username}</span>
              <a href="/logout" className="hover:text-white">Log out</a>
            </div>
          ) : (
            <a href="/app/login" className="hover:text-white">Sign in →</a>
          )}
        </div>
      </aside>

      {/* Drawer overlay */}
      {drawerOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-30 lg:hidden"
          onClick={() => setDrawerOpen(false)}
        />
      )}

      {/* Main content */}
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="sticky top-0 z-20 bg-bg/80 backdrop-blur border-b border-border">
          <div className="flex items-center gap-3 px-4 lg:px-8 py-3">
            <button
              className="lg:hidden p-2 -ml-2 hover:bg-card rounded"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open menu"
            >
              <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor">
                <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              </svg>
            </button>
            <Breadcrumb path={location.pathname} />
            <div className="ml-auto flex items-center gap-2">
              <a
                href="/ui"
                className="text-xs text-muted hover:text-white transition px-3 py-1.5 rounded border border-border"
                title="Open the original Mycelium dashboard"
              >
                Old UI
              </a>
            </div>
          </div>
        </header>
        <main className="flex-1 px-4 lg:px-8 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function SidebarSection({
  title,
  items,
  onClick,
}: {
  title: string;
  items: { to: string; label: string; icon: string; exact?: boolean }[];
  onClick: () => void;
}) {
  return (
    <div className="mb-2">
      {title && (
        <div className="px-5 pt-3 pb-1 text-[10px] uppercase tracking-wider text-muted font-semibold">
          {title}
        </div>
      )}
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.exact}
          onClick={onClick}
          className={({ isActive }) =>
            `flex items-center gap-3 px-5 py-2 text-sm transition relative
             ${isActive
                ? 'text-white bg-accent/10 before:absolute before:left-0 before:top-0 before:bottom-0 before:w-0.5 before:bg-accent'
                : 'text-muted hover:text-white hover:bg-card'
              }`
          }
        >
          <span className="text-base">{item.icon}</span>
          <span>{item.label}</span>
        </NavLink>
      ))}
    </div>
  );
}

function Breadcrumb({ path }: { path: string }) {
  const map: Record<string, string> = {
    '/': 'Discover',
    '/library': 'Library',
    '/watchlist': 'Watchlist',
    '/search': 'Search',
    '/requests': 'My Requests',
    '/wanted': 'Wanted',
    '/admin': 'Admin',
    '/settings': 'Settings',
    '/login': 'Sign in',
  };
  const title = map[path] || 'Mycelium';
  return <h1 className="font-semibold text-lg">{title}</h1>;
}
