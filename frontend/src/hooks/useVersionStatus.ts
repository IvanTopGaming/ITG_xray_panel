import { useQuery } from '@tanstack/react-query';
import { getVersionInfo, isNewer } from '@/lib/version';
import { panelRole } from '@/lib/panelRole';

export interface ServiceStatus {
  key: string;
  label: string;
  current: string;
  latest: string | null;
  updateAvailable: boolean;
}

export function useVersionStatus() {
  const query = useQuery({
    queryKey: ['system', 'version'],
    queryFn: getVersionInfo,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  });

  const data = query.data;
  const latest = data?.latest ?? null;

  const services: ServiceStatus[] = [];

  const backendKey = data?.running.backend_key ?? panelRole;
  const backendCurrent = data?.running.backend ?? __APP_VERSIONS__[backendKey] ?? 'dev';
  services.push({
    key: 'backend',
    label: backendKey,
    current: backendCurrent,
    latest: latest?.[backendKey] ?? null,
    updateAvailable: isNewer(latest?.[backendKey], backendCurrent),
  });

  services.push({
    key: 'frontend',
    label: 'frontend',
    current: __APP_VERSIONS__.frontend,
    latest: latest?.frontend ?? null,
    updateAvailable: isNewer(latest?.frontend, __APP_VERSIONS__.frontend),
  });

  if (data?.running.bot) {
    services.push({
      key: 'bot',
      label: 'bot',
      current: data.running.bot,
      latest: latest?.bot ?? null,
      updateAvailable: isNewer(latest?.bot, data.running.bot),
    });
  }

  services.push({
    key: 'xray',
    label: 'xray',
    current: __APP_VERSIONS__.xray_core_ref,
    latest: latest?.xray_core_ref ?? null,
    updateAvailable: isNewer(latest?.xray_core_ref, __APP_VERSIONS__.xray_core_ref),
  });

  const hasUpdates = services.some((s) => s.updateAvailable);

  return { services, hasUpdates, query };
}
