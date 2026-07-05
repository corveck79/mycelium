import { useQuery } from '@tanstack/react-query'

interface ConfigGate {
  field:      string
  message:    string
  link?:      string
  link_label?: string
}

interface OAuthDeviceSpec {
  connected_field: string
  username_field?: string
  synced_field?:   string
  start_url:  string
  poll_url:   string
  revoke_url: string
}

interface ActionSpec {
  label:            string
  url:              string
  method?:          string
  show_if?:         string
  success_key?:     string
  success_template?: string
}

export interface PluginSettingsUi {
  status_url:   string
  config_gate?: ConfigGate
  oauth_device?: OAuthDeviceSpec
  actions?:     ActionSpec[]
}

export interface PluginMeta {
  name:              string
  label:             string
  version:           string
  description:       string
  user_fields:       string[]
  user_field_labels: Record<string, string>
  settings_ui?:      PluginSettingsUi
}

function usePlugins() {
  const { data } = useQuery<{ plugins: PluginMeta[] }>({
    queryKey: ['plugins'],
    queryFn:  () => fetch('/ui/api/plugins').then(r => {
      if (!r.ok) throw new Error(`${r.status}`)
      return r.json()
    }),
    staleTime: Infinity,  // plugins don't change at runtime
  })
  return {
    plugins:  data?.plugins ?? [],
    isLoaded: (name: string) => data?.plugins.some(p => p.name === name) ?? false,
  }
}

export { usePlugins }
