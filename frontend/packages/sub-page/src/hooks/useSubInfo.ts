import { useCallback, useEffect, useRef, useState } from 'react';
import type { SubInfo } from '@/lib/types';

const REQUEST_TIMEOUT_MS = 10000;

function infoUrl(): string {
  return `${window.location.pathname.replace(/\/+$/, '')}/info${window.location.search}`;
}

export function useSubInfo() {
  const [data, setData] = useState<SubInfo | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);

  const attempt = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    attempt.current?.abort();
    const controller = new AbortController();
    attempt.current = controller;
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const current = () => attempt.current === controller;

    setLoading(true);
    setError(false);
    fetch(infoUrl(), {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then((body: SubInfo) => {
        if (current()) setData(body);
      })
      .catch(() => {
        if (current()) setError(true);
      })
      .finally(() => {
        window.clearTimeout(timer);
        if (current()) setLoading(false);
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, error, loading, reload: load };
}
