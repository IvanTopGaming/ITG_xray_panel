import { useCallback, useEffect, useState } from 'react';
import type { SubInfo } from '@/lib/types';

const REQUEST_TIMEOUT_MS = 10000;

function infoUrl(): string {
  return `${window.location.pathname.replace(/\/+$/, '')}/info${window.location.search}`;
}

export function useSubInfo() {
  const [data, setData] = useState<SubInfo | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    setError(false);
    fetch(infoUrl(), {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then((body: SubInfo) => setData(body))
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, error, loading, reload: load };
}
