import { useState } from 'react';
import Admin from './Admin';

/**
 * The React Admin.tsx page (user management, Radarr/Sonarr import, auto-approve,
 * genre tabs, maintenance) was built but never wired into any route - /admin
 * only ever rendered an iframe of the legacy Jinja dashboard (Overview, Blacklist,
 * Repair, Settings, Logs). Neither fully replaces the other yet, so this shows
 * both behind a tab switcher instead of picking one and losing functionality.
 */
export default function AdminTabs() {
  const [tab, setTab] = useState<'dashboard' | 'classic'>('dashboard');

  return (
    <div>
      <div className="flex gap-2 border-b border-border mb-5">
        {([
          ['dashboard', 'Dashboard'],
          ['classic', 'Classic (Blacklist, Repair, Settings, Logs)'],
        ] as const).map(([v, label]) => (
          <button
            key={v}
            type="button"
            onClick={() => setTab(v)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition ${
              tab === v ? 'border-accent text-white' : 'border-transparent text-muted hover:text-white'
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === 'dashboard' ? (
        <Admin />
      ) : (
        <iframe src="/admin?embed=1" className="w-full border-0" style={{ height: 'calc(100vh - 110px)' }} />
      )}
    </div>
  );
}
