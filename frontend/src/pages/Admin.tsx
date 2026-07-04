import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import type { GenreRule } from '../api';
import { usePlugins } from '../hooks/usePlugins';

export default function Admin() {
  const qc = useQueryClient();
  const { data: pending } = useQuery({
    queryKey: ['user-requests', 'pending'],
    queryFn: () => api.userRequests('pending'),
  });
  const { data: users } = useQuery({ queryKey: ['users'], queryFn: api.users });
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });

  const { plugins } = usePlugins();
  // Collect all user_fields contributed by loaded plugins: [{field, label}]
  const pluginUserFields = plugins.flatMap(p =>
    (p.user_fields || []).map(f => ({
      field: f,
      label: p.user_field_labels?.[f] ?? f,
    }))
  );
  const isBootstrap = !users?.users || users.users.length === 0;

  const approveMut = useMutation({
    mutationFn: (id: number) => api.approveRequest(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-requests'] }),
  });
  const denyMut = useMutation({
    mutationFn: ({ id, note }: { id: number; note?: string }) => api.denyRequest(id, note),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-requests'] }),
  });
  const updateMut = useMutation({
    mutationFn: ({ id, fields }: { id: number; fields: any }) => api.updateUser(id, fields),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
  });
  const deleteMut = useMutation({
    mutationFn: (id: number) => api.deleteUser(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
  });

  return (
    <div className="space-y-8">
      {isBootstrap && (
        <div className="bg-accent/10 border border-accent/30 rounded-lg p-4 text-sm">
          <strong>First-run bootstrap:</strong> No users exist yet. The first account you create
          will become the admin.
        </div>
      )}

      <section>
        <h2 className="text-lg font-bold mb-3">Pending requests</h2>
        {!pending?.items || pending.items.length === 0 ? (
          <div className="text-muted text-sm">No pending requests</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-muted uppercase border-b border-border">
              <tr>
                <th className="text-left py-2 px-3">User</th>
                <th className="text-left py-2 px-3">Title</th>
                <th className="text-left py-2 px-3">Type</th>
                <th className="text-left py-2 px-3">IMDB</th>
                <th className="text-left py-2 px-3">Requested</th>
                <th className="text-right py-2 px-3">Action</th>
              </tr>
            </thead>
            <tbody>
              {pending.items.map((r) => (
                <tr key={r.id} className="border-b border-border/50">
                  <td className="py-2 px-3 font-medium">{r.username}</td>
                  <td className="py-2 px-3">{r.title}</td>
                  <td className="py-2 px-3 text-muted">{r.media_type}</td>
                  <td className="py-2 px-3 text-muted text-xs">
                    <a href={`https://www.imdb.com/title/${r.imdb_id}/`} target="_blank" rel="noopener">{r.imdb_id}</a>
                  </td>
                  <td className="py-2 px-3 text-muted text-xs">{r.created_at}</td>
                  <td className="py-2 px-3 text-right">
                    <button
                      onClick={() => approveMut.mutate(r.id)}
                      className="px-3 py-1 rounded bg-ok/20 text-ok text-xs hover:bg-ok/30"
                    >
                      ✓ Approve
                    </button>
                    <button
                      onClick={() => denyMut.mutate({ id: r.id, note: prompt('Reason?') || '' })}
                      className="ml-2 px-3 py-1 rounded bg-red-500/20 text-red-400 text-xs hover:bg-red-500/30"
                    >
                      ✗ Deny
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section>
        <h2 className="text-lg font-bold mb-3">Users</h2>
        <CreateUserForm />
        {users?.users && users.users.length > 0 && (
          <table className="w-full text-sm mt-4">
            <thead className="text-xs text-muted uppercase border-b border-border">
              <tr>
                <th className="text-left py-2 px-3">Username</th>
                <th className="text-left py-2 px-3">Role</th>
                <th className="text-left py-2 px-3">Auto-approve</th>
                <th className="text-left py-2 px-3">Enabled</th>
                {pluginUserFields.map(({ field, label }) => (
                  <th key={field} className="text-left py-2 px-3">{label}</th>
                ))}
                <th className="text-left py-2 px-3">Last login</th>
                <th className="text-right py-2 px-3">Action</th>
              </tr>
            </thead>
            <tbody>
              {users.users.map((u) => (
                <tr key={u.id} className="border-b border-border/50">
                  <td className="py-2 px-3 font-medium">{u.username}</td>
                  <td className="py-2 px-3 text-muted">{u.role}</td>
                  <td className="py-2 px-3">
                    <Toggle
                      on={u.auto_approve}
                      onClick={() => updateMut.mutate({ id: u.id, fields: { auto_approve: !u.auto_approve } })}
                    />
                  </td>
                  <td className="py-2 px-3">
                    <Toggle
                      on={u.enabled}
                      onClick={() => updateMut.mutate({ id: u.id, fields: { enabled: !u.enabled } })}
                    />
                  </td>
                  {pluginUserFields.map(({ field }) => (
                    <td key={field} className="py-2 px-3">
                      <Toggle
                        on={!!(u as any)[field]}
                        onClick={() => updateMut.mutate({ id: u.id, fields: { [field]: !(u as any)[field] } })}
                      />
                    </td>
                  ))}
                  <td className="py-2 px-3 text-muted text-xs">{u.last_login || ' - '}</td>
                  <td className="py-2 px-3 text-right">
                    {session?.user?.id !== u.id && (
                      <button
                        onClick={() => confirm(`Delete ${u.username}?`) && deleteMut.mutate(u.id)}
                        className="px-3 py-1 rounded bg-red-500/20 text-red-400 text-xs hover:bg-red-500/30"
                      >
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <ArrImportPanel />
      <AutoApprovePanel />
      <DiscoverGenreTabsPanel />
      <MaintenancePanel />
    </div>
  );
}

function CreateUserForm() {
  const qc = useQueryClient();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<'user' | 'admin'>('user');
  const [autoApprove, setAutoApprove] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const mut = useMutation({
    mutationFn: () => api.createUser({ username, password, role, auto_approve: autoApprove }),
    onSuccess: (r) => {
      setMsg({ kind: 'ok', text: r.message || `Created ${username}` });
      setUsername('');
      setPassword('');
      qc.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (e: any) => setMsg({ kind: 'err', text: e.message }),
  });

  return (
    <div className="bg-card rounded-lg border border-border p-4">
      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-muted mb-1">Username</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="bg-bg border border-border rounded px-3 py-2 text-sm"
          />
        </div>
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-muted mb-1">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="bg-bg border border-border rounded px-3 py-2 text-sm"
          />
        </div>
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-muted mb-1">Role</label>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as any)}
            className="bg-bg border border-border rounded px-3 py-2 text-sm"
          >
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={autoApprove}
            onChange={(e) => setAutoApprove(e.target.checked)}
          />
          Auto-approve
        </label>
        <button
          type="button"
          disabled={!username || password.length < 4 || mut.isPending}
          onClick={() => mut.mutate()}
          className="px-4 py-2 rounded bg-accent hover:bg-accent/90 text-sm font-semibold
                      disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {mut.isPending ? 'Creating…' : 'Create user'}
        </button>
      </div>
      {msg && (
        <div className={`mt-3 text-xs ${msg.kind === 'ok' ? 'text-ok' : 'text-red-400'}`}>{msg.text}</div>
      )}
    </div>
  );
}

function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-10 h-5 rounded-full transition relative ${on ? 'bg-accent' : 'bg-border'}`}
    >
      <span
        className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
          on ? 'translate-x-5' : 'translate-x-0.5'
        }`}
      />
    </button>
  );
}

function ArrImportPanel() {
  const [msg, setMsg] = useState<string>('');
  const { data: s } = useQuery({
    queryKey: ['arr-import-status'],
    queryFn: api.arrStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : 5000),
  });
  const test = async (kind: 'radarr' | 'sonarr') => {
    setMsg(`Testing ${kind}…`);
    try {
      const r = await api.arrTest(kind);
      setMsg(r.ok ? `✓ ${kind} reachable` : `✗ ${kind} unreachable${r.error ? ': ' + r.error : ''}`);
    } catch (e: any) {
      setMsg(`✗ ${e.message}`);
    }
  };
  const run = async (kind: 'radarr' | 'sonarr') => {
    if (!confirm(`Start ${kind} import?`)) return;
    setMsg('');
    await api.arrRun(kind);
  };

  const pct = s && s.total > 0 ? Math.round((s.done / s.total) * 100) : 0;

  return (
    <section>
      <h2 className="text-lg font-bold mb-3">Radarr / Sonarr import</h2>
      <div className="bg-card rounded-lg border border-border p-4">
        <p className="text-muted text-sm mb-3">
          Configure RADARR_URL/SONARR_URL + API keys in Settings, then test and run import here.
        </p>
        <div className="flex gap-2 flex-wrap mb-3">
          <button onClick={() => test('radarr')} className="px-3 py-1.5 rounded border border-border text-sm hover:bg-bg">Test Radarr</button>
          <button onClick={() => run('radarr')} disabled={s?.running} className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50">▶ Import Radarr</button>
          <button onClick={() => test('sonarr')} className="px-3 py-1.5 rounded border border-border text-sm hover:bg-bg">Test Sonarr</button>
          <button onClick={() => run('sonarr')} disabled={s?.running} className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50">▶ Import Sonarr</button>
          <button
            onClick={async () => {
              if (!confirm('Import all Sonarr series + search for all episodes and create .strm files. This runs in the background and may take a while. Continue?')) return;
              setMsg('Series backfill started  -  runs in background, check logs for progress…');
              await fetch('/ui/api/series-backfill', { method: 'POST' });
            }}
            disabled={s?.running}
            className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
          >
            ▶ Sync all series + episodes
          </button>
        </div>

        {s && (s.running || s.total > 0) && (
          <div className="mb-3">
            <div className="flex justify-between text-xs text-muted mb-1">
              <span>
                {s.running ? `Importing ${s.kind}…` : `Finished ${s.kind || ''}`}  -  {s.message}
              </span>
              <span>{s.done}/{s.total} ({pct}%)</span>
            </div>
            <div className="w-full h-2 bg-bg rounded overflow-hidden">
              <div
                className={`h-full transition-all ${s.running ? 'bg-accent' : 'bg-ok'}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="flex gap-4 text-xs text-muted mt-1">
              <span className="text-ok">+{s.added} added</span>
              <span>⏭ {s.skipped} skipped</span>
              <span className="text-red-400">✗ {s.errors} errors</span>
            </div>
          </div>
        )}

        {msg && <div className="font-mono text-xs text-muted">{msg}</div>}
      </div>
    </section>
  );
}

function AutoApprovePanel() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState('');
  const { data } = useQuery({ queryKey: ['auto-approve-rules'], queryFn: api.autoApproveGenreRules });
  const { data: movieGenres } = useQuery({ queryKey: ['genres', 'movie'], queryFn: () => api.genres('movie') });
  const { data: tvGenres } = useQuery({ queryKey: ['genres', 'tv'], queryFn: () => api.genres('tv') });

  const [rules, setRules] = useState<GenreRule[] | null>(null);
  const effectiveRules = rules ?? data?.rules ?? [];

  const saveMutation = useMutation({
    mutationFn: (r: GenreRule[]) => api.setAutoApproveGenreRules(r),
    onSuccess: () => { setMsg('Saved.'); qc.invalidateQueries({ queryKey: ['auto-approve-rules'] }); },
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });
  const runMutation = useMutation({
    mutationFn: api.runAutoApproveNow,
    onSuccess: () => setMsg('Started in the background - check logs for progress.'),
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });

  const addRule = () => {
    const genres = movieGenres?.genres || [];
    const first = genres[0];
    setRules([
      ...effectiveRules,
      {
        media_type: 'movie', genre_id: first?.id || 0, genre_name: first?.name || '',
        year_from: null, year_to: null, enabled: true,
      },
    ]);
  };

  const updateRule = (i: number, patch: Partial<GenreRule>) => {
    const next = effectiveRules.map((r, idx) => (idx === i ? { ...r, ...patch } : r));
    setRules(next);
  };

  const removeRule = (i: number) => {
    setRules(effectiveRules.filter((_, idx) => idx !== i));
  };

  return (
    <section>
      <h2 className="text-lg font-bold mb-3">Auto-approve (genres + favorite actors)</h2>
      <div className="bg-card rounded-lg border border-border p-4 space-y-4">
        <p className="text-muted text-sm">
          Automatically request titles matching enabled genre rules (year-ranged) and any
          user's followed actors, up to the daily caps in Settings &gt; Auto-approve.
        </p>

        <GenreRuleRows
          rules={effectiveRules}
          movieGenres={movieGenres?.genres || []}
          tvGenres={tvGenres?.genres || []}
          onUpdate={updateRule}
          onRemove={removeRule}
        />

        <div className="flex flex-wrap gap-2">
          <button onClick={addRule} className="px-3 py-1.5 rounded border border-border text-sm hover:bg-bg">
            + Add genre rule
          </button>
          <button
            onClick={() => saveMutation.mutate(effectiveRules)}
            disabled={saveMutation.isPending}
            className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
          >
            {saveMutation.isPending ? 'Saving...' : 'Save rules'}
          </button>
          <button
            onClick={() => runMutation.mutate()}
            disabled={runMutation.isPending}
            className="px-3 py-1.5 rounded border border-border text-sm hover:bg-bg disabled:opacity-50"
          >
            ▶ Run now
          </button>
        </div>

        {msg && <div className="font-mono text-xs text-muted">{msg}</div>}
      </div>
    </section>
  );
}

function GenreRuleRows({
  rules,
  movieGenres,
  tvGenres,
  onUpdate,
  onRemove,
}: {
  rules: GenreRule[];
  movieGenres: Array<{ id: number; name: string }>;
  tvGenres: Array<{ id: number; name: string }>;
  onUpdate: (i: number, patch: Partial<GenreRule>) => void;
  onRemove: (i: number) => void;
}) {
  return (
    <div className="space-y-2">
      {rules.map((rule, i) => {
        const genres = rule.media_type === 'movie' ? movieGenres : tvGenres;
        return (
          <div key={i} className="flex flex-wrap items-center gap-2 bg-bg rounded-lg p-2">
            <button
              type="button"
              onClick={() => onUpdate(i, { enabled: !rule.enabled })}
              className={`w-9 h-5 rounded-full transition-colors flex items-center px-0.5 flex-shrink-0
                ${rule.enabled ? 'bg-accent' : 'bg-border'}`}
            >
              <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform
                ${rule.enabled ? 'translate-x-4' : 'translate-x-0'}`} />
            </button>
            <select
              value={rule.media_type}
              onChange={(e) => {
                const mt = e.target.value as 'movie' | 'tv';
                const g = (mt === 'movie' ? movieGenres : tvGenres)?.[0];
                onUpdate(i, { media_type: mt, genre_id: g?.id || 0, genre_name: g?.name || '' });
              }}
              className="bg-card border border-border rounded px-2 py-1 text-xs"
            >
              <option value="movie">Movies</option>
              <option value="tv">Shows</option>
            </select>
            <select
              value={rule.genre_id}
              onChange={(e) => {
                const g = genres.find((x) => x.id === Number(e.target.value));
                onUpdate(i, { genre_id: g?.id || 0, genre_name: g?.name || '' });
              }}
              className="bg-card border border-border rounded px-2 py-1 text-xs"
            >
              {genres.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            </select>
            <input
              type="number"
              placeholder="From year"
              value={rule.year_from ?? ''}
              onChange={(e) => onUpdate(i, { year_from: e.target.value ? Number(e.target.value) : null })}
              className="w-24 bg-card border border-border rounded px-2 py-1 text-xs"
            />
            <span className="text-muted text-xs">to</span>
            <input
              type="number"
              placeholder="To year"
              value={rule.year_to ?? ''}
              onChange={(e) => onUpdate(i, { year_to: e.target.value ? Number(e.target.value) : null })}
              className="w-24 bg-card border border-border rounded px-2 py-1 text-xs"
            />
            <button
              type="button"
              onClick={() => onRemove(i)}
              className="ml-auto px-2 py-1 rounded text-xs text-red-400 hover:bg-red-500/10"
            >
              Remove
            </button>
          </div>
        );
      })}
    </div>
  );
}

function DiscoverGenreTabsPanel() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState('');
  const { data } = useQuery({ queryKey: ['discover-genre-tabs-config'], queryFn: api.genreTabsConfig });
  const { data: movieGenres } = useQuery({ queryKey: ['genres', 'movie'], queryFn: () => api.genres('movie') });
  const { data: tvGenres } = useQuery({ queryKey: ['genres', 'tv'], queryFn: () => api.genres('tv') });

  const [tabs, setTabs] = useState<GenreRule[] | null>(null);
  const effectiveTabs = tabs ?? data?.tabs ?? [];

  const saveMutation = useMutation({
    mutationFn: (t: GenreRule[]) => api.setGenreTabsConfig(t),
    onSuccess: () => {
      setMsg('Saved.');
      qc.invalidateQueries({ queryKey: ['discover-genre-tabs-config'] });
      qc.invalidateQueries({ queryKey: ['genre-tabs'] });
    },
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });

  const addTab = () => {
    const genres = movieGenres?.genres || [];
    const first = genres[0];
    setTabs([
      ...effectiveTabs,
      { media_type: 'movie', genre_id: first?.id || 0, genre_name: first?.name || '',
        year_from: null, year_to: null, enabled: true },
    ]);
  };
  const updateTab = (i: number, patch: Partial<GenreRule>) => {
    setTabs(effectiveTabs.map((t, idx) => (idx === i ? { ...t, ...patch } : t)));
  };
  const removeTab = (i: number) => {
    setTabs(effectiveTabs.filter((_, idx) => idx !== i));
  };

  return (
    <section>
      <h2 className="text-lg font-bold mb-3">Discover genre tabs</h2>
      <div className="bg-card rounded-lg border border-border p-4 space-y-4">
        <p className="text-muted text-sm">
          Extra rows shown on the Discover page for browsing by genre, optionally bounded
          by a year range. Purely for browsing - use Auto-approve above to also auto-download.
        </p>

        <GenreRuleRows
          rules={effectiveTabs}
          movieGenres={movieGenres?.genres || []}
          tvGenres={tvGenres?.genres || []}
          onUpdate={updateTab}
          onRemove={removeTab}
        />

        <div className="flex flex-wrap gap-2">
          <button onClick={addTab} className="px-3 py-1.5 rounded border border-border text-sm hover:bg-bg">
            + Add genre tab
          </button>
          <button
            onClick={() => saveMutation.mutate(effectiveTabs)}
            disabled={saveMutation.isPending}
            className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
          >
            {saveMutation.isPending ? 'Saving...' : 'Save tabs'}
          </button>
        </div>

        {msg && <div className="font-mono text-xs text-muted">{msg}</div>}
      </div>
    </section>
  );
}

function MaintenancePanel() {
  const [result, setResult] = useState<string>('');
  const [busy, setBusy] = useState(false);

  const repairStrms = async () => {
    setBusy(true);
    setResult('Scanning .strm files…');
    try {
      const r = await fetch('/ui/api/repair-strms', { method: 'POST' });
      const data = await r.json();
      const parts: string[] = [`scanned: ${data.scanned}`, `ok: ${data.ok}`];
      if (data.missing_strm) parts.push(`missing strm fixed: ${data.missing_strm}`);
      if (data.orphaned_tokens) parts.push(`orphaned tokens fixed: ${data.orphaned_tokens}`);
      if (data.relinked) parts.push(`relinked: ${data.relinked}`);
      if (data.requeued) parts.push(`requeued: ${data.requeued}`);
      if (data.skipped) parts.push(`skipped: ${data.skipped}`);
      const allGood = !data.missing_strm && !data.orphaned_tokens && !data.relinked && !data.requeued;
      setResult('Done  -  ' + parts.join(', ') + (allGood ? '  -  all links look good' : ''));
    } catch (e: any) {
      setResult(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const cleanupDuplicateStrms = async () => {
    setBusy(true);
    setResult('Scanning for duplicate .strm files…');
    try {
      const r = await fetch('/ui/api/cleanup-duplicate-strms', { method: 'POST' });
      const data = await r.json();
      const parts = [`scanned: ${data.scanned}`, `cleaned: ${data.cleaned}`];
      if (data.skipped) parts.push(`skipped: ${data.skipped}`);
      setResult('Done  -  ' + parts.join(', ') + (data.cleaned > 0 ? '  -  do a full Jellyfin library rescan now' : '  -  nothing to clean'));
    } catch (e: any) {
      setResult(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const scanTorboxLibrary = async () => {
    setBusy(true);
    setResult('Scanning TorBox library…');
    try {
      const r = await fetch('/ui/api/torbox/scan-library', { method: 'POST' });
      const data = await r.json();
      const parts = [
        `scanned: ${data.scanned}`,
        `imported: ${data.imported}`,
        `skipped: ${data.skipped}`,
        ...(data.failed ? [`failed: ${data.failed}`] : []),
      ];
      setResult('Done  -  ' + parts.join(', ') + (data.imported > 0 ? '  -  do a full Jellyfin library rescan now' : ''));
    } catch (e: any) {
      setResult(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const migrateCanonical = async () => {
    if (!confirm('This renames movie folders to TMDB canonical names and removes duplicates. Jellyfin needs a full rescan afterwards. Continue?')) return;
    setBusy(true);
    setResult('Migrating to canonical names…');
    try {
      const r = await fetch('/ui/api/migrate-canonical', { method: 'POST' });
      const data = await r.json();
      const parts = [
        `scanned: ${data.scanned}`,
        `renamed: ${data.renamed}`,
        `duplicates removed: ${data.merged}`,
        `skipped: ${data.skipped}`,
        `no imdb_id: ${data.no_imdb}`,
        ...(data.errors ? [`errors: ${data.errors}`] : []),
      ];
      setResult('Done  -  ' + parts.join(', ') + '  -  do a full Jellyfin library rescan now');
    } catch (e: any) {
      setResult(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2 className="text-lg font-bold mb-3">Maintenance</h2>
      <div className="bg-card rounded-lg border border-border p-4 space-y-4">
        <div>
          <p className="text-sm font-medium mb-1">Migrate to canonical names</p>
          <p className="text-muted text-xs mb-2">
            Renames all movie folders to TMDB canonical names (imdb_id is leading).
            Merges duplicates (e.g. Cyrillic + English for same film). Run once, then do a
            full Jellyfin library rescan.
          </p>
          <button
            onClick={migrateCanonical}
            disabled={busy}
            className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
          >
            {busy ? 'Migrating…' : 'Migrate to canonical names'}
          </button>
        </div>
        <div className="border-t border-border pt-3">
          <p className="text-sm font-medium mb-1">Clean up duplicate .strm files</p>
          <p className="text-muted text-xs mb-2">
            Removes extra .strm files from folders that have more than one (e.g. after
            migration left behind old Cyrillic filenames). Keeps the file matching the
            folder name. Run once after migration, then rescan Jellyfin.
          </p>
          <button
            onClick={cleanupDuplicateStrms}
            disabled={busy}
            className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
          >
            {busy ? 'Cleaning…' : 'Clean up duplicate strm files'}
          </button>
        </div>
        <div className="border-t border-border pt-3">
          <p className="text-sm font-medium mb-1">Repair broken .strm links</p>
          <p className="text-muted text-xs mb-2">
            Scans movie .strm files for expired direct TorBox CDN URLs. Re-links them to a
            catbox proxy token (if available) or deletes and requeues for reprocessing.
          </p>
          <button
            onClick={repairStrms}
            disabled={busy}
            className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
          >
            {busy ? 'Scanning…' : 'Repair broken strm files'}
          </button>
        </div>
        <div className="border-t border-border pt-3">
          <p className="text-sm font-medium mb-1">Scan TorBox library</p>
          <p className="text-muted text-xs mb-2">
            Reads everything already cached in your TorBox account and creates .strm files
            for anything Mycelium has no record of (e.g. after a database reset, or content
            added outside Mycelium). Resolves titles via TMDB where possible.
          </p>
          <button
            onClick={scanTorboxLibrary}
            disabled={busy}
            className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
          >
            {busy ? 'Scanning…' : 'Scan TorBox library'}
          </button>
        </div>
        {result && <div className="font-mono text-xs text-muted border-t border-border pt-2">{result}</div>}
      </div>
    </section>
  );
}
