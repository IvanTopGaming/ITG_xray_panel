import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import api from '@ui/lib/api';
import { setDisplayTimezone } from '@ui/lib/datetime';
import { isWorker } from '@ui/lib/panelRole';

export function DisplayConfigLoader() {
  const { data } = useQuery({
    queryKey: ['display-config'],
    queryFn: async () => {
      const { data } = await api.get<{ display_timezone?: string }>('/bot/settings');
      return data?.display_timezone || 'Europe/Moscow';
    },
    staleTime: 5 * 60 * 1000,
    enabled: !isWorker,
  });

  useEffect(() => {
    if (data) setDisplayTimezone(data);
  }, [data]);

  return null;
}
