import { useQuery } from '@tanstack/react-query';
import { getVersionInfo, isNewer } from '@ui/lib/version';
import { panelRole } from '@ui/lib/panelRole';

export interface ServiceStatus {
  key: string;
  label: string;
  current: string | null;
  latest: string | null;
  updateAvailable: boolean;
  isLocal?: boolean;
  silentSince?: number | null;
}

const BACKEND_ROLE_ORDER = ['master', 'worker', 'sub', 'bot_api', 'cron'] as const;

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

  for (const roleKey of BACKEND_ROLE_ORDER) {
    if (roleKey === backendKey) {
      services.push({
        key: `backend-${roleKey}`,
        label: roleKey,
        current: backendCurrent,
        latest: latest?.[roleKey] ?? null,
        updateAvailable: isNewer(latest?.[roleKey], backendCurrent),
        isLocal: true,
      });
      continue;
    }

    const publishedVersion = latest?.[roleKey] ?? null;
    const report = data?.running.roles?.[roleKey] ?? null;
    const silent = report?.state === 'silent';
    const reported = silent ? null : (report?.version ?? null);
    if (publishedVersion || report) {
      services.push({
        key: `backend-${roleKey}`,
        label: roleKey,
        current: reported,
        latest: publishedVersion,
        updateAvailable: isNewer(publishedVersion, reported),
        silentSince: silent ? (report?.reported_at ?? null) : null,
      });
    }
  }

  services.push({
    key: __FRONTEND_VERSION_KEY__,
    label: __FRONTEND_VERSION_KEY__,
    current: __APP_VERSIONS__[__FRONTEND_VERSION_KEY__],
    latest: latest?.[__FRONTEND_VERSION_KEY__] ?? null,
    updateAvailable: isNewer(
      latest?.[__FRONTEND_VERSION_KEY__],
      __APP_VERSIONS__[__FRONTEND_VERSION_KEY__]
    ),
  });

  const botSilent = data?.running.bot_state === 'silent';
  if (data?.running.bot || botSilent) {
    services.push({
      key: 'bot',
      label: 'bot',
      current: data?.running.bot ?? null,
      latest: latest?.bot ?? null,
      updateAvailable: isNewer(latest?.bot, data?.running.bot),
      silentSince: botSilent ? (data?.running.bot_reported_at ?? null) : null,
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
