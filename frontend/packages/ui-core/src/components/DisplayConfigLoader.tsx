import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import api from '@/lib/api';
import { setDisplayTimezone } from '@/lib/datetime';

export function DisplayConfigLoader() {
  const { data } = useQuery({
    queryKey: ['display-config'],
    queryFn: async () => {
      const { data } = await api.get<{ display_timezone?: string }>('/bot/settings');
      return data?.display_timezone || 'Europe/Moscow';
    },
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    if (data) setDisplayTimezone(data);
  }, [data]);

  return null;
}
