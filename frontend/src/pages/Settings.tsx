import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { usePlugins } from '../hooks/usePlugins';
import PluginSettingsCard from '../components/PluginSettingsCard';

export default function Settings() {
  const { plugins } = usePlugins();
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });

  const visiblePlugins = plugins.filter(p => {
    const anyFieldEnabled = (p.user_fields || []).some(f => !!(session?.user as any)?.[f]);
    return anyFieldEnabled || !!p.settings_ui;
  });

  return (
    <div className="space-y-6">
      <ChangePasswordCard />
      <PreferencesCard />
      <TraktCard />
      <MDBListCard />

      {visiblePlugins.length > 0 && (
        <>
          <div>
            <h1 className="text-xl font-bold mb-1">Plugins</h1>
            <p className="text-muted text-sm">Enable features and connect accounts for your profile.</p>
          </div>
          {visiblePlugins.map(plugin => (
            <PluginCard key={plugin.name} plugin={plugin} session={session} />
          ))}
        </>
      )}
    </div>
  );
}

function PluginCard({ plugin, session }: {
  plugin: ReturnType<typeof usePlugins>['plugins'][number];
  session: any;
}) {
  const hasFields = plugin.user_fields?.length > 0;
  const hasUi = !!plugin.settings_ui;

  // User-field toggles: only show if the admin has already enabled at least one
  // field for this user. This keeps toggles admin-controlled  -  users can turn
  // off what they have access to, but cannot self-grant new access.
  const anyFieldEnabled = hasFields &&
    plugin.user_fields.some(f => !!(session?.user as any)?.[f]);

  if (!anyFieldEnabled && !hasUi) return null;

  return (
    <div className="bg-card rounded-lg border border-border p-6 space-y-4">
      <div>
        <h2 className="text-base font-bold leading-tight">{plugin.label}</h2>
        {plugin.description && (
          <p className="text-muted text-xs mt-0.5">{plugin.description}</p>
        )}
      </div>

      {anyFieldEnabled && <PluginUserFieldsSection plugin={plugin} />}
      {hasUi && <PluginSettingsCard plugin={plugin} embedded />}
    </div>
  );
}

function PluginUserFieldsSection({ plugin }: { plugin: ReturnType<typeof usePlugins>['plugins'][number] }) {
  const qc = useQueryClient();
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });
  const mutation = useMutation({
    mutationFn: (fields: Record<string, boolean>) => api.setPluginFields(fields),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['session'] }),
  });

  return (
    <div className="flex flex-wrap gap-4">
      {plugin.user_fields.map(field => {
        const label = plugin.user_field_labels?.[field] || field;
        const value = !!(session?.user as any)?.[field];
        return (
          <label key={field} className="flex items-center gap-2 cursor-pointer select-none">
            <span className="text-sm text-muted">{label}</span>
            <button
              type="button"
              role="switch"
              aria-checked={value}
              onClick={() => mutation.mutate({ [field]: !value })}
              disabled={mutation.isPending}
              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors
                ${value ? 'bg-accent' : 'bg-zinc-600'}
                ${mutation.isPending ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
            >
              <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform
                ${value ? 'translate-x-4' : 'translate-x-1'}`} />
            </button>
            <span className={`text-xs font-medium ${value ? 'text-accent' : 'text-muted'}`}>
              {value ? 'On' : 'Off'}
            </span>
          </label>
        );
      })}
    </div>
  );
}

function PreferencesCard() {
  const qc = useQueryClient();
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });
  const clickJellyfin = !!(session?.user as any)?.library_click_jellyfin;
  const jellyfinUrl = session?.jellyfin_url;

  const mutation = useMutation({
    mutationFn: (prefs: Record<string, boolean | string>) => api.setPreferences(prefs),
    onError: () => {
      // Revert optimistic update on failure
      qc.invalidateQueries({ queryKey: ['session'] });
    },
  });

  const toggle = () => {
    const newVal = !clickJellyfin;
    // Optimistic update: immediately flip in the shared session cache so
    // Library.tsx (which reads the same cache) picks it up without a reload.
    qc.setQueryData(['session'], (old: any) =>
      old ? { ...old, user: { ...old.user, library_click_jellyfin: newVal } } : old,
    );
    mutation.mutate({ library_click_jellyfin: newVal });
  };

  const [includeLang, setIncludeLang] = useState((session?.user as any)?.discover_language_include || '');
  const [excludeLang, setExcludeLang] = useState((session?.user as any)?.discover_language_exclude || '');
  useEffect(() => {
    setIncludeLang((session?.user as any)?.discover_language_include || '');
    setExcludeLang((session?.user as any)?.discover_language_exclude || '');
  }, [session]);

  const saveLanguages = () => {
    mutation.mutate({
      discover_language_include: includeLang,
      discover_language_exclude: excludeLang,
    });
  };

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h2 className="text-base font-bold mb-1">Preferences</h2>
      <p className="text-muted text-xs mb-4">Personalise how the app behaves for your account.</p>
      <div className="space-y-3">
        <label className="flex items-start gap-3 cursor-pointer select-none" onClick={toggle}>
          <div className="mt-0.5 flex-shrink-0">
            <div className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5
                ${clickJellyfin ? 'bg-accent' : 'bg-border'}`}
            >
              <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform
                ${clickJellyfin ? 'translate-x-5' : 'translate-x-0'}`} />
            </div>
          </div>
          <div>
            <div className="text-sm font-medium">Open library items in Jellyfin</div>
            <div className="text-xs text-muted mt-0.5">
              Clicking a poster in the Library tab opens the item in Jellyfin web instead of showing the detail modal.
              {!jellyfinUrl && (
                <span className="text-yellow-400 ml-1">(Jellyfin URL not configured)</span>
              )}
            </div>
          </div>
        </label>

        <div className="border-t border-border pt-3">
          <div className="text-sm font-medium mb-1">Discover language filter</div>
          <p className="text-xs text-muted mb-2">
            Comma-separated ISO 639-1 codes (e.g. <code>en,nl</code>). Only-include takes priority over exclude.
            Leave both empty to show everything.
          </p>
          <div className="flex flex-col sm:flex-row gap-2">
            <input
              type="text"
              value={includeLang}
              onChange={(e) => setIncludeLang(e.target.value)}
              placeholder="Only show (e.g. en,nl)"
              className="flex-1 bg-bg border border-border rounded-lg px-3 py-2 text-sm
                         placeholder:text-muted focus:outline-none focus:border-accent"
            />
            <input
              type="text"
              value={excludeLang}
              onChange={(e) => setExcludeLang(e.target.value)}
              placeholder="Hide (e.g. ru,hi)"
              className="flex-1 bg-bg border border-border rounded-lg px-3 py-2 text-sm
                         placeholder:text-muted focus:outline-none focus:border-accent"
            />
            <button
              onClick={saveLanguages}
              disabled={mutation.isPending}
              className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50 whitespace-nowrap"
            >
              {mutation.isPending ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


function TraktCard() {
  const qc = useQueryClient();
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });
  const connected = !!(session?.user as any)?.trakt_connected;

  const [pairing, setPairing] = useState<{ user_code: string; verification_url: string } | null>(null);
  const [syncResult, setSyncResult] = useState('');
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollDeadline = useRef<number>(0);

  useEffect(() => () => { if (pollTimer.current) clearInterval(pollTimer.current); }, []);

  const startMutation = useMutation({
    mutationFn: api.traktAuthStart,
    onSuccess: (data: any) => {
      setPairing({ user_code: data.user_code, verification_url: data.verification_url });
      pollDeadline.current = Date.now() + (data.expires_in || 600) * 1000;
      const intervalMs = Math.max(2, data.interval || 5) * 1000;
      if (pollTimer.current) clearInterval(pollTimer.current);
      pollTimer.current = setInterval(async () => {
        if (Date.now() > pollDeadline.current) {
          clearInterval(pollTimer.current!);
          setPairing(null);
          return;
        }
        try {
          const r = await api.traktAuthPoll();
          if (r.status === 'success') {
            clearInterval(pollTimer.current!);
            setPairing(null);
            qc.invalidateQueries({ queryKey: ['session'] });
          } else if (r.status === 'error') {
            clearInterval(pollTimer.current!);
            setPairing(null);
          }
        } catch { /* keep polling */ }
      }, intervalMs);
    },
  });

  const revokeMutation = useMutation({
    mutationFn: api.traktRevoke,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['session'] }),
  });

  const syncMutation = useMutation({
    mutationFn: api.traktSync,
    onSuccess: (data: any) => setSyncResult(`Queued ${data.added} new watchlist item${data.added === 1 ? '' : 's'}.`),
    onError: (e: any) => setSyncResult(`Error: ${e.message}`),
  });

  const syncWatchedMutation = useMutation({
    mutationFn: api.traktSyncWatched,
    onSuccess: (data: any) => setSyncResult(`Synced ${data.watched} watched item${data.watched === 1 ? '' : 's'}.`),
    onError: (e: any) => setSyncResult(`Error: ${e.message}`),
  });

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h2 className="text-base font-bold mb-1">Trakt</h2>
      <p className="text-muted text-xs mb-4">
        Connect your Trakt account to auto-request items from your watchlist and show watched badges.
      </p>

      {connected ? (
        <div className="space-y-3">
          <p className="text-sm text-ok">Connected as {(session?.user as any)?.trakt_username || 'your Trakt account'}.</p>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => syncMutation.mutate()}
              disabled={syncMutation.isPending}
              className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
            >
              {syncMutation.isPending ? 'Syncing...' : 'Sync watchlist now'}
            </button>
            <button
              onClick={() => syncWatchedMutation.mutate()}
              disabled={syncWatchedMutation.isPending}
              className="px-3 py-1.5 rounded border border-border text-sm font-medium disabled:opacity-50"
            >
              {syncWatchedMutation.isPending ? 'Syncing...' : 'Sync watched status'}
            </button>
            <button
              onClick={() => revokeMutation.mutate()}
              disabled={revokeMutation.isPending}
              className="px-3 py-1.5 rounded border border-danger text-danger text-sm font-medium disabled:opacity-50"
            >
              Disconnect
            </button>
          </div>
          {syncResult && <p className="text-xs text-muted">{syncResult}</p>}
        </div>
      ) : pairing ? (
        <div className="space-y-2">
          <p className="text-sm">
            Go to <a href={pairing.verification_url} target="_blank" rel="noreferrer" className="text-accent underline">
              {pairing.verification_url}
            </a> and enter this code:
          </p>
          <p className="text-2xl font-mono font-bold tracking-widest">{pairing.user_code}</p>
          <p className="text-xs text-muted">Waiting for confirmation...</p>
        </div>
      ) : (
        <div className="space-y-2">
          <button
            onClick={() => startMutation.mutate()}
            disabled={startMutation.isPending}
            className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/90 disabled:opacity-60 font-semibold text-sm"
          >
            {startMutation.isPending ? 'Starting...' : 'Connect Trakt'}
          </button>
          {startMutation.isError && (
            <p className="text-danger text-xs">{(startMutation.error as any)?.message || 'Could not start Trakt authorization'}</p>
          )}
        </div>
      )}
    </div>
  );
}


function MDBListCard() {
  const qc = useQueryClient();
  const { data: status } = useQuery({ queryKey: ['mdblist-status'], queryFn: api.mdblistStatus });
  const { data: listsData } = useQuery({
    queryKey: ['mdblist-lists'],
    queryFn: api.mdblistLists,
    enabled: !!status?.connected,
  });
  const [apiKey, setApiKey] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    setSelected((status?.list_ids || '').split(',').filter(Boolean));
  }, [status]);

  const connectMutation = useMutation({
    mutationFn: () => api.mdblistConnect(apiKey),
    onSuccess: () => { setApiKey(''); qc.invalidateQueries({ queryKey: ['mdblist-status'] }); },
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });
  const disconnectMutation = useMutation({
    mutationFn: api.mdblistDisconnect,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['mdblist-status'] }),
  });
  const setListsMutation = useMutation({
    mutationFn: (ids: string[]) => api.mdblistSetLists(ids),
    onSuccess: () => setMsg('Lists saved.'),
  });
  const syncMutation = useMutation({
    mutationFn: api.mdblistSync,
    onSuccess: (data) => setMsg(`Queued ${data.added} new item${data.added === 1 ? '' : 's'}.`),
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });

  const toggleList = (id: string) => {
    const next = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
    setSelected(next);
    setListsMutation.mutate(next);
  };

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h2 className="text-base font-bold mb-1">MDBList</h2>
      <p className="text-muted text-xs mb-4">
        Connect your MDBList API key (from{' '}
        <a href="https://mdblist.com/preferences" target="_blank" rel="noreferrer" className="text-accent underline">
          mdblist.com/preferences
        </a>) to sync your lists and auto-request new items.
      </p>

      {status?.connected ? (
        <div className="space-y-3">
          {listsData?.lists && listsData.lists.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs text-muted mb-1">Lists to sync:</p>
              {listsData.lists.map((l) => (
                <label key={l.id} className="flex items-center gap-2 text-sm cursor-pointer">
                  <input
                    type="checkbox"
                    checked={selected.includes(String(l.id))}
                    onChange={() => toggleList(String(l.id))}
                  />
                  {l.name}
                </label>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => syncMutation.mutate()}
              disabled={syncMutation.isPending}
              className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
            >
              {syncMutation.isPending ? 'Syncing...' : 'Sync now'}
            </button>
            <button
              onClick={() => disconnectMutation.mutate()}
              disabled={disconnectMutation.isPending}
              className="px-3 py-1.5 rounded border border-danger text-danger text-sm font-medium disabled:opacity-50"
            >
              Disconnect
            </button>
          </div>
          {msg && <p className="text-xs text-muted">{msg}</p>}
        </div>
      ) : (
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="text"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="MDBList API key"
            className="flex-1 bg-bg border border-border rounded-lg px-3 py-2 text-sm
                       placeholder:text-muted focus:outline-none focus:border-accent"
          />
          <button
            onClick={() => connectMutation.mutate()}
            disabled={connectMutation.isPending || !apiKey}
            className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/90 disabled:opacity-60 font-semibold text-sm whitespace-nowrap"
          >
            {connectMutation.isPending ? 'Connecting...' : 'Connect'}
          </button>
        </div>
      )}
    </div>
  );
}


function ChangePasswordCard() {
  const [current, setCurrent] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);

  const mutation = useMutation({
    mutationFn: () => api.changePassword(current, password),
    onSuccess: () => {
      setSuccess(true);
      setCurrent(''); setPassword(''); setConfirm('');
      setTimeout(() => setSuccess(false), 3000);
    },
    onError: (e: any) => setError(e.message || 'Failed to change password'),
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    if (password.length < 6) { setError('At least 6 characters required'); return; }
    if (password !== confirm) { setError('Passwords do not match'); return; }
    mutation.mutate();
  };

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h2 className="text-base font-bold mb-4">Change password</h2>
      {success && <p className="text-ok text-sm mb-3">Password changed successfully.</p>}
      <form onSubmit={submit} className="space-y-3 max-w-sm">
        <div>
          <label className="block text-xs text-muted mb-1">Current password</label>
          <input type="password" value={current} onChange={e => setCurrent(e.target.value)}
            className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent" />
        </div>
        <div>
          <label className="block text-xs text-muted mb-1">New password</label>
          <input type="password" value={password} onChange={e => setPassword(e.target.value)}
            className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent" />
        </div>
        <div>
          <label className="block text-xs text-muted mb-1">Confirm new password</label>
          <input type="password" value={confirm} onChange={e => setConfirm(e.target.value)}
            className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent" />
        </div>
        {error && <p className="text-danger text-xs">{error}</p>}
        <button type="submit" disabled={mutation.isPending}
          className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/90 disabled:opacity-60 font-semibold text-sm">
          {mutation.isPending ? 'Saving...' : 'Change password'}
        </button>
      </form>
    </div>
  );
}
