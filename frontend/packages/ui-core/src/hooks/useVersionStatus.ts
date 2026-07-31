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
    // §10.8: sub, bot-api and cron have no UI of their own, so until they started stamping their
    // version into the shared Redis the only thing this row could show was what the release said
    // they ought to be running.
    const reported = data?.running.roles?.[roleKey]?.version ?? null;
    if (publishedVersion || reported) {
      services.push({
        key: `backend-${roleKey}`,
        label: roleKey,
        current: reported,
        latest: publishedVersion,
        updateAvailable: isNewer(publishedVersion, reported),
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
