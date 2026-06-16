import { useQuery } from '@tanstack/react-query';
import api from '../../lib/api';
import { MonitoringSnapshot } from '../../lib/types';

export function useSnapshot() {
  return useQuery<MonitoringSnapshot>({
    queryKey: ['mon-snapshot'],
    queryFn: async () => (await api.get('/monitoring/snapshot')).data,
    refetchInterval: 2000,
  });
}
