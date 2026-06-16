import { useQuery, keepPreviousData } from '@tanstack/react-query';
import api from '../../lib/api';
import { SeriesAgg } from '../../lib/types';

function rangeWindow(range: string): { from: number; to: number; refetchInterval: number } {
  const now = Math.floor(Date.now() / 1000);
  switch (range) {
    case '1h':
      return { from: now - 3600, to: now, refetchInterval: 30000 };
    case '24h':
      return { from: now - 86400, to: now, refetchInterval: 30000 };
    case '7d':
      return { from: now - 604800, to: now, refetchInterval: 30000 };
    case '30d':
      return { from: now - 2592000, to: now, refetchInterval: 30000 };
    case 'live':
    default:
      return { from: now - 300, to: now, refetchInterval: 2000 };
  }
}

export function useSeries(
  metric: string,
  scope: string,
  entity: string,
  range: string,
  enabled = true,
  override?: { from: number; to: number }
) {
  return useQuery<{ ts: number; value: number }[]>({
    queryKey: ['mon-series', metric, scope, entity, range, override?.from, override?.to],
    enabled,
    queryFn: async () => {
      const { from, to } = override ?? rangeWindow(range);
      const params = new URLSearchParams({
        metric,
        scope,
        entity,
        from: String(from),
        to: String(to),
        points: '400',
      });
      const data = (await api.get(`/monitoring/series?${params.toString()}`)).data as {
        points?: SeriesAgg[];
      };
      const points = data.points ?? [];
      return points.map((p) => ({
        ts: p.Ts ?? (p as unknown as { ts: number }).ts,
        value: p.Avg ?? (p as unknown as { avg: number }).avg,
      }));
    },
    refetchInterval: override ? 30000 : rangeWindow(range).refetchInterval,
    placeholderData: keepPreviousData,
  });
}
