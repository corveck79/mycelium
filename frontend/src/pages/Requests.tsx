import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';

export default function Requests() {
  const { data, isLoading } = useQuery({ queryKey: ['my-requests'], queryFn: api.myRequests });
  if (isLoading) return <div className="text-muted">Loading…</div>;
  const items = data?.items || [];
  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-lg font-bold mb-3">My requests</h2>
        {items.length === 0 ? (
          <div className="text-center py-8">
            <div className="text-5xl mb-3">📋</div>
            <h2 className="text-lg font-semibold mb-1">No requests yet</h2>
            <p className="text-muted text-sm">Anything you add from Discover shows up here.</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-muted uppercase border-b border-border">
              <tr>
                <th className="text-left py-2 px-3">Title</th>
                <th className="text-left py-2 px-3">Type</th>
                <th className="text-left py-2 px-3">Status</th>
                <th className="text-left py-2 px-3">Requested</th>
                <th className="text-left py-2 px-3">Note</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r: any) => (
                <tr key={r.id} className="border-b border-border/50 hover:bg-card">
                  <td className="py-2 px-3 font-medium">{r.title}</td>
                  <td className="py-2 px-3 text-muted">{r.media_type}</td>
                  <td className="py-2 px-3">
                    <StatusPill status={r.status} />
                  </td>
                  <td className="py-2 px-3 text-muted text-xs">{r.created_at}</td>
                  <td className="py-2 px-3 text-muted text-xs">{r.note || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <FailedRequestsPanel />
    </div>
  );
}

function FailedRequestsPanel() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ['failed-requests'], queryFn: api.failedRequests, refetchInterval: 10000 });
  const retryMut = useMutation({
    mutationFn: (id: number) => api.retryRequest(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['failed-requests'] });
    },
  });

  const items = data?.items || [];
  if (items.length === 0) return null;

  return (
    <section>
      <h2 className="text-lg font-bold mb-3 text-red-400">Failed requests</h2>
      <p className="text-muted text-xs mb-3">
        These requests failed to find a stream. The system retries automatically — you can also retry manually.
      </p>
      <table className="w-full text-sm">
        <thead className="text-xs text-muted uppercase border-b border-border">
          <tr>
            <th className="text-left py-2 px-3">Title</th>
            <th className="text-left py-2 px-3">Type</th>
            <th className="text-left py-2 px-3">Error</th>
            <th className="text-left py-2 px-3">Updated</th>
            <th className="text-right py-2 px-3">Action</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r: any) => (
            <tr key={r.id} className="border-b border-border/50 hover:bg-card">
              <td className="py-2 px-3 font-medium">{r.title}</td>
              <td className="py-2 px-3 text-muted">{r.media_type}</td>
              <td className="py-2 px-3 text-red-400 text-xs max-w-xs truncate" title={r.error || ''}>
                {r.error || '—'}
              </td>
              <td className="py-2 px-3 text-muted text-xs">{r.updated_at}</td>
              <td className="py-2 px-3 text-right">
                <button
                  onClick={() => retryMut.mutate(r.id)}
                  disabled={retryMut.isPending}
                  className="px-3 py-1 rounded bg-accent/20 text-accent text-xs hover:bg-accent/30 disabled:opacity-50"
                >
                  ↺ Retry
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function StatusPill({ status }: { status: string }) {
  const cls =
    status === 'approved' ? 'bg-ok/20 text-ok' :
    status === 'denied' ? 'bg-red-500/20 text-red-400' :
    status === 'failed' ? 'bg-red-500/20 text-red-400' :
    'bg-amber/20 text-amber';
  return <span className={`px-2 py-0.5 rounded text-xs font-semibold capitalize ${cls}`}>{status}</span>;
}
