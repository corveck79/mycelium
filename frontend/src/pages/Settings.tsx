import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import { usePlugins } from '../hooks/usePlugins';
import PluginSettingsCard from '../components/PluginSettingsCard';

export default function Settings() {
  const { plugins } = usePlugins();
  const pluginsWithUi = plugins.filter(p => p.settings_ui);
  const pluginsWithFields = plugins.filter(p => p.user_fields?.length > 0);

  return (
    <div className="space-y-6">
      <div className="bg-card rounded-lg border border-border p-6">
        <h2 className="text-lg font-bold mb-2">Settings</h2>
        <p className="text-muted text-sm mb-4">
          Runtime settings are managed in the admin panel.
        </p>
        <a
          href="/admin#settings"
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-accent text-sm font-semibold"
        >
          Open Settings
        </a>
      </div>

      {pluginsWithFields.length > 0 && (
        <div className="bg-card rounded-lg border border-border p-6">
          <h2 className="text-base font-bold mb-1">Plugins</h2>
          <p className="text-muted text-xs mb-4">Enable or disable plugin features for your account.</p>
          <div className="space-y-3">
            {pluginsWithFields.map(plugin => (
              <PluginUserFieldsRow key={plugin.name} plugin={plugin} />
            ))}
          </div>
        </div>
      )}

      {pluginsWithUi.map(plugin => (
        <PluginSettingsCard key={plugin.name} plugin={plugin} />
      ))}
    </div>
  );
}

function PluginUserFieldsRow({ plugin }: { plugin: ReturnType<typeof usePlugins>['plugins'][number] }) {
  const qc = useQueryClient();
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });

  const mutation = useMutation({
    mutationFn: (fields: Record<string, boolean>) => api.setPluginFields(fields),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['session'] }),
  });

  return (
    <div className="flex items-center justify-between py-2 border-b border-border last:border-0">
      <div>
        <span className="text-sm font-medium">{plugin.label}</span>
        {plugin.description && (
          <p className="text-xs text-muted mt-0.5">{plugin.description}</p>
        )}
      </div>
      <div className="flex items-center gap-3 flex-shrink-0 ml-4">
        {plugin.user_fields.map(field => {
          const label = plugin.user_field_labels?.[field] || field;
          const value = !!(session?.user as any)?.[field];
          return (
            <label key={field} className="flex items-center gap-2 cursor-pointer select-none">
              <span className="text-xs text-muted">{label}</span>
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
                <span
                  className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform
                    ${value ? 'translate-x-4' : 'translate-x-1'}`}
                />
              </button>
            </label>
          );
        })}
      </div>
    </div>
  );
}
