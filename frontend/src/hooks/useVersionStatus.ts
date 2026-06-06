import { useQuery } from '@tanstack/react-query';
import { getVersionInfo, isNewer } from '@/lib/version';

export interface ServiceStatus {
  key: string;
  label: string;
  current: string;
  latest: string | null;
  updateAvailable: boolean;
}

/**
 * Per-service version status for the four shown services.
 * - backend / bot: live (from the API `running` block).
 * - frontend / xray: build-time constant (their own running build).
 * - bot row is included only when the bot reported recently (API nulls it otherwise).
 */
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

  const backendCurrent = data?.running.backend ?? __APP_VERSIONS__.backend;
  services.push({
    key: 'backend',
    label: 'backend',
    current: backendCurrent,
    latest: latest?.backend ?? null,
    updateAvailable: isNewer(latest?.backend, backendCurrent),
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
