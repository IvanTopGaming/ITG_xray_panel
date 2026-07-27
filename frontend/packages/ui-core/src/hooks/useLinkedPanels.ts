import { useQuery } from '@tanstack/react-query';
import api from '@ui/lib/api';
import { isWorker } from '@ui/lib/panelRole';
import type { LinkedPanel } from '@ui/lib/types';

export function useLinkedPanels(enabled: boolean = true) {
  return useQuery<LinkedPanel[]>({
    queryKey: ['panels'],
    queryFn: async () => (await api.get<LinkedPanel[]>('/panels')).data,
    enabled: enabled && !isWorker,
  });
}
