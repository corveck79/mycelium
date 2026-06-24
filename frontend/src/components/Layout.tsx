import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';

const navItems = [
  { to: '/', label: 'Discover', icon: '✨', exact: true },
  { to: '/shows', label: 'Shows', icon: '📺' },
  { to: '/movies', label: 'Movies', icon: '🎬' },
  { to: '/library', label: 'Library', icon: '📚' },
  { to: '/watchlist', label: 'Watchlist', icon: '★' },
  { to: '/search', label: 'Search', icon: '🔍' },
  { to: '/requests', label: 'My Requests', icon: '📋' },
  { to: '/wanted', label: 'Wanted', icon: '⏳' },
  { to: '/settings', label: 'Settings', icon: '🔌' },
];

const adminItems = [
  { to: '/auto-approve', label: 'Auto-Approve', icon: '🤖' },
  { to: '/blacklist', label: 'Blacklist', icon: '🚫' },
  { to: '/admin', label: 'Admin', icon: '⚙️' },
  { to: '/manual', label: 'Manual', icon: '📖' },
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
          <span className="font-mono font-bold tracking-wide text-lg text-white">
            myc<span className="text-accent-2">3</span>l<span className="text-accent-2">1</span>um
          </span>
        </div>
        <nav className="py-3">
          <SidebarSection title="Browse" items={navItems} onClick={() => setDrawerOpen(false)} />
          {showAdmin && (
            <SidebarSection title="Manage" items={adminItems} onClick={() => setDrawerOpen(false)} />
          )}
        </nav>
        <div className="absolute bottom-0 left-0 right-0 p-4 border-t border-border text-xs text-muted">
          {session?.user ? (
            <div className="flex items-center justify-between">
              <span>👤 {session.user.username}</span>
              <a href="/logout" className="hover:text-white">Log out</a>
            </div>
          ) : (
            <a href="/login" className="hover:text-white">Sign in</a>
          )}
          <div className="mt-2 text-center text-[10px] opacity-50">v0.2.0-beta</div>
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
              className="lg:hidden p-2 -ml-2 hover:bg-card rounded text-white"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open menu"
            >
              <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
                <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              </svg>
            </button>
            <Breadcrumb path={location.pathname} />
            <div className="ml-auto flex items-center gap-2">
              {session?.user && <RegionPicker region={session.user.region || 'NL'} />}
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
  items: { to: string; label: string; icon: string; exact?: boolean; external?: boolean }[];
  onClick: () => void;
}) {
  return (
    <div className="mb-2">
      {title && (
        <div className="px-5 pt-3 pb-1 text-[10px] uppercase tracking-wider text-muted font-semibold">
          {title}
        </div>
      )}
      {items.map((item) =>
        item.external ? (
          <a
            key={item.to}
            href={item.to}
            onClick={onClick}
            className="flex items-center gap-3 px-5 py-2 text-sm transition relative text-muted hover:text-white hover:bg-bg"
          >
            <span className="text-base">{item.icon}</span>
            <span>{item.label}</span>
          </a>
        ) : (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.exact}
            onClick={onClick}
            className={({ isActive }) =>
              `flex items-center gap-3 px-5 py-2 text-sm transition relative
               ${isActive
                  ? 'text-white bg-accent/10 before:absolute before:left-0 before:top-0 before:bottom-0 before:w-0.5 before:bg-accent'
                  : 'text-muted hover:text-white hover:bg-bg'
                }`
            }
          >
            <span className="text-base">{item.icon}</span>
            <span>{item.label}</span>
          </NavLink>
        )
      )}
    </div>
  );
}

const REGIONS: { code: string; flag: string; name: string }[] = [
  { code: 'NL', flag: '\u{1F1F3}\u{1F1F1}', name: 'Netherlands' },
  { code: 'BE', flag: '\u{1F1E7}\u{1F1EA}', name: 'Belgium' },
  { code: 'ZA', flag: '\u{1F1FF}\u{1F1E6}', name: 'South Africa' },
  { code: 'US', flag: '\u{1F1FA}\u{1F1F8}', name: 'United States' },
  { code: 'GB', flag: '\u{1F1EC}\u{1F1E7}', name: 'United Kingdom' },
  { code: 'DE', flag: '\u{1F1E9}\u{1F1EA}', name: 'Germany' },
  { code: 'FR', flag: '\u{1F1EB}\u{1F1F7}', name: 'France' },
  { code: 'ES', flag: '\u{1F1EA}\u{1F1F8}', name: 'Spain' },
  { code: 'IT', flag: '\u{1F1EE}\u{1F1F9}', name: 'Italy' },
  { code: 'AU', flag: '\u{1F1E6}\u{1F1FA}', name: 'Australia' },
  { code: 'CA', flag: '\u{1F1E8}\u{1F1E6}', name: 'Canada' },
  { code: 'BR', flag: '\u{1F1E7}\u{1F1F7}', name: 'Brazil' },
  { code: 'IN', flag: '\u{1F1EE}\u{1F1F3}', name: 'India' },
  { code: 'JP', flag: '\u{1F1EF}\u{1F1F5}', name: 'Japan' },
  { code: 'KR', flag: '\u{1F1F0}\u{1F1F7}', name: 'South Korea' },
  { code: 'SE', flag: '\u{1F1F8}\u{1F1EA}', name: 'Sweden' },
  { code: 'NO', flag: '\u{1F1F3}\u{1F1F4}', name: 'Norway' },
  { code: 'DK', flag: '\u{1F1E9}\u{1F1F0}', name: 'Denmark' },
  { code: 'PT', flag: '\u{1F1F5}\u{1F1F9}', name: 'Portugal' },
  { code: 'PL', flag: '\u{1F1F5}\u{1F1F1}', name: 'Poland' },
];

function RegionPicker({ region }: { region: string }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (code: string) => api.setRegion(code),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['session'] });
      queryClient.invalidateQueries({ queryKey: ['trending'] });
      queryClient.invalidateQueries({ queryKey: ['popular'] });
      queryClient.invalidateQueries({ queryKey: ['top-rated'] });
      queryClient.invalidateQueries({ queryKey: ['now-playing'] });
      queryClient.invalidateQueries({ queryKey: ['upcoming'] });
      queryClient.invalidateQueries({ queryKey: ['providers'] });
      queryClient.invalidateQueries({ queryKey: ['by-provider'] });
    },
  });

  const current = REGIONS.find((r) => r.code === region);
  const flag = current?.flag || region;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-2 py-1 rounded border border-border hover:border-accent/50 text-sm transition"
        title={current?.name || region}
      >
        <span className="text-base">{flag}</span>
        <span className="text-xs text-muted">{region}</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-lg shadow-xl overflow-hidden w-48 max-h-64 overflow-y-auto">
            {REGIONS.map((r) => (
              <button
                key={r.code}
                type="button"
                onClick={() => {
                  mutation.mutate(r.code);
                  setOpen(false);
                }}
                className={`w-full flex items-center gap-2 px-3 py-2 text-sm text-left transition
                  ${r.code === region ? 'bg-accent/10 text-white' : 'text-muted hover:text-white hover:bg-bg'}`}
              >
                <span>{r.flag}</span>
                <span>{r.name}</span>
                <span className="ml-auto text-[10px] opacity-50">{r.code}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function Breadcrumb({ path }: { path: string }) {
  const map: Record<string, string> = {
    '/': 'Discover',
    '/shows': 'Shows',
    '/movies': 'Movies',
    '/library': 'Library',
    '/watchlist': 'Watchlist',
    '/search': 'Search',
    '/requests': 'My Requests',
    '/wanted': 'Wanted',
    '/settings': 'Settings',
    '/auto-approve': 'Auto-Approve',
    '/blacklist': 'Blacklist',
    '/admin': 'Admin',
    '/manual': 'Manual',
    '/login': 'Sign in',
  };
  const title = map[path] || 'Mycelium';
  return <h1 className="font-semibold text-lg">{title}</h1>;
}

